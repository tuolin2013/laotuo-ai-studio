"""【配音】选项卡数据模型：配音文案分段、音色来源、逐段生成的版本与完整配音成品（VoxCPM2）。

写法规则依据 VoxCPM2 官方最佳实践（https://voxcpm.readthedocs.io/zh-cn/latest/cookbook.html）：
- 风格 / 音色指令写在正文前的括号里（服务端拼成 "(指令)正文"），所以指令本身不能再带括号；
- 非语言标签用推荐的英文方括号标签，如 [laughing]、[sigh]，点到为止；
- 发音细调用带声调数字的拼音，如 {chong2}；
- 长文本切成短段分别合成再拼接，比一次合成整段更稳；极短的句子（不到 1 秒）听起来会发虚；
- 不提供参考音频时每次都会随机一个声音，多段配音要音色一致，就得固定一段参考音频（锁定试听音色 / 克隆 / 音色库）。

本项目约定的多音字写法：在字后面紧跟拼音，如「重{chong2}新」。发给模型时把这个字换成 {chong2}，字幕仍显示原字。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import new_id, utc_now

VoiceMode = Literal["design", "clone", "library"]

# 语言 / 方言：code -> (界面名称, 自动加在风格指令前的方言名)。官方建议方言指令只写方言名，越简单越好；
# 外语不需要语言标签，直接用目标语言写正文即可
LANGUAGES: dict[str, tuple[str, str]] = {
    "mandarin": ("普通话", ""),
    "cantonese": ("粤语", "广东话"),
    "sichuan": ("四川话", "四川话"),
    "dongbei": ("东北话", "东北话"),
    "henan": ("河南话", "河南话"),
    "shaanxi": ("陕西话", "陕西话"),
    "shandong": ("山东话", "山东话"),
    "tianjin": ("天津话", "天津话"),
    "minnan": ("闽南话", "闽南话"),
    "wu": ("吴语（上海话）", "上海话"),
    "english": ("English 英语", ""),
    "japanese": ("日本語 日语", ""),
    "korean": ("한국어 韩语", ""),
    "thai": ("ไทย 泰语", ""),
    "vietnamese": ("Tiếng Việt 越南语", ""),
    "indonesian": ("Bahasa Indonesia 印尼语", ""),
    "malay": ("Bahasa Melayu 马来语", ""),
    "spanish": ("Español 西班牙语", ""),
}
# 用汉字书写的语言：字幕按 14 字一行切分
CJK_LANGUAGES = frozenset({"mandarin", "cantonese", "sichuan", "dongbei", "henan", "shaanxi", "shandong", "tianjin",
                           "minnan", "wu", "japanese"})

PURPOSES: dict[str, str] = {
    "live_selling": "带货口播（直播间 / 短视频促单）",
    "seeding": "种草分享（真实体验口吻）",
    "explainer": "产品讲解旁白（专业、清晰）",
    "brand_story": "品牌故事（温暖、有画面感）",
    "promo": "活动促销播报（节奏快、记得住）",
}

# 声音设计预设：按官方建议把「身份设定 + 声音质感 + 表达状态」揉进一条描述
VOICE_PRESETS: dict[str, str] = {
    "热情女主播": "年轻女主播，声音明亮甜美，语速偏快，热情有感染力，像在直播间和粉丝聊天",
    "亲切大叔": "中年男性，声音浑厚淳朴，语速适中，亲切踏实，像邻家大叔推荐自家好货",
    "专业讲解": "三十岁左右的男讲解员，吐字清晰，音色干净有磁性，语气专业可信，节奏平稳",
    "温柔种草": "二十多岁的女生，声音温柔轻快，略带笑意，像和闺蜜分享好物",
    "品牌旁白": "成熟女性旁白，声音低沉温暖，语速舒缓，富有画面感和故事感",
    "促销播报": "热情洋溢的男主播，声音高亢响亮，节奏紧凑，带着限时抢购的紧迫感",
    "慈祥长辈": "七十岁左右的老奶奶，声音略带沙哑，慢条斯理，慈祥温和",
}

# 试听 / 锁定音色用的样句：8~10 秒、不含数字（锁定后它就是参考文本，数字读法开关改了也对得上）
PREVIEW_TEXTS: dict[str, str] = {
    "mandarin": "大家好，欢迎来到直播间。今天给大家带来一款好东西，用过的朋友都说好，我们一起来看看吧。",
    "cantonese": "大家好，欢迎嚟到直播间。今日同大家介绍一样好嘢，用过嘅朋友都话好，一齐睇下啦。",
    "sichuan": "各位老铁，欢迎来到直播间哈。今天给大家带个巴适得板的好东西，用过的都说要得，一起来看一哈嘛。",
    "dongbei": "老铁们，欢迎来到直播间。今儿个给大伙儿整个好东西，用过的都说贼拉好，咱一块儿瞅瞅。",
    "english": "Hi everyone, welcome to the live stream. Today I have something really special to show you. "
               "Everyone who has tried it loves it, so let's take a look together.",
    "japanese": "皆さん、こんにちは。今日はとっておきの商品をご紹介します。"
                "使った方はみんな気に入っています。一緒に見ていきましょう。",
    "korean": "여러분 안녕하세요. 오늘은 정말 좋은 제품을 소개해 드릴게요. "
              "써 보신 분들 모두 만족하셨어요. 함께 보시죠.",
}


def preview_text_for(language: str) -> str:
    """该语言的默认试听样句：没有专门样句的方言用普通话，外语用英语。"""
    if language in PREVIEW_TEXTS:
        return PREVIEW_TEXTS[language]
    return PREVIEW_TEXTS["mandarin" if language in CJK_LANGUAGES else "english"]


# 官方推荐的非语言标签（全小写的 [laughing] 比 [Laughter] 这类变体更稳定）
RECOMMENDED_TAGS: tuple[str, ...] = (
    "[laughing]", "[sigh]", "[Uhm]", "[Shh]", "[Question-ah]", "[Question-ei]", "[Question-en]", "[Question-oh]",
    "[Surprise-wa]", "[Surprise-yo]", "[Dissatisfaction-hnn]",
)

# 段后停顿上限（秒）与一段文案的建议字数
MAX_PAUSE_SECONDS = 5.0
SEGMENT_SOFT_CHARS = 24
SEGMENT_MAX_CHARS = 40
SEGMENT_MIN_CHARS = 8
# 参考音频的建议时长（官方：5–30 秒较实用）
MIN_REFERENCE_SECONDS = 5.0
MAX_REFERENCE_SECONDS = 30.0

_CJK = "㐀-䶿一-鿿"
_CJK_CHAR = re.compile(f"[{_CJK}]")
# 「重{chong2}」：字后紧跟带声调数字的拼音，表示这个字按该拼音读
_ANNOTATED = re.compile(rf"([{_CJK}])\{{([A-Za-z]+[1-5])\}}")
_PHONEME = re.compile(r"\{[^{}\n]+\}")
_TAG = re.compile(r"\[([A-Za-z][A-Za-z\-]*)\]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_DIGIT = re.compile(r"\d")
_SPACES = re.compile(r"\s+")
# 句末：中文标点直接切；英文 . ! ? 后面跟空格才切（保留 3.5、U.S. 里的点）
_SENTENCE_END = re.compile(r"(?<=[。！？；…\n])|(?<=[.!?;])(?=\s)")
_CLAUSE_END = re.compile(r"(?<=[，,、：:])")
_CONTROL_STRIP = re.compile(r"[()（）\[\]【】{}]")
# 文案里的括号舞台说明会被直接读出来（如「（笑）」）
_STAGE_NOTE = re.compile(r"[（(][^()（）]{1,20}[)）]")
# 段首的括号：模型会把它当成风格指令，搬到「本段风格」里
_LEADING_NOTE = re.compile(r"^\s*[（(]([^()（）]{1,40})[)）]\s*")
MAX_CONTROL_CHARS = 120


# ---------------------------------------------------------------- 文本工具


def tts_text(text: str) -> str:
    """发给模型的正文：「重{chong2}」→「{chong2}」，其余原样保留（含非语言标签与拼音）。"""
    return _ANNOTATED.sub(lambda m: "{" + m.group(2).lower() + "}", (text or "").strip())


def display_text(text: str) -> str:
    """给人看的文字（字幕、参考文本）：去掉非语言标签和拼音，多音字标注保留原字。"""
    out = _TAG.sub("", text or "")
    out = _ANNOTATED.sub(r"\1", out)
    out = _PHONEME.sub("", out)
    return _SPACES.sub(" ", out).strip()


def has_phonemes(text: str) -> bool:
    """正文里有 {拼音} / {音素}：官方要求这种输入关闭文本规范化，否则拼音里的数字可能被改写。"""
    return bool(_PHONEME.search(tts_text(text)))


def unknown_tags(text: str) -> list[str]:
    """不在官方推荐列表里的方括号标签（大小写也要一致）。"""
    return list(dict.fromkeys(f"[{t}]" for t in _TAG.findall(text or "") if f"[{t}]" not in RECOMMENDED_TAGS))


def spoken_len(text: str) -> int:
    """朗读长度，折算成汉字：汉字算 1；数字展开后偏长，算 1.5；外文单词按语速（2.6 词 / 秒 vs 4.5 字 / 秒）算 1.7。
    分段与「太短 / 太长」提醒都按它判断，中外文用同一套阈值。"""
    shown = display_text(text)
    return round(len(_CJK_CHAR.findall(shown)) + 1.5 * len(_DIGIT.findall(shown))
                 + 1.7 * len(_LATIN_WORD.findall(shown)))


def estimate_seconds(text: str) -> float:
    """按中速口播估算时长：汉字 4.5 字 / 秒，外文 2.6 词 / 秒，数字展开后偏长，每个非语言标签约 0.4 秒。"""
    shown = display_text(text)
    seconds = (len(_CJK_CHAR.findall(shown)) / 4.5 + len(_LATIN_WORD.findall(shown)) / 2.6
               + len(_DIGIT.findall(shown)) / 3.0 + len(_TAG.findall(text or "")) * 0.4)
    return round(max(seconds, 0.5), 1)


def clean_control(text: str | None) -> str:
    """风格指令去掉括号（括号会破坏 "(指令)正文" 格式），合并空白，限制长度。"""
    out = _SPACES.sub(" ", _CONTROL_STRIP.sub("", text or "")).strip(" ，,。")
    return out[:MAX_CONTROL_CHARS]


def compose_control(*parts: str | None) -> str:
    """把方言名、音色描述、本段风格拼成一条指令（去重、去空），如「广东话，中年男性，语速稍快」。"""
    items: list[str] = []
    for p in parts:
        c = clean_control(p)
        if c and not any(c in x for x in items):
            items = [x for x in items if x not in c]
            items.append(c)
    return clean_control("，".join(items))


def split_leading_note(text: str) -> tuple[str, str]:
    """把段首括号里的说明拆出来：「(语速放慢)今天…」→ ("今天…", "语速放慢")。"""
    m = _LEADING_NOTE.match(text or "")
    if not m:
        return (text or "").strip(), ""
    return text[m.end():].strip(), clean_control(m.group(1))


def _hard_split(piece: str, max_chars: int) -> list[str]:
    """超长的句子先按逗号拆，仍然过长（没有标点）时按字数硬切。"""
    if spoken_len(piece) <= max_chars:
        return [piece]
    out, cur = [], ""
    for clause in (c for c in _CLAUSE_END.split(piece) if c):
        if cur and spoken_len(cur + clause) > max_chars:
            out.append(cur)
            cur = ""
        cur += clause
    if cur:
        out.append(cur)
    final = []
    for part in out:
        if spoken_len(part) <= max_chars:
            final.append(part)
        elif " " in part.strip():
            # 外文：在单词之间切，不把单词切断
            line = ""
            for word in part.split(" "):
                if line and spoken_len(f"{line} {word}") > max_chars:
                    final.append(line)
                    line = word
                else:
                    line = f"{line} {word}".strip()
            if line:
                final.append(line)
        else:
            while spoken_len(part) > max_chars and len(part) > max_chars:
                final.append(part[:max_chars])
                part = part[max_chars:]
            final.append(part)
    return final


def split_script(text: str, max_chars: int = SEGMENT_MAX_CHARS) -> list[str]:
    """把整段口播稿切成适合逐段合成的段落：按句切开，再把太短的句子与相邻句合并（每段约 8~40 字）。"""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split((text or "").replace("\r\n", "\n")):
        sentence = sentence.strip()
        if sentence:
            pieces.extend(p.strip() for p in _hard_split(sentence, max_chars) if p.strip())
    segments: list[str] = []
    cur = ""
    for piece in pieces:
        # 一句一段；只有前一段太短（不到 8 字）或两句加起来仍很短时才合并
        n = spoken_len(cur)
        if cur and n >= SEGMENT_MIN_CHARS and n + spoken_len(piece) > SEGMENT_SOFT_CHARS:
            segments.append(cur)
            cur = ""
        cur = _join(cur, piece)
    if cur:
        if segments and spoken_len(cur) < SEGMENT_MIN_CHARS:
            segments[-1] = _join(segments[-1], cur)
        else:
            segments.append(cur)
    return segments


def _join(a: str, b: str) -> str:
    """拼接两句：外文句子之间补一个空格，中文直接相连。"""
    if a and b and a[-1].isascii() and b[0].isascii() and not a[-1].isspace():
        return f"{a} {b}"
    return a + b


def segment_warnings(text: str) -> list[str]:
    """单段文案的写法提醒（不阻止生成）。"""
    tips = []
    bad = unknown_tags(text)
    if bad:
        tips.append(f"标签 {'、'.join(bad)} 不在官方推荐列表里，可能被直接读出来")
    if len(_TAG.findall(text or "")) > 2:
        tips.append("非语言标签太多，一段最多 1~2 个")
    if _STAGE_NOTE.search(text or ""):
        tips.append("括号里的说明会被读出来：语气请写到「本段风格」，笑声叹气用 [laughing] / [sigh]")
    n = spoken_len(text)
    if 0 < n < 4:
        tips.append("太短（不到 1 秒）听起来会发虚，建议与相邻段合并")
    if n > SEGMENT_MAX_CHARS + 20:
        tips.append("偏长，建议拆成两段：长段更容易越读越快或发糊")
    return tips


# ---------------------------------------------------------------- 数据模型


class VoiceTake(BaseModel):
    """一段文案的一次生成结果。重复生成时追加，绝不覆盖。"""

    take_id: str = Field(default_factory=lambda: new_id("take"))
    file_path: Path
    duration_seconds: float = 0.0
    provider: str = "demo_voice"
    voice_label: str = ""
    # 生成时所用音色的标识（VoiceSource.voice_key）：之后换了音色就提示重新生成
    voice_key: str = ""
    hifi: bool = False
    # 实际发给模型的风格指令（含方言名、音色描述）
    control: str = ""
    seed: int | None = None
    # 生成时这一段的文案与风格：之后被修改过就提示重新生成；字幕也按生成时的文案出
    segment_text: str = ""
    segment_control: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class VoiceSegment(BaseModel):
    segment_id: str = Field(default_factory=lambda: new_id("seg"))
    text: str
    # 本段风格指令（选填），如「语速稍快，热情」
    control: str = ""
    # 拼接完整配音时，这一段之后的停顿（秒）
    pause_after: float = Field(default=0.4, ge=0, le=MAX_PAUSE_SECONDS)
    takes: list[VoiceTake] = Field(default_factory=list)
    # 用于完整配音的版本；为空时用最新一版
    active_take_id: str | None = None

    @field_validator("text")
    @classmethod
    def _check_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not display_text(v) and not has_phonemes(v):
            raise ValueError("配音文案不能为空")
        return v

    @field_validator("control")
    @classmethod
    def _check_control(cls, v: str) -> str:
        return clean_control(v)

    @property
    def playable_takes(self) -> list[VoiceTake]:
        return [t for t in self.takes if t.file_path.exists()]

    @property
    def active_take(self) -> VoiceTake | None:
        takes = self.playable_takes
        for t in takes:
            if t.take_id == self.active_take_id:
                return t
        return takes[-1] if takes else None

    @property
    def is_stale(self) -> bool:
        """当前使用的版本生成后，文案或风格指令又被修改过。"""
        t = self.active_take
        return t is not None and (t.segment_text != self.text or t.segment_control != self.control)

    def needs_render(self, voice_key: str) -> bool:
        """「逐段生成」是否要处理这一段：还没有配音、文案改过，或换了音色。"""
        t = self.active_take
        return t is None or self.is_stale or t.voice_key != voice_key

    def status_label(self, voice_key: str) -> str:
        t = self.active_take
        if t is None:
            return "⚪ 未生成"
        if self.is_stale:
            return "✏️ 文案已改，需重新生成"
        if t.voice_key != voice_key:
            return "🔄 音色已更换"
        n = len(self.playable_takes)
        return f"✅ {t.duration_seconds:.1f} 秒" + (f"（共 {n} 版）" if n > 1 else "")


class VoiceSource(BaseModel):
    """当前选用的音色。"""

    mode: VoiceMode = "design"
    # 声音设计：音色描述
    description: str = ""
    # 声音设计：锁定的试听样音及其文本。锁定后各段都以它为参考音频，音色前后一致；
    # 改了音色描述就视为解锁（locked_description 记录锁定时的描述）
    locked_sample_path: Path | None = None
    locked_sample_text: str = ""
    locked_description: str = ""
    # 克隆：已转成 16kHz 单声道的参考音频片段（≤30 秒）
    reference_path: Path | None = None
    reference_name: str = ""
    reference_seconds: float = 0.0
    # 参考音频的逐字文本：高保真克隆用（识别结果可手动改错字）
    prompt_text: str = ""
    # 音色库
    voice_id: str | None = None
    # 克隆 / 音色库：整体风格（选填），如「语速稍快，热情」。官方提示：克隆只能调情绪、语速，不能改变说话人身份
    style: str = ""
    # 高保真克隆：参考音频 + 逐字文本续写，音色最像，但风格指令不生效
    hifi: bool = True
    # 克隆前给参考音频降噪（参考音频有背景音乐 / 噪声时打开）
    denoise: bool = False
    # 最近一次试听
    preview_path: Path | None = None
    preview_text: str = ""
    preview_mode: str = ""
    preview_description: str = ""
    preview_count: int = 0

    @field_validator("description", "locked_description", "preview_description", "style")
    @classmethod
    def _check_description(cls, v: str) -> str:
        return clean_control(v)

    @field_validator("prompt_text", "locked_sample_text")
    @classmethod
    def _check_prompt(cls, v: str) -> str:
        return _SPACES.sub(" ", v or "").strip()

    @property
    def design_locked(self) -> bool:
        return (self.locked_sample_path is not None and self.locked_sample_path.exists()
                and self.locked_description == self.description)

    @property
    def voice_key(self) -> str:
        """当前音色的标识：用来判断已生成的段落是不是同一个声音。"""
        if self.mode == "design":
            if self.design_locked:
                return f"design-lock:{self.locked_sample_path.name}|hifi={self.hifi}"
            return f"design:{self.description}"
        if self.mode == "clone":
            name = self.reference_path.name if self.reference_path else ""
            return f"clone:{name}|hifi={self.hifi}|denoise={self.denoise}"
        return f"library:{self.voice_id}|hifi={self.hifi}"


class VoiceMix(BaseModel):
    """一次「合成完整配音」的成品。每次合成都新建一份，不覆盖旧成品。"""

    mix_id: str = Field(default_factory=lambda: new_id("mix"))
    wav_path: Path
    mp3_path: Path | None = None
    srt_path: Path | None = None
    zip_path: Path | None = None
    duration_seconds: float = 0.0
    segment_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def files(self) -> list[Path]:
        return [p for p in (self.wav_path, self.mp3_path, self.srt_path, self.zip_path) if p and p.exists()]


class VoiceProfile(BaseModel):
    """音色库中的一个音色：一段样音 + 它的逐字文本。所有项目共用，保证同一品牌的配音音色一致。"""

    voice_id: str = Field(default_factory=lambda: new_id("voice"))
    name: str
    kind: Literal["design", "clone"]
    description: str = ""
    sample_path: Path
    prompt_text: str = ""
    language: str = "mandarin"
    sample_seconds: float = 0.0
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        v = _SPACES.sub(" ", v or "").strip()
        if not v:
            raise ValueError("音色名称不能为空")
        return v[:40]

    @property
    def kind_label(self) -> str:
        return "声音设计" if self.kind == "design" else "声音克隆"


_PARAM_FIELDS = ("cfg_value", "inference_timesteps", "normalize", "seed", "trim_silence")
_META_FIELDS = ("language", "purpose", "target_duration_seconds")


class VoiceStudio(BaseModel):
    """一个项目的配音工作区。"""

    language: str = "mandarin"
    purpose: str = "live_selling"
    target_duration_seconds: int = Field(default=30, ge=5, le=300)
    title: str = ""
    notes: str = ""
    writer_skill_id: str = ""
    segments: list[VoiceSegment] = Field(default_factory=list)
    source: VoiceSource = Field(default_factory=VoiceSource)
    # 合成参数（官方默认 cfg 2.0、10 步；长音频发糊时把 cfg 降到 1.5~1.6）
    cfg_value: float = Field(default=2.0, ge=1.0, le=3.0)
    inference_timesteps: int = Field(default=10, ge=4, le=30)
    # 文本规范化：把数字、日期、单位转成读法（如 99元 → 九十九元）
    normalize: bool = True
    # 固定种子：同一配置结果可复现；None = 每次随机。重新生成同一段时自动换种子
    seed: int | None = 42
    # 拼接完整配音时去掉每段首尾静音，停顿完全由「段后停顿」控制
    trim_silence: bool = True
    mixes: list[VoiceMix] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("language")
    @classmethod
    def _check_language(cls, v: str) -> str:
        return v if v in LANGUAGES else "mandarin"

    @field_validator("purpose")
    @classmethod
    def _check_purpose(cls, v: str) -> str:
        return v if v in PURPOSES else "live_selling"

    # ---------- 查询

    @property
    def language_label(self) -> str:
        return LANGUAGES[self.language][0]

    @property
    def dialect_control(self) -> str:
        return LANGUAGES[self.language][1]

    @property
    def is_cjk(self) -> bool:
        return self.language in CJK_LANGUAGES

    @property
    def total_estimated_seconds(self) -> float:
        if not self.segments:
            return 0.0
        speech = sum(estimate_seconds(s.text) for s in self.segments)
        return round(speech + sum(s.pause_after for s in self.segments[:-1]), 1)

    @property
    def latest_mix(self) -> VoiceMix | None:
        mixes = [m for m in self.mixes if m.wav_path.exists()]
        return mixes[-1] if mixes else None

    def index_of(self, segment_id: str) -> int:
        for i, s in enumerate(self.segments):
            if s.segment_id == segment_id:
                return i
        raise ValueError(f"找不到这一段配音文案：{segment_id}")

    def get_segment(self, segment_id: str) -> VoiceSegment:
        return self.segments[self.index_of(segment_id)]

    def missing_segments(self) -> list[int]:
        """还没有可用配音的段落序号（从 1 开始）。"""
        return [i for i, s in enumerate(self.segments, start=1) if s.active_take is None]

    def stale_segments(self) -> list[int]:
        """生成后又改过文案 / 风格的段落序号。"""
        return [i for i, s in enumerate(self.segments, start=1) if s.is_stale]

    def seed_for(self, segment: VoiceSegment) -> int | None:
        """第 1 次生成用固定种子；同一段再次生成时自动换一个种子，否则会得到一模一样的结果。"""
        return None if self.seed is None else self.seed + len(segment.takes)

    # ---------- 修改（所有状态变更都走这里，便于统一校验）

    def _touch(self) -> None:
        self.updated_at = utc_now()

    def _set_checked(self, allowed: tuple[str, ...], fields: dict) -> None:
        bad = set(fields) - set(allowed)
        if bad:
            raise ValueError(f"未知字段：{sorted(bad)}")
        checked = VoiceStudio(**fields)
        for k in fields:
            setattr(self, k, getattr(checked, k))
        self._touch()

    def set_meta(self, **fields) -> None:
        self._set_checked(_META_FIELDS, fields)

    def set_params(self, **fields) -> None:
        self._set_checked(_PARAM_FIELDS, fields)

    def set_script(self, segments: list[dict], *, title: str = "", notes: str = "", skill_id: str = "") -> None:
        """整体替换配音文案（AI 撰写或按句分段的结果）。旧段落的生成记录随之清空，音频文件仍保留在磁盘上。"""
        new = [VoiceSegment(**{k: v for k, v in s.items() if k in ("text", "control", "pause_after")})
               for s in segments]
        if not new:
            raise ValueError("配音文案为空：至少需要一段")
        self.segments = new
        self.title, self.notes = (title or "").strip(), (notes or "").strip()
        if skill_id:
            self.writer_skill_id = skill_id
        self._touch()

    def update_segment(self, segment_id: str, **fields) -> VoiceSegment:
        bad = set(fields) - {"text", "control", "pause_after"}
        if bad:
            raise ValueError(f"不能修改的字段：{sorted(bad)}")
        i = self.index_of(segment_id)
        self.segments[i] = VoiceSegment.model_validate({**self.segments[i].model_dump(), **fields})
        self._touch()
        return self.segments[i]

    def add_segment(self, after_id: str | None = None, text: str = "请填写配音文案") -> VoiceSegment:
        seg = VoiceSegment(text=text)
        pos = self.index_of(after_id) + 1 if after_id else len(self.segments)
        self.segments.insert(pos, seg)
        self._touch()
        return seg

    def delete_segment(self, segment_id: str) -> None:
        if len(self.segments) <= 1:
            raise ValueError("至少保留一段配音文案")
        self.segments.pop(self.index_of(segment_id))
        self._touch()

    def move_segment(self, segment_id: str, delta: int) -> None:
        i = self.index_of(segment_id)
        j = i + delta
        if not 0 <= j < len(self.segments):
            raise ValueError("已经到头了，不能再移动")
        self.segments[i], self.segments[j] = self.segments[j], self.segments[i]
        self._touch()

    def add_take(self, segment_id: str, take: VoiceTake) -> None:
        """新版本追加到末尾并设为当前使用的版本。"""
        seg = self.get_segment(segment_id)
        seg.takes.append(take)
        seg.active_take_id = take.take_id
        self._touch()

    def choose_take(self, segment_id: str, take_id: str) -> None:
        seg = self.get_segment(segment_id)
        if take_id not in {t.take_id for t in seg.playable_takes}:
            raise ValueError("这个版本的音频文件已不存在")
        seg.active_take_id = take_id
        self._touch()

    def set_source(self, **fields) -> VoiceSource:
        self.source = VoiceSource.model_validate({**self.source.model_dump(), **fields})
        self._touch()
        return self.source

    def add_mix(self, mix: VoiceMix) -> None:
        self.mixes.append(mix)
        self._touch()
