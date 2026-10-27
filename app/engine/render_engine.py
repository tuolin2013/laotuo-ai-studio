"""渲染引擎：并发生成候选图 / 视频首帧，以及调用视频后端生成带声视频。

所有方法都是异步生成器，逐条产出 RenderEvent，UI 层直接 yield 到运行日志与画廊，
生成一张回显一张；每完成一张立即落盘 project.json。

画图：每张候选图都是一个独立的 RightAPI 异步任务（"async": true + 轮询 /v1/tasks），
多个任务并发进行，互不等待；同时进行的任务数由 settings.image_concurrency 控制。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.engine.image_router import image_key, route_image_request
from app.engine.prompt_compiler import DEFAULT_H3_SKILL, PromptCompiler, h3_mode_label
from app.engine.runtime import EngineRuntime
from app.providers.demo.aesthetics import pill_from_copy
from app.providers.demo.render import DemoArt, render_demo_image
from app.providers.image import gpt_image_size, gpt_quality_for, is_experimental_size
from app.providers.image_models import CHANNEL_LABELS, get_model, live_channel_of, protocol_of
from app.providers.video import VideoProviderError, VideoRequest
from app.schemas.common import mode_for_reference_count
from app.schemas.editable import ReferenceAsset
from app.schemas.motion import VISUAL_USAGE, MotionAd, MotionAsset, MotionScene, visual_ratio
from app.schemas.plan import AESTHETIC_STYLE_NAMES, CandidateImage, ImagePlan, ImageSlot
from app.schemas.project import Project
from app.schemas.video import StoryboardShot, VideoScript

# 提示词编译（LLM）并发上限；画图任务并发由 settings.image_concurrency 控制
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


_REF_ROLE = re.compile(r"^参考图 ([A-Za-z0-9_]+)")
# 动效素材作为生图参考时的角色名（参考图沿用上传时登记的角色，如 product_package）
MOTION_REF_ROLES = {"main_image": "product_main_image", "detail_page": "product_detail_page",
                    "first_frame": "product_scene_frame", "upload": "product_photo", "screen": "screenshot",
                    "generated": "ai_scene", "reference": "product_reference"}


def motion_reference(asset: MotionAsset) -> ReferenceAsset:
    """动效素材 → 生图参考图（角色名会写进生图指令：图1=product_package …）。"""
    m = _REF_ROLE.match(asset.label) if asset.source == "reference" else None
    role = m.group(1) if m else MOTION_REF_ROLES.get(asset.source, "product_photo")
    return ReferenceAsset(role=role, file_path=asset.path)


def motion_visual_data(scene: MotionScene, aspect_ratio: str, theme: str) -> dict:
    """动效 AI 画面的生图输入：只有画面，不排版文字（标题、卖点由动效引擎另外叠加）。"""
    return {
        "title": "",
        "copy_text": "",
        "visual_description": f"{scene.visual}。（用途：动效广告「{scene.label}」屏的{VISUAL_USAGE[scene.kind]}）"
                              "标题与卖点由动效引擎另外叠加，画面中不要出现任何文字、logo 或水印。",
        "aspect_ratio": aspect_ratio,
        "aesthetic_style": theme if theme in AESTHETIC_STYLE_NAMES else "auto",
        "composition_mode": "auto",
    }


class RenderEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt
        self.compiler = PromptCompiler(rt)

    def _save(self, project: Project) -> None:
        self.rt.store.save(project)

    # ------------------------------------------------------------ 单张生图

    async def _generate_one(
        self, *, model: str, prompt: str, aspect_ratio: str, references: list[Path], out_path: Path,
        art: DemoArt, resolution: str | None, quality: str | None,
        emit: Callable[[str], Awaitable[None]],
    ) -> Path:
        """生成 1 张图（真实接口或演示渲染），返回保存路径。"""
        if self.rt.is_live(live_channel_of(model)):
            req = route_image_request(self.rt.settings, model, prompt, aspect_ratio, references,
                                      resolution=resolution, quality=quality)
            client = self.rt.image_client(self.rt.settings.image_base_url(model))
            data = (await client.send(req, image_key(self.rt.settings, model), on_event=emit))[0]
            out_path.write_bytes(data)
            return out_path
        await asyncio.sleep(self.rt.demo_delay)
        return await asyncio.to_thread(render_demo_image, art, aspect_ratio, out_path)

    def _mode_label(self, model: str) -> str:
        """日志用：渠道 + 真实 / 演示。"""
        ch = CHANNEL_LABELS[get_model(model).channel]
        return f"image {ch}渠道 · {'真实 API' if self.rt.is_live(live_channel_of(model)) else '演示模式'}"

    def describe_request(self, model: str, aspect_ratio: str, n_refs: int, resolution: str | None = None,
                         quality: str | None = None) -> str:
        """日志用：说明本次会调用哪个接口与参数，便于核对协议是否正确。"""
        tier = (resolution or self.rt.settings.image_resolution).upper()
        m = get_model(model)
        if m.protocol == "openai":
            path = "/v1/images/edits" if n_refs else "/v1/images/generations"
            size = gpt_image_size(aspect_ratio, tier)
            q = gpt_quality_for(model, quality or self.rt.settings.image_quality)
            warn = "（超过 2560x1440 属实验性大图，耗时更长）" if is_experimental_size(size) else ""
            return f"OpenAI 生成 {path} · size {size} · quality {q}{warn}"
        return f"Gemini 生成 /v1beta/models/{m.request_model}:generateContent · {aspect_ratio} · imageSize {tier}"

    async def _render_items(
        self,
        *,
        project: Project,
        items: list[tuple[int, object]],
        label_of: Callable[[int, object], str],
        compile_prompt: Callable[[object, list], Awaitable[str]],
        on_prompt: Callable[[object, str], None],
        count_of: Callable[[object], int],
        refs_of: Callable[[object], list],
        ratio_of: Callable[[object], str],
        art_of: Callable[[int, object, list, int, int], DemoArt],
        out_dir_of: Callable[[object], Path],
        on_image: Callable[[object, Path, str], None],
        model: str,
        resolution: str | None,
        quality: str | None,
        noun: str,
    ) -> AsyncIterator[RenderEvent]:
        """通用并发渲染：先编译每个条目的提示词，再把每张图作为独立异步任务并发提交。"""
        queue: asyncio.Queue[RenderEvent] = asyncio.Queue()
        llm_sem = asyncio.Semaphore(MAX_PARALLEL)
        img_sem = asyncio.Semaphore(max(1, self.rt.settings.image_concurrency))

        async def work(index: int, item) -> None:
            label = label_of(index, item)
            item_id = getattr(item, "item_id", None)
            refs = refs_of(item)
            n = count_of(item)
            try:
                async with llm_sem:
                    prompt = await compile_prompt(item, refs)
                on_prompt(item, prompt)
                await queue.put(RenderEvent(
                    f"{label} 提交 {n} 个{noun}任务："
                    f"{self.describe_request(model, ratio_of(item), len(refs), resolution, quality)}",
                    item_id=item_id))
            except Exception as e:  # 单个条目失败不影响其他条目
                await queue.put(RenderEvent(f"❌ {label} 提示词编译失败：{e}", item_id=item_id, error=True))
                return
            out_dir = out_dir_of(item)

            async def one(i: int) -> None:
                async def emit(msg: str) -> None:
                    await queue.put(RenderEvent(f"{label} {noun}{i + 1}：{msg}", item_id=item_id))

                try:
                    async with img_sem:
                        path = await self._generate_one(
                            model=model, prompt=prompt, aspect_ratio=ratio_of(item),
                            references=[a.file_path for a in refs],
                            out_path=out_dir / f"{item_id}_{_ts()}_{i}.png",
                            art=art_of(index, item, refs, i, n), resolution=resolution, quality=quality,
                            emit=emit)
                    on_image(item, path, prompt)
                    await queue.put(RenderEvent(f"✅ {label} {noun}{i + 1} 完成：{path.name}", image_path=path,
                                                item_id=item_id))
                except Exception as e:  # 单张失败不影响其他候选
                    await queue.put(RenderEvent(f"❌ {label} {noun}{i + 1} 失败：{e}", item_id=item_id,
                                                error=True))

            await asyncio.gather(*(one(i) for i in range(n)))

        tasks = [asyncio.create_task(work(i, it)) for i, it in items]
        async for ev in self._drain(queue, tasks):
            if ev.image_path:
                self._save(project)
            yield ev
        self._save(project)

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

    # ------------------------------------------------------------ 主图 / 详情页

    async def render_plan(
        self,
        project: Project,
        plan: ImagePlan,
        model: str,
        skill_id: str,
        slot_ids: list[str] | None = None,
        resolution: str | None = None,
        quality: str | None = None,
    ) -> AsyncIterator[RenderEvent]:
        plan.require_confirmed()
        protocol_of(model)  # 未登记协议的模型直接报错，绝不调用错误的接口
        missing = plan.missing_references()
        if missing:
            yield RenderEvent("⚠️ 以下参考图尚未上传，相关槽位将按已有参考图（可能退化为文生图）生成：" +
                              "、".join(m.role for m in missing))
        slots = [s for s in plan.slots if slot_ids is None or s.slot_id in slot_ids]
        cat = self.rt.category(plan.category_id)
        res = (resolution or self.rt.settings.image_resolution).upper()
        mode = self._mode_label(model)
        total = sum(s.candidate_count for s in slots)
        yield RenderEvent(f"🚀 开始并发生成 {len(slots)} 个槽位 / {total} 张（{model} · {res} · {mode}，"
                          f"同时进行 {self.rt.settings.image_concurrency} 个画图任务）")
        prompt_dir = self.rt.store.asset_dir(project.project_id, "prompts")

        def on_prompt(slot: ImageSlot, prompt: str) -> None:
            slot.compiled_prompt = prompt
            (prompt_dir / f"{slot.slot_id}.txt").write_text(prompt, encoding="utf-8")

        def art_of(index, slot: ImageSlot, refs, i, n) -> DemoArt:
            return DemoArt(
                title=slot.title, subtitle=slot.subtitle, copy_text=slot.copy_text,
                description=slot.visual_description, mode=mode_for_reference_count(len(refs)), model=model,
                palette=cat.visual_style.palette, references=[a.file_path for a in refs],
                reference_labels=[a.role for a in refs], seed=f"{slot.slot_id}-{i}-{_ts()}",
                badge=f"{res} · 候选 {i + 1}/{n}",
                aesthetic=slot.aesthetic_style, composition=slot.composition_mode,
                pill=pill_from_copy(slot.copy_text, slot.title, slot.subtitle),
            )

        async for ev in self._render_items(
            project=project,
            items=[(i, s) for i, s in enumerate(plan.slots) if s in slots],
            label_of=lambda i, s: f"#{i + 1}「{s.title}」",
            compile_prompt=lambda s, refs: self.compiler.slot_prompt(s, refs, plan.category_id, plan.platform_id,
                                                                     skill_id, plan.deliverable_type),
            on_prompt=on_prompt,
            count_of=lambda s: s.candidate_count,
            refs_of=lambda s: plan.references_for(s.slot_id),
            ratio_of=lambda s: s.aspect_ratio,
            art_of=art_of,
            out_dir_of=lambda s: self.rt.store.asset_dir(project.project_id, "candidates", s.slot_id),
            on_image=lambda s, p, prompt: s.candidates.append(CandidateImage(file_path=p, model=model, prompt=prompt)),
            model=model, resolution=res, quality=quality, noun="候选",
        ):
            yield ev
        done = sum(len(s.candidates) for s in slots)
        yield RenderEvent(f"🎉 全部完成，本轮共 {len(slots)} 个槽位，累计候选图 {done} 张", done=True)

    # ------------------------------------------------------------ 视频首帧

    async def render_first_frames(
        self, project: Project, script: VideoScript, model: str, skill_id: str, shot_ids: list[str] | None = None,
        count: int = 1, resolution: str | None = None, quality: str | None = None,
    ) -> AsyncIterator[RenderEvent]:
        script.require_confirmed()
        protocol_of(model)
        cat = self.rt.category(script.category_id)
        shots = [s for s in script.shots if shot_ids is None or s.shot_id in shot_ids]
        res = (resolution or self.rt.settings.image_resolution).upper()
        missing = script.missing_references()
        if missing:
            yield RenderEvent("⚠️ 以下参考图尚未上传，相关首帧将按已有参考图（可能退化为文生图）生成：" +
                              "、".join(m.role for m in missing))
        yield RenderEvent(f"🖼️ 开始生成 {len(shots)} 个分镜首帧（比例锁定 {script.aspect_ratio}，{model} · {res} · "
                          f"{self._mode_label(model)}）")

        def on_prompt(shot: StoryboardShot, prompt: str) -> None:
            shot.first_frame_prompt = prompt

        def art_of(index, shot: StoryboardShot, refs, i, n) -> DemoArt:
            return DemoArt(
                title=f"镜头 {index + 1} · 首帧", subtitle=f"{shot.shot_size} {shot.camera_movement}",
                copy_text="", description=shot.first_frame_description,
                mode=mode_for_reference_count(len(refs)), model=model, palette=cat.visual_style.palette,
                references=[a.file_path for a in refs], reference_labels=[a.role for a in refs],
                seed=f"{shot.shot_id}-{i}-{_ts()}", badge=f"{res} · 候选 {i + 1}/{n}",
            )

        def on_image(shot: StoryboardShot, path: Path, prompt: str) -> None:
            script.add_first_frame(shot.shot_id, path, model=model, prompt=prompt, aspect_ratio=script.aspect_ratio)

        async for ev in self._render_items(
            project=project,
            items=[(i, s) for i, s in enumerate(script.shots) if s in shots],
            label_of=lambda i, s: f"镜头 {i + 1}",
            compile_prompt=lambda s, refs: self.compiler.first_frame_prompt(script, s, refs, skill_id),
            on_prompt=on_prompt,
            count_of=lambda s: count,
            refs_of=lambda s: script.references_for(s.shot_id),
            ratio_of=lambda s: script.aspect_ratio,
            art_of=art_of,
            out_dir_of=lambda s: self.rt.store.asset_dir(project.project_id, "first_frames", s.shot_id),
            on_image=on_image,
            model=model, resolution=res, quality=quality, noun="首帧",
        ):
            yield ev
        yield RenderEvent("🎉 首帧生成完成，请逐镜检查商品包装与文字无变形后锁定", done=True)

    # ------------------------------------------------------------ 动效广告 AI 画面

    async def render_motion_visuals(
        self, project: Project, ad: MotionAd, model: str, skill_id: str, scene_ids: list[str] | None = None,
        resolution: str | None = None, quality: str | None = None,
    ) -> AsyncIterator[RenderEvent]:
        """为写了「AI 画面」的动效分镜各生成 1 张画面（scene_ids 为空时只生成待生成 / 描述改过的分镜）。

        每完成一张就加进素材库并绑定到该屏（MotionAd.bind_visual），旧画面留在素材库里不删除。
        出图比例按该屏版式的图片框选择（钩子是全屏背景，与成片同比例）。
        """
        protocol_of(model)  # 未登记协议的模型直接报错，绝不调用错误的接口
        if scene_ids is None:
            reused = ad.reuse_visuals()  # 描述与之前生成过的画面一字不差的屏直接沿用，不重复花钱
            if reused:
                self._save(project)
                yield RenderEvent(f"♻️ {reused} 屏的描述与之前生成过的画面相同，直接沿用")
        try:
            targets = ad.visual_targets(scene_ids)
        except ValueError as e:
            yield RenderEvent(f"⚠️ {e}", error=True, done=True)
            return
        if scene_ids is None and ad.draft_visuals:
            yield RenderEvent(f"⚠️ 第 {'、'.join(map(str, ad.draft_visuals))} 屏的「AI 画面」还是占位文字，本轮跳过："
                              "改成真实的画面描述后再点一次")
        if not targets:
            yield RenderEvent("没有需要生成的 AI 画面：表格「AI 画面」列写了描述、还没生成或改过描述的分镜才会生成",
                              done=True)
            return
        cat = self.rt.category(project.category_id)
        res = (resolution or self.rt.settings.image_resolution).upper()
        cost = "，消耗画图额度" if self.rt.is_live(live_channel_of(model)) else ""
        yield RenderEvent(f"🎨 开始生成 {len(targets)} 屏 AI 画面（每屏 1 张 · {model} · {res} · "
                          f"{self._mode_label(model)}{cost}）")
        prompt_dir = self.rt.store.asset_dir(project.project_id, "prompts")
        ratios = {s.scene_id: visual_ratio(s.kind, ad.aspect_ratio) for s in targets}
        refs = {s.scene_id: [motion_reference(a) for a in ad.visual_references(s)] for s in targets}
        ok: list[str] = []

        def on_prompt(scene: MotionScene, prompt: str) -> None:
            (prompt_dir / f"motion_{scene.scene_id}.txt").write_text(prompt, encoding="utf-8")

        def art_of(index, scene: MotionScene, rs, i, n) -> DemoArt:
            return DemoArt(
                title=f"第 {index + 1} 屏 · AI 画面", subtitle=scene.label, description=scene.visual,
                mode=mode_for_reference_count(len(rs)), model=model, palette=cat.visual_style.palette,
                references=[a.file_path for a in rs], reference_labels=[a.role for a in rs],
                seed=f"{scene.scene_id}-{_ts()}", badge=f"{res} · {ratios[scene.scene_id]}",
                aesthetic=ad.theme if ad.theme in AESTHETIC_STYLE_NAMES else "auto",
            )

        def on_image(scene: MotionScene, path: Path, prompt: str) -> None:
            with Image.open(path) as im:
                w, h = im.size
            # 生成期间重新编排过方案时，挂到当前方案上（描述相同的分镜直接用上）
            (project.motion_ad or ad).bind_visual(scene.scene_id, path, visual=scene.visual, model=model,
                                                  width=w, height=h)
            ok.append(scene.scene_id)

        async for ev in self._render_items(
            project=project,
            items=[(i, s) for i, s in enumerate(ad.scenes) if s in targets],
            label_of=lambda i, s: f"第 {i + 1} 屏「{s.label}」",
            compile_prompt=lambda s, rs: self.compiler.image_prompt(
                motion_visual_data(s, ratios[s.scene_id], ad.theme), rs, project.category_id, project.platform_id,
                ratios[s.scene_id], skill_id),
            on_prompt=on_prompt,
            count_of=lambda s: 1,
            refs_of=lambda s: refs[s.scene_id],
            ratio_of=lambda s: ratios[s.scene_id],
            art_of=art_of,
            out_dir_of=lambda s: self.rt.store.asset_dir(project.project_id, "motion", "visuals"),
            on_image=on_image,
            model=model, resolution=res, quality=quality, noun="画面",
        ):
            yield ev
        failed = len(targets) - len(ok)
        if failed:
            msg = (f"⚠️ AI 画面完成 {len(ok)}/{len(targets)} 屏，{failed} 屏失败：再点一次「🎨 生成 AI 画面」"
                   "只重做没完成的")
        else:
            msg = f"🎉 AI 画面生成完成：{len(ok)}/{len(targets)} 屏，可以渲染了"
        yield RenderEvent(msg, done=True, error=bool(failed))

    # ------------------------------------------------------------ 视频

    async def compile_h3(
        self, project: Project, script: VideoScript, shot: StoryboardShot, skill_id: str | None = None
    ) -> str:
        """编译单个分镜的 H3 提示词（官方卡带自动选择 I2VA / Ref2VA），落盘到 prompts/ 并保存项目。"""
        prompt = await self.compiler.h3_prompt(script, shot, skill_id or DEFAULT_H3_SKILL)
        prompt_dir = self.rt.store.asset_dir(project.project_id, "prompts")
        (prompt_dir / f"h3_{shot.shot_id}.txt").write_text(prompt, encoding="utf-8")
        self._save(project)
        return prompt

    async def render_video(
        self, project: Project, script: VideoScript, shot_id: str, provider_id: str = "modal_minimax_h3",
        skill_id: str | None = None,
    ) -> AsyncIterator[RenderEvent]:
        """生成单个分镜的带声视频。还没有 H3 提示词时先自动编写，再提交视频后端。

        每次生成都追加为该分镜的新版本（不覆盖旧视频）。
        """
        frame = script.require_first_frame_confirmed(shot_id)
        shot = script.get_shot(shot_id)
        if not shot.h3_prompt:
            yield RenderEvent("✍️ 正在自动编写 H3 音画提示词…", item_id=shot_id)
            await self.compile_h3(project, script, shot, skill_id)
            yield RenderEvent(f"✅ H3 提示词已生成（{h3_mode_label(shot)}）", item_id=shot_id)
        provider = self.rt.video_provider(provider_id)
        yield RenderEvent(f"🎬 视频后端：{provider.display_name}", item_id=shot_id)
        refs = [a for a in script.references_for(shot_id) if a.file_path.exists()]
        req = VideoRequest(
            shot_id=shot_id, prompt=shot.h3_prompt, first_frame_path=frame.file_path,
            aspect_ratio=script.aspect_ratio, duration_seconds=shot.duration_seconds,
            output_dir=self.rt.store.asset_dir(project.project_id, "videos", shot_id),
            with_audio=shot.audio_mode != "silent",
            # ref2va：首帧之后附上该分镜绑定的商品参考图，保证包装一致
            extra={
                "reference_paths": [a.file_path for a in refs],
                "reference_roles": [a.role for a in refs],
                # 官方 h3-prompt-writing 模式（i2va / ref2va）；旧版卡带为 None
                "h3_prompt_mode": shot.h3_official_mode,
                "seed": int(hashlib.sha256(script.script_id.encode()).hexdigest()[:8], 16),
            },
        )
        try:
            async for ev in provider.generate(req):
                yield RenderEvent(f"[{ev.stage}] {ev.message}", item_id=shot_id)
                if ev.video_path:
                    script.bind_video(shot_id, ev.video_path, provider=provider.provider_id)
                    self._save(project)
                    n = len(script.get_shot(shot_id).videos)
                    yield RenderEvent(f"✅ 视频已绑定到分镜（第 {n} 版）", video_path=ev.video_path,
                                      item_id=shot_id, done=True)
        except VideoProviderError as e:
            yield RenderEvent(f"❌ 视频生成失败：{e}", item_id=shot_id, error=True, done=True)
