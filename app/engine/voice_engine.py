"""【配音】引擎：撰写配音文案（voice_writer Skill）、处理参考音频、逐段调用 VoxCPM2、拼接完整配音并导出字幕。

- 逐段合成：官方建议长文切成短段分别合成再拼接，比一次合成整段更稳（不会越读越快、发糊）。
- 音色一致：不给参考音频时 VoxCPM2 每次随机一个声音，所以「声音设计」要先锁定一段样音，之后每段都以它为参考音频；
  没锁定就开始生成时自动先生成一段样音并锁定。克隆 / 音色库本身就用固定的参考音频。
- 拼接：每段去掉首尾静音，停顿完全由「段后停顿」控制；整条配音统一到 -16 LUFS，同时导出 wav / mp3 / srt 字幕。
- 所有生成结果追加保存（文件名带时间戳），绝不覆盖；每段可在多个版本之间切换。
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import time
import wave
import zipfile
from array import array
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.engine.compiler import IDEA_PLACEHOLDER, SYSTEM_PROMPT
from app.engine.compliance import find_violations
from app.engine.draft_export import build_srt
from app.engine.edit_render import safe_name
from app.engine.runtime import EngineRuntime
from app.engine.video_cloner import ffmpeg_exe
from app.providers.demo.voice import demo_voice_script
from app.providers.voice import VoiceProvider, VoiceProviderError, VoiceRequest
from app.schemas.common import new_id
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.edit import Cue, split_subtitle, spoken_text
from app.schemas.project import Project
from app.schemas.voice import (
    MAX_REFERENCE_SECONDS,
    MIN_REFERENCE_SECONDS,
    PURPOSES,
    SEGMENT_MAX_CHARS,
    VoiceMix,
    VoiceProfile,
    VoiceSegment,
    VoiceSource,
    VoiceStudio,
    VoiceTake,
    compose_control,
    display_text,
    estimate_seconds,
    has_phonemes,
    preview_text_for,
    split_leading_note,
    split_script,
    spoken_len,
    tts_text,
)
from app.storage.voice_library import VoiceLibrary

SAMPLE_RATE = 48000
HEARTBEAT_SECONDS = 15
# 去首尾静音的阈值（约 -45 dBFS）与保留的余量
SILENCE_PEAK = 184
SILENCE_KEEP_SECONDS = 0.04
HEAD_SILENCE_SECONDS = 0.15
TAIL_SILENCE_SECONDS = 0.3
# 完整配音的响度：-16 LUFS 是口播 / 播客常用值，后期再叠背景音乐时留有余量
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
MAX_LLM_PAUSE = 1.5
SUBTITLE_WORDS_CHARS = 42


class VoiceError(ValueError):
    """可以直接展示给用户的中文错误。"""


@dataclass(frozen=True)
class VoiceEvent:
    """长任务的进度事件：UI 逐条写进运行日志。final=True 的事件携带任务结果。"""

    message: str
    segment_id: str | None = None
    audio_path: Path | None = None
    error: bool = False
    final: bool = False
    result: Any = None


@dataclass(frozen=True)
class ScriptResult:
    segments: list[dict]
    title: str
    notes: str
    voice_description: str
    mode: str
    warnings: list[str]


# ---------------------------------------------------------------- 配音文案 Skill


def compile_voice_writer(skill, studio: VoiceStudio, category: CategoryConfig, platform: PlatformConfig,
                         product_info: dict[str, str] | None, user_voice_idea: str | None) -> str:
    """配音文案 Skill：语言 / 方言写法 + 用途 + 目标时长 + 类目、平台、商品信息。附属指南附在指令之后。"""
    name = studio.language_label
    if studio.language == "mandarin":
        rule = "全部用标准普通话书写。"
    elif studio.dialect_control:
        rule = (f"用地道的{name}词汇和说法书写正文，不要用普通话硬套；系统会自动在风格指令里加上"
                f"「{studio.dialect_control}」，正文和 control 里都不用再写方言名。")
    else:
        rule = f"全部正文用 {name.split()[0]} 书写，不要夹杂中文；title、voice_description、control 与 notes 用中文。"
    d = studio.target_duration_seconds
    hint = (f"正文合计约 {round(d * 4.5 * 0.85)} 个字" if studio.is_cjk
            else f"正文合计约 {round(d * 2.6 * 0.85)} 个单词")
    values = {
        "language_label": name,
        "language_rule": rule,
        "purpose_label": PURPOSES[studio.purpose],
        "target_duration_seconds": d,
        "length_hint": hint + "，再加上段后停顿",
        "category_json": json.dumps(category.prompt_dict(), ensure_ascii=False, indent=2),
        "platform_json": json.dumps(platform.model_dump(mode="json"), ensure_ascii=False, indent=2),
        "product_info_json": json.dumps(product_info or {}, ensure_ascii=False, indent=2),
        "user_voice_idea": (user_voice_idea or "").strip() or IDEA_PLACEHOLDER,
    }
    text = skill.render(**{k: v for k, v in values.items() if k in skill.placeholders})
    guide = skill.reference_context()
    return f"{text}\n\n# 附属指南\n\n{guide}" if guide else text


def parse_script(data: dict, category: CategoryConfig, platform: PlatformConfig) -> tuple[list[dict], list[str]]:
    """把模型输出整理成合法的段落：段首括号搬进本段风格、停顿限制在 0~1.5 秒、过长的段落再切开、检查违禁词。"""
    raw = data.get("segments")
    if not isinstance(raw, list):
        raise VoiceError("模型输出缺少 segments 段落列表，请重新生成")
    out: list[dict] = []
    for item in raw:
        item = {"text": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        text, lead = split_leading_note(str(item.get("text") or ""))
        if not display_text(text) and not has_phonemes(text):
            continue
        control = compose_control(lead, str(item.get("control") or ""))
        try:
            pause = float(item.get("pause_after", 0.4))
        except (TypeError, ValueError):
            pause = 0.4
        pause = round(min(max(pause, 0.0), MAX_LLM_PAUSE), 2)
        pieces = split_script(text) if spoken_len(text) > SEGMENT_MAX_CHARS + 20 else [text]
        for k, piece in enumerate(pieces):
            out.append({"text": piece, "control": control, "pause_after": pause if k == len(pieces) - 1 else 0.3})
    if not out:
        raise VoiceError("模型没有写出可用的配音段落，请重新生成")
    warnings = []
    hits = find_violations(" ".join(s["text"] for s in out), category, platform)
    if hits:
        warnings.append(f"命中违禁词：{'、'.join(hits)}，请在表格里修改后再生成配音")
    return out, warnings


# ---------------------------------------------------------------- 音频工具（PCM16 单声道）


def wav_info(data: bytes) -> tuple[int, int, int, float]:
    """(声道数, 采样宽度, 采样率, 时长秒)；不是 wav 时报错。"""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError) as e:
        raise VoiceProviderError(f"配音后端返回的不是 wav 音频：{e}") from e


def _pcm(raw: bytes) -> array:
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def read_pcm(path: Path) -> array:
    """读出 48kHz 单声道 16-bit 采样；其他格式先用 ffmpeg 转换（在线程里调用）。"""
    with wave.open(str(path)) as w:
        fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        if fmt == (1, 2, SAMPLE_RATE):
            return _pcm(w.readframes(w.getnframes()))
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise VoiceError("缺少 ffmpeg：请运行 pip install imageio-ffmpeg")
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le",
                        "-"], capture_output=True, timeout=120, check=False)
    if r.returncode != 0:
        raise VoiceError(f"无法读取音频 {path.name}：{r.stderr.decode(errors='ignore')[-200:]}")
    return _pcm(r.stdout)


def write_pcm(path: Path, samples: array, rate: int = SAMPLE_RATE) -> None:
    data = array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def wav_stats(path: Path) -> tuple[float, int]:
    """(时长秒, 峰值)。"""
    with wave.open(str(path)) as w:
        rate, n, width = w.getframerate(), w.getnframes(), w.getsampwidth()
        frames = w.readframes(n)
    if width != 2:
        return n / rate, 32767
    samples = _pcm(frames)
    return n / rate, max(max(samples, default=0), -min(samples, default=0))


def trim_silence(samples: array, rate: int = SAMPLE_RATE, threshold: int = SILENCE_PEAK,
                 keep: float = SILENCE_KEEP_SECONDS) -> array:
    """去掉首尾低于阈值的静音（10ms 窗口），两端各留一点余量；整段都是静音时原样返回。"""
    win = max(1, rate // 100)
    n = len(samples)

    def loud(i: int) -> bool:
        chunk = samples[i: i + win]
        return bool(chunk) and max(max(chunk), -min(chunk)) > threshold

    start = 0
    while start < n and not loud(start):
        start += win
    if start >= n:
        return array("h", samples)
    end = n
    while end > start and not loud(max(start, end - win)):
        end -= win
    pad = int(rate * keep)
    return samples[max(0, start - pad): min(n, end + pad)]


def _silence(seconds: float, rate: int = SAMPLE_RATE) -> array:
    return array("h", bytes(int(rate * seconds) * 2))


def split_words(text: str, max_chars: int = SUBTITLE_WORDS_CHARS) -> list[str]:
    """外语字幕：按单词拼行，每行不超过 max_chars 个字符。"""
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


def subtitle_cues(texts: list[str], spans: list[tuple[float, float]], cjk: bool) -> list[Cue]:
    """每段的字幕在这一段的有声时间内按字数分配（时间单位：毫秒）。"""
    cues: list[Cue] = []
    for text, (a, b) in zip(texts, spans, strict=True):
        lines = split_subtitle(text) if cjk else split_words(text)
        total = sum(max(len(x), 1) for x in lines)
        t = a
        for line in lines:
            d = (b - a) * max(len(line), 1) / total
            cues.append(Cue(start=round(t * 1000), end=round((t + d) * 1000), text=line, kind="subtitle"))
            t += d
    return cues


def assemble(entries: list[tuple[VoiceSegment, VoiceTake]], trim: bool, folder: Path,
             cjk: bool) -> tuple[Path, list[tuple[float, float]], list[Path], float]:
    """按顺序拼接各段：去首尾静音 + 段后停顿。返回（原始拼接 wav、每段起止秒、每段单独的 wav、总时长）。同步执行。"""
    parts_dir = folder / "分段音频"
    parts_dir.mkdir(parents=True, exist_ok=True)
    out = _silence(HEAD_SILENCE_SECONDS)
    spans: list[tuple[float, float]] = []
    parts: list[Path] = []
    for i, (seg, take) in enumerate(entries, start=1):
        samples = read_pcm(take.file_path)
        if trim:
            samples = trim_silence(samples)
        start = len(out) / SAMPLE_RATE
        out.extend(samples)
        spans.append((start, len(out) / SAMPLE_RATE))
        label = safe_name(display_text(take.segment_text), 12) if cjk else f"segment_{i:02d}"
        part = parts_dir / f"{i:02d}_{label}.wav"
        write_pcm(part, samples)
        parts.append(part)
        if i < len(entries):
            out.extend(_silence(seg.pause_after))
    out.extend(_silence(TAIL_SILENCE_SECONDS))
    raw = folder / ".raw_mix.wav"
    write_pcm(raw, out)
    return raw, spans, parts, len(out) / SAMPLE_RATE


async def _run_ffmpeg(args: list[str], timeout: float = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "ffmpeg 超时"
    return proc.returncode, err.decode("utf-8", errors="ignore")


def _ms() -> str:
    return str(int(time.time() * 1000))


# ---------------------------------------------------------------- 引擎


class VoiceEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    @property
    def library(self) -> VoiceLibrary:
        return VoiceLibrary(self.rt.settings.workspace_dir)

    @staticmethod
    def studio(project: Project) -> VoiceStudio:
        if project.voice_studio is None:
            project.voice_studio = VoiceStudio()
        return project.voice_studio

    def save(self, project: Project) -> None:
        self.rt.store.save(project)

    def _dir(self, project: Project, sub: str) -> Path:
        return self.rt.store.asset_dir(project.project_id, "voices", sub)

    # ------------------------------------------------------------ Step 1：文案

    async def write_script(self, project: Project, *, skill_id: str, idea: str | None) -> ScriptResult:
        """执行 voice_writer Skill（真实 LLM 或演示），返回整理好的段落；由调用方决定是否写回。"""
        st = self.studio(project)
        cat, plat = self.rt.category(project.category_id), self.rt.platform(project.platform_id)
        info = {k: v for k, v in project.product_info.items() if v}
        skill = self.rt.skills.get("voice_writer", skill_id)
        if self.rt.is_live("llm"):
            prompt = compile_voice_writer(skill, st, cat, plat, info, idea)
            data = await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)
            mode = "真实 LLM"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo_voice_script(cat, plat, st.language, st.purpose, st.target_duration_seconds, idea, info)
            mode = "演示模式"
        segments, warnings = parse_script(data, cat, plat)
        return ScriptResult(segments=segments, title=str(data.get("title") or "").strip()[:20],
                            notes=str(data.get("notes") or "").strip(),
                            voice_description=compose_control(str(data.get("voice_description") or "")),
                            mode=mode, warnings=warnings)

    def apply_script(self, project: Project, result: ScriptResult, skill_id: str) -> None:
        st = self.studio(project)
        st.set_script(result.segments, title=result.title, notes=result.notes, skill_id=skill_id)
        # AI 建议的音色描述只在用户还没填写时采用，不覆盖用户的设定
        if result.voice_description and not st.source.description:
            st.set_source(description=result.voice_description)
        self.save(project)

    def split_pasted(self, project: Project, text: str) -> int:
        """把粘贴的整段口播稿按句切成段落（段首括号说明搬进本段风格）。"""
        pieces = split_script(text)
        if not pieces:
            raise VoiceError("请先粘贴配音文案")
        segments = []
        for piece in pieces:
            body, lead = split_leading_note(piece)
            if display_text(body) or has_phonemes(body):
                segments.append({"text": body, "control": lead, "pause_after": 0.4})
        st = self.studio(project)
        st.set_script(segments, title=st.title, notes="由粘贴的文案按句自动分段")
        self.save(project)
        return len(segments)

    def import_video_lines(self, project: Project) -> int:
        """把视频分镜剧本里的人物同期声台词导入成配音文案（每个有台词的分镜一段，按剧本顺序）。"""
        script = project.video_script
        if script is None:
            raise VoiceError("这个项目还没有视频分镜剧本：请先在【带货视频生成】选项卡生成剧本")
        segments = []
        for shot in script.shots:
            line = spoken_text(shot.character_dialogue)
            if line and (display_text(line) or has_phonemes(line)):
                segments.append({"text": line, "control": "", "pause_after": 0.4})
        if not segments:
            raise VoiceError("视频剧本里没有人物台词可以导入")
        st = self.studio(project)
        st.set_script(segments, title=st.title or "视频台词配音", notes="从视频分镜剧本的人物同期声台词导入")
        self.save(project)
        return len(segments)

    @staticmethod
    def shot_video_choices(project: Project) -> list[tuple[str, str]]:
        """可以拿来克隆音色的分镜视频（每个分镜的最新一版）：(显示名, 视频文件路径)。"""
        script = project.video_script
        if script is None:
            return []
        out = []
        for i, shot in enumerate(script.shots, start=1):
            vids = shot.playable_videos
            if vids:
                line = display_text(spoken_text(shot.character_dialogue))[:16] or "（无台词）"
                out.append((f"镜头 {i}：{line}", str(vids[-1].file_path)))
        return out

    async def normalize_preview(self, project: Project) -> list[tuple[int, str, str]] | None:
        """各段「数字 / 单位转读法」后的文字：[(段号, 原文, 读法)]，只列出有变化的段；后端不支持时返回 None。"""
        st = self.studio(project)
        texts = [tts_text(s.text) for s in st.segments]
        if not texts:
            raise VoiceError("还没有配音文案")
        result = await self.rt.voice_provider().normalize_preview(texts)
        if result is None:
            return None
        return [(i, a, b) for i, (a, b) in enumerate(zip(texts, result, strict=False), start=1) if a != b]

    # ------------------------------------------------------------ Step 2：音色

    async def prepare_reference(self, project: Project, src: Path, *, start: float = 0.0,
                                name: str = "") -> VoiceSource:
        """参考音频 / 视频 → 16kHz 单声道 wav，最长 30 秒（官方建议 5~30 秒），存进 voices/references/。"""
        ffmpeg = ffmpeg_exe()
        if not ffmpeg:
            raise VoiceError("缺少 ffmpeg：请运行 pip install imageio-ffmpeg")
        out = self._dir(project, "references") / f"ref_{_ms()}.wav"
        code, err = await _run_ffmpeg([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, float(start or 0)):.2f}",
            "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", f"{MAX_REFERENCE_SECONDS:g}",
            "-c:a", "pcm_s16le", str(out)])
        if code != 0 or not out.exists():
            out.unlink(missing_ok=True)
            raise VoiceError(f"无法读取这个文件里的声音：{err.strip()[-200:] or '没有音轨'}")
        seconds, peak = await asyncio.to_thread(wav_stats, out)
        if seconds < 1.0 or peak < 300:
            out.unlink(missing_ok=True)
            raise VoiceError("文件里没有读到人声（静音或不到 1 秒）：请换一段清晰的说话录音")
        st = self.studio(project)
        st.set_source(mode="clone", reference_path=out, reference_name=name or Path(src).name,
                      reference_seconds=round(seconds, 1), prompt_text="")
        self.save(project)
        return st.source

    def reference_tips(self, source: VoiceSource) -> list[str]:
        tips = []
        if source.reference_seconds and source.reference_seconds < MIN_REFERENCE_SECONDS:
            tips.append(f"参考音频只有 {source.reference_seconds:.1f} 秒，官方建议至少 5 秒，克隆效果可能变差")
        if source.reference_seconds >= MAX_REFERENCE_SECONDS - 0.05:
            tips.append(f"已截取前 {MAX_REFERENCE_SECONDS:g} 秒（官方建议 5~30 秒）")
        return tips

    def _reference_for_transcribe(self, source: VoiceSource) -> Path:
        if source.mode == "clone" and source.reference_path and source.reference_path.exists():
            return source.reference_path
        raise VoiceError("请先上传参考音频（或选择分镜视频的人声）")

    async def transcribe_reference(self, project: Project) -> AsyncIterator[VoiceEvent]:
        """识别参考音频的逐字文本（高保真克隆用）。识别结果写回音色设定，供用户核对修改。"""
        st = self.studio(project)
        path = self._reference_for_transcribe(st.source)
        provider = self.rt.voice_provider()
        yield VoiceEvent(f"🎧 正在识别参考音频里说的话（{provider.display_name}）…")
        text = ""
        async for ev in self._with_heartbeat(provider.transcribe(path.read_bytes()), "识别中", provider):
            if ev.final:
                text = str(ev.result or "").strip()
            else:
                yield ev
        if not text:
            raise VoiceError("没有识别出文字：参考音频里可能没有人声，请换一段，或手动填写逐字文本")
        st.set_source(prompt_text=text)
        self.save(project)
        yield VoiceEvent(f"✅ 识别完成：「{text}」——请核对错别字，高保真克隆要求与录音逐字一致",
                         final=True, result=text)

    def build_request(self, st: VoiceStudio, text: str, control: str, *, seed: int | None,
                      for_preview: bool = False) -> tuple[VoiceRequest, str]:
        """按当前音色设定组装一次合成请求。返回（请求, 给人看的音色说明）。"""
        src = st.source
        dialect = st.dialect_control
        common = {"text": tts_text(text), "cfg_value": st.cfg_value, "inference_timesteps": st.inference_timesteps,
                  "normalize": st.normalize, "seed": seed}
        if src.mode == "design":
            if not src.description:
                raise VoiceError("请先在 Step 2 填写音色描述（或选一个预设）")
            if for_preview or not src.design_locked:
                return (VoiceRequest(control=compose_control(dialect, src.description, control), **common),
                        f"声音设计「{src.description}」")
            ref, prompt, label, denoise = src.locked_sample_path, src.locked_sample_text, "已锁定的设计音色", False
        elif src.mode == "clone":
            if not (src.reference_path and src.reference_path.exists()):
                raise VoiceError("请先在 Step 2 上传参考音频（或选择分镜视频的人声）")
            ref, prompt, denoise = src.reference_path, src.prompt_text, src.denoise
            label = f"克隆「{src.reference_name}」"
        else:
            profile = self.library.get(src.voice_id)
            ref, prompt, label = profile.sample_path, profile.prompt_text, f"音色库「{profile.name}」"
            denoise = src.denoise and profile.kind == "clone"
        # 锁定的设计音色在可控克隆时仍带上原描述，保留描述里的语速、情绪；克隆 / 音色库用整体风格
        style = src.description if src.mode == "design" else src.style
        hifi = src.hifi and bool(prompt)
        req = VoiceRequest(control="" if hifi else compose_control(dialect, style, control), reference_path=ref,
                           prompt_text=prompt if hifi else "", denoise=denoise, **common)
        return req, label

    async def preview(self, project: Project, text: str | None = None) -> AsyncIterator[VoiceEvent]:
        """试听当前音色。声音设计每次试听都是一个新声音（种子递增），满意后再锁定。"""
        st = self.studio(project)
        text = (text or "").strip() or preview_text_for(st.language)
        provider = self.rt.voice_provider()
        seed = None if st.seed is None else st.seed + st.source.preview_count
        req, label = self.build_request(st, text, "", seed=seed, for_preview=True)
        yield VoiceEvent(f"🔊 试听（{req.mode_label}｜{label}）：「{display_text(text)[:30]}」")
        data = b""
        async for ev in self._with_heartbeat(provider.synthesize(req), "试听合成中", provider):
            if ev.final:
                data = ev.result
            else:
                yield ev
        path = self._dir(project, "previews") / f"preview_{_ms()}.wav"
        seconds = self._write_wav(path, data)
        src = st.source
        st.set_source(preview_path=path, preview_text=text, preview_mode=src.mode,
                      preview_description=src.description, preview_count=src.preview_count + 1)
        self.save(project)
        tip = ""
        if src.mode == "design":
            tip = "；满意就点「🔒 锁定这个音色」，之后每段都用它" if seconds >= MIN_REFERENCE_SECONDS else \
                "；样音不到 5 秒，锁定后克隆效果可能变差，建议换一句更长的试听文字"
        yield VoiceEvent(f"✅ 试听已生成：{seconds:.1f} 秒{tip}", audio_path=path, final=True, result=path)

    def lock_preview(self, project: Project) -> VoiceSource:
        """把最近一次声音设计的试听样音锁定为参考音频。"""
        st = self.studio(project)
        src = st.source
        if src.mode != "design":
            raise VoiceError("只有「声音设计」需要锁定音色：克隆和音色库本身就用固定的参考音频")
        if not (src.preview_path and src.preview_path.exists() and src.preview_mode == "design"):
            raise VoiceError("请先点「🔊 试听」生成一段样音")
        if src.preview_description != src.description:
            raise VoiceError("音色描述在试听之后改过了：请用当前描述重新试听一次再锁定")
        st.set_source(locked_sample_path=src.preview_path, locked_sample_text=tts_text(src.preview_text),
                      locked_description=src.description)
        self.save(project)
        return st.source

    def save_to_library(self, project: Project, name: str) -> VoiceProfile:
        """把当前音色（锁定的设计样音 / 克隆参考音频）存进音色库，所有项目都能用。"""
        st = self.studio(project)
        src = st.source
        if src.mode == "design":
            if src.design_locked:
                sample, text = src.locked_sample_path, src.locked_sample_text
            elif src.preview_mode == "design" and src.preview_path and src.preview_path.exists() \
                    and src.preview_description == src.description:
                sample, text = src.preview_path, tts_text(src.preview_text)
            else:
                raise VoiceError("请先用当前音色描述试听一段满意的声音")
            kind, desc = "design", src.description
        elif src.mode == "clone":
            sample, text = self._reference_for_transcribe(src), src.prompt_text
            kind, desc = "clone", src.style or src.reference_name
        else:
            raise VoiceError("当前用的已经是音色库里的音色")
        seconds, _ = wav_stats(sample)
        return self.library.add(name, kind, sample, prompt_text=text, description=desc, language=st.language,
                                sample_seconds=round(seconds, 1))

    def use_library_voice(self, project: Project, voice_id: str) -> VoiceProfile:
        profile = self.library.get(voice_id)
        st = self.studio(project)
        st.set_source(mode="library", voice_id=profile.voice_id)
        self.save(project)
        return profile

    # ------------------------------------------------------------ Step 3：逐段生成

    @staticmethod
    def targets(st: VoiceStudio, way: str, picked: list[str] | None) -> list[str]:
        if way == "pick":
            chosen = set(picked or [])
            if not chosen:
                raise VoiceError("请先勾选要生成的段落")
            return [s.segment_id for s in st.segments if s.segment_id in chosen]
        if way == "all":
            return [s.segment_id for s in st.segments]
        key = st.source.voice_key
        return [s.segment_id for s in st.segments if s.needs_render(key)]

    async def render(self, project: Project, way: str = "pending",
                     picked: list[str] | None = None) -> AsyncIterator[VoiceEvent]:
        """逐段合成（串行排队，与服务端单容器一致）。单段失败不影响后面的段落。"""
        st = self.studio(project)
        if not st.segments:
            raise VoiceError("请先在 Step 1 写好配音文案")
        provider = self.rt.voice_provider()
        yield VoiceEvent(f"🎙️ 配音后端：{provider.display_name}")
        if st.source.mode == "design" and not st.source.design_locked:
            yield VoiceEvent("🔒 声音设计还没有锁定音色：先生成一段样音并锁定，保证每段都是同一个声音…")
            async for ev in self.preview(project):
                yield ev if not ev.final else VoiceEvent(ev.message, audio_path=ev.audio_path)
            self.lock_preview(project)
            yield VoiceEvent("✅ 已自动锁定这段样音的音色（Step 2 可试听；不满意就重新试听并锁定，"
                             "再选「全部重新生成」）")
        ids = self.targets(st, way, picked)
        if not ids:
            yield VoiceEvent("✅ 所有段落都已是当前音色的最新配音；要重做请选「选段生成」或「全部重新生成」",
                             final=True)
            return
        key = st.source.voice_key
        first, label = self.build_request(st, st.get_segment(ids[0]).text, "", seed=None)
        yield VoiceEvent(f"开始生成 {len(ids)} 段（{first.mode_label}｜{label}）")
        if first.prompt_text and any(st.get_segment(i).control for i in ids):
            yield VoiceEvent("ℹ️ 高保真克隆会忽略「本段风格」；需要逐段调语气时，请在 Step 2 取消勾选「高保真克隆」")
        ok = 0
        for k, sid in enumerate(ids, start=1):
            try:
                i, seg = st.index_of(sid) + 1, st.get_segment(sid)
            except ValueError:
                yield VoiceEvent("⚠️ 有段落在生成过程中被删除，已跳过")
                continue
            req, label = self.build_request(st, seg.text, seg.control, seed=st.seed_for(seg))
            yield VoiceEvent(f"—— 第 {k}/{len(ids)} 个：第 {i} 段「{display_text(seg.text)[:24]}」"
                             + (f"｜风格：{req.control}" if req.control else ""))
            try:
                data = b""
                async for ev in self._with_heartbeat(provider.synthesize(req), f"第 {i} 段合成中", provider):
                    if ev.final:
                        data = ev.result
                    else:
                        yield ev
                path = self._dir(project, sid) / f"{sid}_{_ms()}.wav"
                seconds = self._write_wav(path, data)
            except (VoiceProviderError, VoiceError, OSError) as e:
                yield VoiceEvent(f"❌ 第 {i} 段失败：{e}", segment_id=sid, error=True)
                continue
            take = VoiceTake(file_path=path, duration_seconds=round(seconds, 2), provider=provider.provider_id,
                             voice_label=label, voice_key=key, hifi=bool(req.prompt_text), control=req.control,
                             seed=req.seed, segment_text=seg.text, segment_control=seg.control)
            try:
                st.add_take(sid, take)
            except ValueError:
                yield VoiceEvent(f"⚠️ 第 {i} 段在生成过程中被删除，这一版没有保存到段落里")
                continue
            self.save(project)
            ok += 1
            est = estimate_seconds(seg.text)
            odd = seconds > est * 2.2 + 1.5 or seconds < est * 0.4
            yield VoiceEvent(f"✅ 第 {i} 段完成：{seconds:.1f} 秒（预计 {est:.1f} 秒）"
                             + ("　⚠️ 时长和预计差很多，可能漏读或拖长，建议试听后重新生成这一段" if odd else ""),
                             segment_id=sid, audio_path=path)
        yield VoiceEvent(f"🎉 本轮完成：成功 {ok}/{len(ids)} 段", final=True, result=ok)

    # ------------------------------------------------------------ Step 4：合成完整配音

    async def mix(self, project: Project) -> AsyncIterator[VoiceEvent]:
        """拼接所有段落的当前版本 → 响度统一的 wav + mp3 + srt 字幕 + 打包 zip。每次都新建一份成品。"""
        st = self.studio(project)
        if not st.segments:
            raise VoiceError("还没有配音文案")
        missing = st.missing_segments()
        if missing:
            raise VoiceError(f"第 {'、'.join(map(str, missing))} 段还没有配音：请先在 Step 3 生成")
        ffmpeg = ffmpeg_exe()
        if not ffmpeg:
            raise VoiceError("缺少 ffmpeg：请运行 pip install imageio-ffmpeg")
        key = st.source.voice_key
        stale = st.stale_segments()
        other = [i for i, s in enumerate(st.segments, start=1) if s.active_take and s.active_take.voice_key != key]
        if stale:
            yield VoiceEvent(f"⚠️ 第 {'、'.join(map(str, stale))} 段生成后又改过文案，"
                             "本次仍使用旧版配音（字幕按旧文案）")
        if other:
            yield VoiceEvent(f"⚠️ 第 {'、'.join(map(str, other))} 段是用之前的音色生成的，"
                             "整条配音可能听起来不是同一个人")
        entries = [(s, s.active_take) for s in st.segments]
        mix_id = new_id("mix")
        folder = self._dir(project, mix_id)
        base = safe_name(st.title or project.name, 20)
        trim_note = "去掉首尾静音，" if st.trim_silence else ""
        yield VoiceEvent(f"🎧 正在拼接 {len(entries)} 段（{trim_note}按段后停顿衔接）…")
        raw, spans, parts, total = await asyncio.to_thread(assemble, entries, st.trim_silence, folder, st.is_cjk)
        wav_path, mp3_path = folder / f"配音_{base}.wav", folder / f"配音_{base}.mp3"
        yield VoiceEvent("🔊 统一响度到 -16 LUFS，并导出 mp3…")
        code, err = await _run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
                                       "-af", LOUDNORM, "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le",
                                       str(wav_path)])
        if code != 0:
            raise VoiceError(f"响度统一失败：{err.strip()[-300:]}")
        code, err = await _run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_path),
                                       "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_path)])
        mp3 = mp3_path if code == 0 and mp3_path.exists() else None
        if mp3 is None:
            yield VoiceEvent(f"⚠️ mp3 导出失败（wav 不受影响）：{err.strip()[-200:]}")
        raw.unlink(missing_ok=True)
        texts = [display_text(take.segment_text) for _, take in entries]
        srt_path = folder / f"配音字幕_{base}.srt"
        # utf-8-sig：带 BOM，Windows 上的剪辑软件识别中文更稳
        srt_path.write_text(build_srt(subtitle_cues(texts, spans, st.is_cjk), 1000), encoding="utf-8-sig")
        script_path = folder / "配音文案.txt"
        script_path.write_text(self._script_text(st, entries), encoding="utf-8-sig")
        zip_path = folder / f"配音_{base}.zip"
        await asyncio.to_thread(self._zip, zip_path, [wav_path, mp3, srt_path, script_path], parts)
        mix = VoiceMix(mix_id=mix_id, wav_path=wav_path, mp3_path=mp3, srt_path=srt_path, zip_path=zip_path,
                       duration_seconds=round(total, 2), segment_count=len(entries))
        st.add_mix(mix)
        self.save(project)
        yield VoiceEvent(f"🎉 完整配音已生成：{total:.1f} 秒，{len(entries)} 段（第 {len(st.mixes)} 版成品）",
                         audio_path=wav_path, final=True, result=mix)

    # ------------------------------------------------------------ 内部工具

    @staticmethod
    def _write_wav(path: Path, data: bytes) -> float:
        _, _, _, seconds = wav_info(data)
        if seconds <= 0.05:
            raise VoiceProviderError("配音后端返回了空音频")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return seconds

    @staticmethod
    async def _with_heartbeat(coro: Awaitable, label: str, provider: VoiceProvider) -> AsyncIterator[VoiceEvent]:
        """等待后端返回的同时每隔一会儿报一次进度（Modal 冷启动约 2 分钟）；最后一条事件携带结果。"""
        task = asyncio.ensure_future(coro)
        start = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
                if done:
                    break
                hint = f"（{provider.cold_start_hint}）" if provider.cold_start_hint else ""
                yield VoiceEvent(f"{label}…已等待 {time.monotonic() - start:.0f} 秒{hint}")
            yield VoiceEvent("", final=True, result=task.result())
        finally:
            if not task.done():
                task.cancel()

    @staticmethod
    def _script_text(st: VoiceStudio, entries: list[tuple[VoiceSegment, VoiceTake]]) -> str:
        lines = [f"标题：{st.title or '（未命名）'}", f"语言：{st.language_label}　用途：{PURPOSES[st.purpose]}", ""]
        for i, (seg, take) in enumerate(entries, start=1):
            extra = f"｜风格：{take.control}" if take.control else ""
            lines.append(f"第 {i} 段（{take.duration_seconds:.1f} 秒，段后停顿 {seg.pause_after:g} 秒{extra}）")
            lines.append(display_text(take.segment_text))
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _zip(zip_path: Path, files: list[Path | None], parts: list[Path]) -> None:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                if f and f.exists():
                    z.write(f, f.name)
            for p in parts:
                z.write(p, f"分段音频/{p.name}")


