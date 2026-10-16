"""渲染引擎：并发生成候选图 / 视频首帧，以及调用视频后端生成带声视频。

所有方法都是异步生成器，逐条产出 RenderEvent，UI 层直接 yield 到运行日志与画廊，
生成一张回显一张；每完成一张立即落盘 project.json。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from app.engine.image_router import image_key, route_image_request
from app.engine.prompt_compiler import PromptCompiler
from app.engine.runtime import EngineRuntime
from app.providers.demo.render import DemoArt, render_demo_image
from app.providers.video import ManualActionRequired, VideoProviderError, VideoRequest
from app.schemas.common import mode_for_reference_count
from app.schemas.plan import CandidateImage, ImagePlan, ImageSlot
from app.schemas.project import Project
from app.schemas.video import StoryboardShot, VideoScript

MAX_PARALLEL = 4


@dataclass(frozen=True)
class RenderEvent:
    message: str
    image_path: Path | None = None
    video_path: Path | None = None
    item_id: str | None = None
    done: bool = False
    error: bool = False


def _ts() -> str:
    return str(int(time.time() * 1000))


class RenderEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt
        self.compiler = PromptCompiler(rt)

    def _save(self, project: Project) -> None:
        self.rt.store.save(project)

    # ------------------------------------------------------------ 单张生图

    async def _generate_images(
        self, *, model: str, prompt: str, aspect_ratio: str, references: list[Path], n: int, out_dir: Path,
        prefix: str, art: DemoArt, resolution: str | None = None,
    ) -> list[Path]:
        """真实接口或演示渲染，返回保存后的图片路径。"""
        out_dir.mkdir(parents=True, exist_ok=True)
        if self.rt.is_live("image"):
            req = route_image_request(self.rt.settings, model, prompt, aspect_ratio, references, n=n,
                                      resolution=resolution)
            key = image_key(self.rt.settings)
            client = self.rt.image_client()
            images: list[bytes] = []
            if req.family == "gemini":
                # Nano-Banana 一次返回 1 张，候选图通过并发多次请求获得
                results = await asyncio.gather(*(client.send(req, key) for _ in range(n)))
                for r in results:
                    images.extend(r[:1])
            else:
                images = await client.send(req, key)
            paths = []
            for i, data in enumerate(images[:n]):
                p = out_dir / f"{prefix}_{_ts()}_{i}.png"
                p.write_bytes(data)
                paths.append(p)
            return paths
        paths = []
        for i in range(n):
            await asyncio.sleep(self.rt.demo_delay)
            art.seed = f"{prefix}-{i}-{_ts()}"
            art.badge = f"候选 {i + 1}/{n}"
            paths.append(await asyncio.to_thread(
                render_demo_image, art, aspect_ratio, out_dir / f"{prefix}_{_ts()}_{i}.png"))
        return paths

    # ------------------------------------------------------------ 主图 / 详情页

    async def render_plan(
        self,
        project: Project,
        plan: ImagePlan,
        model: str,
        skill_id: str,
        slot_ids: list[str] | None = None,
        resolution: str | None = None,
    ) -> AsyncIterator[RenderEvent]:
        plan.require_confirmed()
        missing = plan.missing_references()
        if missing:
            yield RenderEvent("⚠️ 以下参考图尚未上传，相关槽位将按已有参考图（可能退化为文生图）生成：" +
                              "、".join(m.role for m in missing))
        slots = [s for s in plan.slots if slot_ids is None or s.slot_id in slot_ids]
        cat = self.rt.category(plan.category_id)
        mode = "真实 API" if self.rt.is_live("image") else "演示模式"
        res = resolution or self.rt.settings.image_resolution
        yield RenderEvent(
            f"🚀 开始并发生成 {len(slots)} 个槽位（模型 {model}，{res}，{mode}，并发上限 {MAX_PARALLEL}）")

        queue: asyncio.Queue[RenderEvent] = asyncio.Queue()
        sem = asyncio.Semaphore(MAX_PARALLEL)

        async def work(index: int, slot: ImageSlot) -> None:
            label = f"#{index + 1}「{slot.title}」"
            refs = plan.references_for(slot.slot_id)
            try:
                async with sem:
                    await queue.put(RenderEvent(f"{label} 编译提示词（实际参考图 {len(refs)} 张）",
                                                item_id=slot.slot_id))
                    prompt = await self.compiler.slot_prompt(slot, refs, plan.category_id, plan.platform_id, skill_id)
                    slot.compiled_prompt = prompt
                    prompt_dir = self.rt.store.asset_dir(project.project_id, "prompts")
                    (prompt_dir / f"{slot.slot_id}.txt").write_text(prompt, encoding="utf-8")
                    await queue.put(RenderEvent(f"{label} 生成 {slot.candidate_count} 张候选图中…",
                                                item_id=slot.slot_id))
                    art = DemoArt(
                        title=slot.title, subtitle=slot.subtitle, copy_text=slot.copy_text,
                        description=slot.visual_description, mode=mode_for_reference_count(len(refs)), model=model,
                        palette=cat.visual_style.palette, references=[a.file_path for a in refs],
                        reference_labels=[a.role for a in refs],
                    )
                    out_dir = self.rt.store.asset_dir(project.project_id, "candidates", slot.slot_id)
                    paths = await self._generate_images(
                        model=model, prompt=prompt, aspect_ratio=slot.aspect_ratio,
                        references=[a.file_path for a in refs], n=slot.candidate_count, out_dir=out_dir,
                        prefix=slot.slot_id, art=art, resolution=res,
                    )
                for p in paths:
                    slot.candidates.append(CandidateImage(file_path=p, model=model, prompt=prompt))
                    await queue.put(RenderEvent(f"✅ {label} 候选图完成：{p.name}", image_path=p, item_id=slot.slot_id))
            except Exception as e:
                await queue.put(RenderEvent(f"❌ {label} 生成失败：{e}", item_id=slot.slot_id, error=True))

        tasks = [asyncio.create_task(work(i, s)) for i, s in enumerate(plan.slots) if s in slots]
        async for ev in self._drain(queue, tasks):
            if ev.image_path:
                self._save(project)
            yield ev
        self._save(project)
        total = sum(len(s.candidates) for s in slots)
        yield RenderEvent(f"🎉 全部完成，本轮共 {len(slots)} 个槽位，累计候选图 {total} 张", done=True)

    @staticmethod
    async def _drain(queue: asyncio.Queue, tasks: list[asyncio.Task]) -> AsyncIterator[RenderEvent]:
        """边执行边产出事件，直到所有任务结束且队列清空。"""
        pending = set(tasks)
        while pending or not queue.empty():
            if not queue.empty():
                yield queue.get_nowait()
                continue
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, *pending}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield getter.result()
            else:
                getter.cancel()
            pending = {t for t in pending if not t.done()}

    # ------------------------------------------------------------ 视频首帧

    async def render_first_frames(
        self, project: Project, script: VideoScript, model: str, skill_id: str, shot_ids: list[str] | None = None,
        count: int = 1, resolution: str | None = None,
    ) -> AsyncIterator[RenderEvent]:
        script.require_confirmed()
        cat = self.rt.category(script.category_id)
        shots = [s for s in script.shots if shot_ids is None or s.shot_id in shot_ids]
        missing = script.missing_references()
        if missing:
            yield RenderEvent("⚠️ 以下参考图尚未上传，相关首帧将按已有参考图（可能退化为文生图）生成：" +
                              "、".join(m.role for m in missing))
        yield RenderEvent(f"🖼️ 开始生成 {len(shots)} 个分镜首帧（比例锁定 {script.aspect_ratio}，模型 {model}）")
        queue: asyncio.Queue[RenderEvent] = asyncio.Queue()
        sem = asyncio.Semaphore(MAX_PARALLEL)

        async def work(index: int, shot: StoryboardShot) -> None:
            label = f"镜头 {index + 1}"
            refs = script.references_for(shot.shot_id)
            try:
                async with sem:
                    prompt = await self.compiler.first_frame_prompt(script, shot, refs, skill_id)
                    shot.first_frame_prompt = prompt
                    await queue.put(RenderEvent(f"{label} 首帧提示词已编译（参考图 {len(refs)} 张）",
                                                item_id=shot.shot_id))
                    art = DemoArt(
                        title=f"镜头 {index + 1} · 首帧", subtitle=f"{shot.shot_size} {shot.camera_movement}",
                        copy_text="", description=shot.first_frame_description,
                        mode=mode_for_reference_count(len(refs)), model=model, palette=cat.visual_style.palette,
                        references=[a.file_path for a in refs], reference_labels=[a.role for a in refs],
                    )
                    out_dir = self.rt.store.asset_dir(project.project_id, "first_frames", shot.shot_id)
                    paths = await self._generate_images(
                        model=model, prompt=prompt, aspect_ratio=script.aspect_ratio,
                        references=[a.file_path for a in refs], n=count, out_dir=out_dir, prefix=shot.shot_id,
                        art=art, resolution=resolution,
                    )
                for p in paths:
                    script.add_first_frame(shot.shot_id, p, model=model, prompt=prompt,
                                           aspect_ratio=script.aspect_ratio)
                    await queue.put(RenderEvent(f"✅ {label} 首帧完成：{p.name}", image_path=p, item_id=shot.shot_id))
            except Exception as e:
                await queue.put(RenderEvent(f"❌ {label} 首帧失败：{e}", item_id=shot.shot_id, error=True))

        tasks = [asyncio.create_task(work(i, s)) for i, s in enumerate(script.shots) if s in shots]
        async for ev in self._drain(queue, tasks):
            if ev.image_path:
                self._save(project)
            yield ev
        self._save(project)
        yield RenderEvent("🎉 首帧生成完成，请逐镜检查商品包装与文字无变形后锁定", done=True)

    # ------------------------------------------------------------ 视频

    async def render_video(
        self, project: Project, script: VideoScript, shot_id: str, provider_id: str
    ) -> AsyncIterator[RenderEvent]:
        frame = script.require_first_frame_confirmed(shot_id)
        shot = script.get_shot(shot_id)
        if not shot.h3_prompt:
            raise ValueError("请先运行 H3 Skill 生成该分镜的音画提示词")
        provider = self.rt.video_provider(provider_id)
        yield RenderEvent(f"🎬 视频后端：{provider.display_name}", item_id=shot_id)
        req = VideoRequest(
            shot_id=shot_id, prompt=shot.h3_prompt, first_frame_path=frame.file_path,
            aspect_ratio=script.aspect_ratio, duration_seconds=shot.duration_seconds,
            output_dir=self.rt.store.asset_dir(project.project_id, "videos", shot_id),
            with_audio=shot.audio_mode != "silent",
        )
        try:
            async for ev in provider.generate(req):
                yield RenderEvent(f"[{ev.stage}] {ev.message}", item_id=shot_id)
                if ev.video_path:
                    script.bind_video(shot_id, ev.video_path, provider=provider.provider_id)
                    self._save(project)
                    yield RenderEvent("✅ 视频已绑定到分镜", video_path=ev.video_path, item_id=shot_id, done=True)
        except ManualActionRequired as e:
            yield RenderEvent(f"📋 {e}", item_id=shot_id, done=True)
        except VideoProviderError as e:
            yield RenderEvent(f"❌ 视频生成失败：{e}", item_id=shot_id, error=True, done=True)
