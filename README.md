# laotuo-ai-studio · 电商 AIGC 内容工厂

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Gradio](https://img.shields.io/badge/gradio-6.30.0-orange)
![Lint](https://img.shields.io/badge/lint-ruff-261230)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)

面向电商运营的一体化 AIGC 内容生产工具。选好类目和平台、填写商品信息后，在一个 Web 界面里完成三类素材：

- 主图：按平台建议张数策划，逐槽位并发出图、勾选定稿、打包导出。
- 详情页：按屏规划，生成长图序列预览。
- 带货短视频：基于 MiniMax H3 原生音画一体生成，画面和声音一次产出，用「首帧锁定」防止商品变形。

全部 AI 能力由 `config/skills/` 下的 Markdown「卡带」驱动，代码里不写死提示词。没有 API Key 也能完整跑通流程（零 Key 演示模式）。

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [使用教程](#使用教程)
- [系统架构](#系统架构)
- [插卡式 Skill 系统](#插卡式-skill-系统)
- [扩展开发](#扩展开发)
- [开发与测试](#开发与测试)
- [安全须知](#安全须知)
- [贡献指南](#贡献指南)
- [许可证](#许可证)
- [致谢](#致谢)

## 功能特性

- 三步式策划工作流：填写想法（选填）→ AI 生成草稿 → 人工编辑并确认。确认后才能传图和渲染。
- 生图模式自动判定：按参考图数量在文生图（0 张）、单图保真编辑（1 张）、多图融合（2~3 张）之间路由。
- 参考图按角色登记：同一角色（如 `product_package`）上传一次，自动绑定到所有需要它的槽位。
- 视觉设计体系：5 大美学流派 × 6 大构图模式，可整套统一，也可逐槽位指定。
- 视频双路策划：原创剧本，或克隆参考爆款视频的结构（本地抽帧 → 拆解 → 替换为本品）。
- MiniMax H3 提示词自动编写：内置官方 `h3-prompt-writing` 技能。分镜绑定了商品参考图时用 Ref2VA，否则用 I2VA，无需手动选择。
- 视频版本管理：每个分镜单独显示最新视频，重新生成会保存为新版本，不覆盖旧视频。
- 三路并发：主图、详情页、视频三个选项卡各自排队，互不阻塞；切换选项卡不会中断任务。
- 实时回显：运行日志实时滚动，图片生成一张显示一张。
- 本地持久化：所有状态写入 `workspace/projects/`，重启后可恢复历史项目。
- 合规检查：按广告法与类目违禁词实时提示。

## 快速开始

### 环境要求

- Python 3.11 或更高版本（开发环境为 3.14）
- 无需 Node.js、Redis 或 Docker。视频抽帧所需的 ffmpeg 由 `imageio-ffmpeg` 自带
- 演示图会用到中文字体：Windows / macOS 自带；Linux 请安装 Noto CJK 或文泉驿字体

### 安装与启动

```bash
git clone https://github.com/tuolin2013/laotuo-ai-studio.git
cd laotuo-ai-studio

python -m venv .venv
# Windows（Git Bash）
.venv/Scripts/python -m pip install -e ".[dev]"
# macOS / Linux
# .venv/bin/python -m pip install -e ".[dev]"

cp .env.example .env          # 不填任何 Key 也能启动（演示模式）
.venv/Scripts/python -m app.main
```

浏览器打开 http://127.0.0.1:7860 。

首次体验建议保持 `.env` 中的 Key 为空，或在界面勾选「🧪 本地演示测试模式」，先走一遍完整流程，不消耗任何额度。

## 配置说明

所有配置在 `.env` 中（模板见 [.env.example](.env.example)），也可以在界面「⚙️ API 密钥与接口配置」中修改并写回 `.env`。

### 三个通道，独立判定真实 / 演示

| 通道 | 需要的配置 | 用途 | 未配置时 |
|---|---|---|---|
| LLM | `RIGHTAPI_LLM_KEY` | 执行全部 Skill（策划、编剧、提示词） | 演示数据，JSON 结构与真实输出一致 |
| 画图 · image 高质量 | `RIGHTAPI_IMAGE_KEY` | GPT Image / Nano Banana 生图（生产默认） | PIL 绘制中文排版的预览图，带「DEMO」角标 |
| 画图 · image 普通 | `RIGHTAPI_IMAGE_STD_KEY` | 普通渠道的 GPT Image / Nano Banana 模型 | 同上 |
| 视频 | 本机已登录 Modal | 调用自行部署的 MiniMax H3 服务 | ffmpeg 合成演示视频 |

`DEMO_MODE=true` 会强制所有通道走演示模式，即使已填写 Key。

### 常用配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RIGHTAPI_BASE_URL` | `https://www.rightapi.ai` | RightAPI 网关地址，会自动补全协议、去掉结尾的 `/v1` |
| `RIGHTAPI_LLM_MODEL` | `claude-opus-5-5` | LLM 模型 |
| `RIGHTAPI_LLM_API_FORMAT` | `auto` | `anthropic`（`/v1/messages`）/ `openai`（`/v1/chat/completions`）/ `auto` 自动探测 |
| `RIGHTAPI_IMAGE_STD_BASE_URL` | 空（同 `RIGHTAPI_BASE_URL`） | 普通渠道的 Base URL |
| `RIGHTAPI_IMAGE_MODELS` | 两个渠道全部 11 个模型 | 界面可选的画图模型，必须是已登记的模型 |
| `RIGHTAPI_IMAGE_BUSY_RETRIES` | `3` | 上游繁忙时自动重新提交的次数，`0` 关闭 |
| `RIGHTAPI_IMAGE_RESOLUTION` | `2K` | `1K` / `2K` / `4K` |
| `RIGHTAPI_IMAGE_QUALITY` | `high` | GPT Image 质量档，`xhigh` / `max` 仅 2.5 模型可用 |
| `RIGHTAPI_IMAGE_CONCURRENCY` | `8` | 本机同时进行的画图任务数 |
| `MODAL_H3_APP` / `MODAL_H3_CLASS` | `minimax-h3-diffusers-service` / `H3BatchRunner` | Modal 上 H3 服务的应用名与类名 |
| `GRADIO_SERVER_NAME` | `127.0.0.1` | 监听地址，见[安全须知](#安全须知) |
| `GRADIO_SERVER_PORT` | `7860` | 监听端口 |

### 画图模型

协议由模型决定，不按名字猜测。所有模型在 [app/providers/image_models.py](app/providers/image_models.py) 中显式登记，未登记的模型会被拒绝调用。

| 渠道 | 模型 | 协议 |
|---|---|---|
| image 高质量（推荐） | `gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`、`gpt-image-2` | OpenAI 生成 / 编辑接口 |
| image 高质量（推荐） | `gemini-3-pro-image-preview`、`gemini-nano-banana-2.1` | Gemini `generateContent` |
| image 普通 | `gpt-image-2`（目录 ID `gpt-image-2@image`）、`gpt-image-2-vip`、`gpt-image-2.5` | OpenAI 生成 / 编辑接口 |
| image 普通 | `nano-banana-pro`、`nano-banana-2`、`nano-banana-2-lite` | Gemini `generateContent` |

全部请求以异步任务提交并轮询结果，接口细节以 [RightAPI 官方文档](https://docs.rightapi.ai/docs/rc_draw/) 为准。

### MiniMax H3 视频服务（可选）

本仓库只包含调用端，不包含 H3 服务端部署脚本。如需真实生成视频：

1. 在 Modal 上自行部署 MiniMax H3 服务，提供 `H3BatchRunner` 类（可通过 `MODAL_H3_CLASS` 修改）：
   - 构造参数 `workflow="ref2va"`
   - 方法 `render_single_shot(prompt, images, frames, seed, steps, aspect_ratio)`，`images` 为图片字节列表，返回 mp4 字节
2. 在本机登录 Modal：`modal token new`
3. 在界面点击「🔎 检查 Modal H3 服务（不启动 GPU、不扣费）」确认可用

冒烟测试脚本 [scripts/h3_smoke_test.py](scripts/h3_smoke_test.py) 会真实占用 GPU 并产生费用。

未登录 Modal 或开启演示模式时，视频由本地演示后端（ffmpeg 合成）生成，可以先走通完整流程。

## 使用教程

### 1. 准备项目（顶部控制栏）

1. 点击「➕ 新建项目」，或在下拉框中选择历史项目后点「📂 加载」。
2. 选择类目和平台。内置类目：张家界莓茶、宠物营养补充剂；内置平台：淘宝 / 天猫、拼多多、微信小店 / 视频号小店。
3. 展开「📝 商品信息」填写表单。表单字段随类目变化，下方实时显示合规检查结果。
4. 配好 Key 后，点「🩺 连通性诊断」验证。诊断不提交画图任务，不消耗额度。

### 2. 主图 / 详情页

两个选项卡流程相同，以主图为例。

Step 1　生成与编辑策划案

1. 在「💡 我的策划想法（选填）」写下创意方向。填写后作为最高优先级约束；留空则由 AI 根据类目和平台自主策划。
2. 选择「🎨 全局视觉设计美学流派」、张数与比例，点击「✨ 生成主图策划案」。
3. 在表格中修改标题、文案、画面描述、构图、参考图需求，点「💾 保存表格修改」。也可以单独重新生成某个槽位，或增删、调整槽位顺序。
4. 满意后点「✅ 确认主图策划案，生成传图清单」。系统会汇总出去重后的《所需参考图清单》。如需再改，点「🔓 解锁返回修改策划」。

Step 2　按清单传图与并发渲染

1. 按清单上传参考图。每个角色只需上传一次，会自动绑定到所有相关槽位。「♻️ 一键复用」可直接使用项目内其他模块已上传的图。
2. 个别槽位需要不同的参考图时，在「🎯 为特定槽位单独上传 / 替换参考图」中绑定。
3. 选择生图模型、分辨率、质量档和每槽位候选数量（1~4），点击「🚀 开始并发生成主图」。
4. 在「⭐ 勾选定稿图」中选出每个槽位的定稿，点「⭐ 保存定稿勾选」，再点「📦 打包导出定稿」下载 zip。

详情页选项卡额外提供「📜 详情页长图序列预览」，按屏顺序拼出每屏定稿图。

### 3. 带货短视频

视频的防变形链路：

```
剧本 → 确认剧本 → 生成首帧（比例与视频一致）→ 锁定首帧 → 自动编写 H3 提示词 → Modal H3 生成带声视频
```

前一步未确认时，后一步不可执行。

Step 1　分镜剧本（二选一）

- 📝 自己策划原创剧本：选择视频类型（场景种草 / 产品展示 / 信任背书 / 对比测评 / 情感故事）、分镜数量和比例，点「✨ 生成带货视频剧本」。也可以在「🧭 进阶（原创）」中按「① 创意大纲 → ② 完整剧本 → ③ 编译为分镜」三步走。
- 📹 克隆参考爆款视频：上传一段参考视频（.mp4 / .mov / .webm），系统在本地抽取 4~8 张关键帧，拆解原片的钩子与节奏，再按原片结构替换为当前商品。

两种方式都会填入同一张可编辑的分镜表。每个分镜包含声音设计：人物同期声对白、环境音与动作音效，或静音。H3 原生生成声音，所以没有单独的画外音轨。编辑完成后点「✅ 确认分镜剧本」。

Step 2　首帧锁定

1. 在「2.1」中按汇总清单上传首帧所需的参考图。
2. 在「2.2」中点「🖼️ 开始批量生成各分镜首帧候选图」。
3. 在「2.3」中为每个分镜勾选一张商品没有变形的首帧，点「🔒 保存首帧锁定勾选」；或用「🔒 全部分镜自动锁定最新首帧」快速锁定。

Step 3　MiniMax H3 生成带声视频

1. 选择生成方式：
   - ① 逐个分镜依次生成（默认）：生成全部已锁定首帧的分镜。勾选「跳过已有视频的分镜」时，只补齐还没生成的。
   - ② 选分镜生成：只生成勾选的分镜。
2. 点「🎬 开始生成视频」。分镜排队逐个生成，每个约 5~8 分钟；某个分镜失败不影响其余分镜。
3. 在「🎞️ 各分镜视频」中查看结果，每个分镜一个播放器，显示最新版本。重新生成会保存为第 N 版，旧版本在「🗂️ 全部视频版本」中。

H3 提示词由系统在生成前自动编写，写法按分镜自动选择：

- 绑定了商品参考图：Ref2VA，`<Picture 1>` 为锁定首帧，`<Picture 2>` 起为商品参考图，包装更稳定。
- 没有参考图：I2VA，只使用锁定首帧。

如需查看或微调，展开「📝 H3 音画提示词（系统自动编写，可选查看 / 微调）」，可重新编写单个分镜或全部分镜，改完点「💾 保存微调」。

H3 帧数按「时长 × 24fps」向上取整到 17n+5，范围 124~345 帧（约 5.2~14.4 秒），短于 5.2 秒的分镜会被拉长。

Step 4　剪辑大师一键成片 · 导出剪映 / 达芬奇草稿

1. 点「🧠 生成剪辑方案」（可先在「✂️ 我的剪辑想法」里写要求，例如「控制在 30 秒内」）。剪辑大师会先检测每个分镜视频里台词和声音的实际位置，再决定顺序、掐头去尾、转场和卖点花字，保证不剪断台词。台词字幕直接取自剧本里的人物同期声对白，不做语音识别，字不会错。
2. 在剪辑方案表格里微调：改「顺序」调换镜头先后、取消「启用」去掉某个镜头、修改入点 / 出点、转场（硬切 / 叠化 / 闪黑 / 闪白）、原声音量和卖点花字，改完点「💾 保存剪辑修改」。
3. 展开「🎚️ 成片选项」可以选择是否烧录字幕和花字，并上传背景音乐（请使用有版权授权的音乐）。人物说话时音乐自动压低，成片响度统一到短视频平台常用的 -14 LUFS。
4. 点「🎞️ 一键成片」，本地 ffmpeg 合成 1080P、24 帧的 mp4，在「成片预览」中播放和下载。
5. 想继续精剪时：
   - 「🎬 一键导出剪映草稿」：本机装了剪映专业版时，草稿会直接写进剪映的草稿文件夹，重新打开剪映就能在「本地草稿」里看到；同时提供 zip 压缩包，可拷到其他电脑使用。
   - 「🎨 一键导出达芬奇工程」：生成 FCPXML 1.10（DaVinci Resolve 18 及以上）、OTIO 时间线、台词字幕 / 卖点花字 SRT 和素材文件夹，压缩包里附中文导入说明。

两种草稿的时间线与成片完全一致：顺序、出入点、转场、字幕都已排好。剪映草稿和达芬奇工程目前只验证了文件结构，还没有在真实软件中打开测试，首次使用时请确认能正常导入。

### 4. 产出文件

```
workspace/projects/{project_id}/
├── project.json            # 项目完整快照（策划案、剧本、状态）
├── edit_log.jsonl          # 编辑记录（追加写）
├── references/             # 上传的参考图
├── candidates/{slot_id}/   # 主图 / 详情页候选图
├── first_frames/{shot_id}/ # 视频首帧
├── videos/{shot_id}/       # 生成的视频（每次重新生成都保留为新版本）
├── prompts/                # 编译后的提示词
├── edits/{edit_id}/        # 一键成片 mp4（每次合成都保留为新文件）与字幕文件
└── exports/                # 打包导出的 zip、剪映草稿、达芬奇工程
```

## 系统架构

```mermaid
flowchart LR
    UI["Gradio 界面<br/>app/ui"] --> Engine["引擎层<br/>app/engine"]
    Engine --> Skills[("Skill 卡带<br/>config/skills")]
    Engine --> Config[("类目 / 平台配置<br/>config/*.json")]
    Engine --> Store[("本地持久化<br/>workspace/projects")]
    Engine --> Providers["Provider 层<br/>app/providers"]
    Providers --> LLM["RightAPI · Claude"]
    Providers --> IMG["RightAPI · GPT Image / Nano Banana"]
    Providers --> H3["Modal · MiniMax H3"]
    Providers --> Demo["本地演示兜底"]
```

### 分层说明

| 层 | 位置 | 职责 |
|---|---|---|
| 界面 | `app/ui/` | 顶部控制栏 + 主图 / 详情页 / 视频三个选项卡。浏览器端只保存 `project_id` |
| 引擎 | `app/engine/` | 策划、编剧、提示词编译、并发渲染、项目管理、合规检查 |
| 数据模型 | `app/schemas/` | Pydantic v2 模型，草稿 / 确认状态机与校验规则 |
| Provider | `app/providers/` | LLM、画图、视频后端客户端，以及零 Key 演示实现 |
| 存储 | `app/storage/` | 项目落盘，JSON 先写临时文件再原子替换 |

### 引擎模块

| 模块 | 职责 |
|---|---|
| `runtime.py` | 持有配置、Skill 注册表与各通道客户端；决定每个通道走真实接口还是演示 |
| `planner_engine.py` | 执行 `ecommerce_planner` 卡带，生成主图 / 详情页策划草稿 |
| `concept_engine.py` / `story_engine.py` | 视频进阶编剧：创意大纲 → 完整剧本 |
| `script_engine.py` | 生成分镜剧本，支持原创与爆款克隆两种模式 |
| `video_cloner.py` | 参考视频本地抽帧，记录时间戳 |
| `prompt_compiler.py` | 编译生图提示词与 H3 音画提示词 |
| `render_engine.py` | 并发生成候选图 / 首帧 / 视频，以异步生成器逐条推送事件 |
| `project_service.py` | 新建 / 加载项目、跨模块复用参考图、导出定稿 |
| `compliance.py` | 广告法与类目违禁词检查 |
| `diagnostics.py` | 连通性诊断 |
| `edit_engine.py` | 剪辑大师：检测各分镜视频的台词位置，执行 `video_editor` 卡带生成剪辑方案 |
| `edit_render.py` | 按剪辑方案用 ffmpeg 合成成片（转场、字幕花字、背景音乐、响度统一） |
| `draft_export.py` | 导出剪映草稿与达芬奇工程（FCPXML / OTIO / SRT） |

### 并发模型

- 每个长任务都是 `async def` + `yield` 的异步生成器，持续推送日志和已完成的图片。
- `demo.queue()` 开启队列，三个选项卡使用不同的 `concurrency_id`，互不阻塞。
- 事件处理中不使用阻塞 IO，网络请求统一使用 `httpx.AsyncClient`，外部进程用 `asyncio` 子进程。

## 插卡式 Skill 系统

每个 AI 能力对应 `config/skills/` 下的一个卡槽，卡槽由文件夹名决定：

| 卡槽 | 用途 |
|---|---|
| `ecommerce_planner` | 主图 / 详情页策划 |
| `concept_writer` | 视频创意大纲 |
| `story_scriptwriter` | 视频完整剧本（按视频类型分卡带） |
| `video_scriptwriter` | 视频分镜剧本（原创 / 爆款克隆） |
| `image_prompt` | 生图提示词（GPT Image / Nano Banana） |
| `video_prompt` | MiniMax H3 音画提示词 |
| `video_editor` | 剪辑大师：分镜视频 → 剪辑方案（顺序、出入点、转场、卖点花字） |

卡带格式为 YAML front matter + Markdown 正文：

```markdown
---
skill_id: my_planner_v2          # 必填，全局唯一，小写字母 / 数字 / 下划线
name: 我的策划 Skill              # 必填，界面显示名
description: 一句话说明            # 选填
version: "1.0"                   # 选填
variables: [product_name]        # 选填，声明正文使用的变量
default: false                   # 选填，true 表示该卡槽默认选中
categories: [zhangjiajie_berry_tea]  # 选填，类目专属卡带；不写则所有类目可用
references: [references/guide.md]    # 选填，附属指南，加载时自动读入
---
正文……可以使用 {{ product_name }} 这样的变量占位。
```

- 新卡带放进对应文件夹即可被识别，按文件修改时间热重载，无需重启。
- 渲染时缺少变量会报错，不会把未填充的占位符发给模型。
- 单张卡带解析失败不影响其他卡带，错误显示在界面上。
- 界面「🔌 插卡式 Skill 管理与在线微调」可预览正文，并临时覆盖当前会话的指令，覆盖内容不写回文件。
- 附属指南只允许放在卡带同目录的 `references/` 下，单文件不超过 200KB。

## 扩展开发

### 新增类目或平台

在 `config/categories/` 或 `config/platforms/` 下新增 JSON 文件，文件名必须与其中的 `category_id` / `platform_id` 一致。字段定义见 [app/schemas/config.py](app/schemas/config.py)，可参考现有的 [zhangjiajie_berry_tea.json](config/categories/zhangjiajie_berry_tea.json)。

类目专属卡带建议命名为 `laotuo_{类目}_{卡槽}_skill.md`，并在 front matter 中声明 `categories`。

### 新增画图模型

在 [app/providers/image_models.py](app/providers/image_models.py) 的 `CATALOG` 中登记模型名、协议（`openai` / `gemini`）和渠道。

### 新增视频后端

1. 在 `app/providers/video/` 下继承 `VideoProvider`，实现 `is_configured()` 和异步生成器 `generate()`，按需实现 `warmup()`。
2. 用 `@register_provider` 注册。
3. 在 `EngineRuntime.video_provider()` 中加入创建分支，并把视频选项卡中的 `VIDEO_PROVIDER` 改为新后端的 `provider_id`。

接口定义见 [app/providers/video/base.py](app/providers/video/base.py)。

## 开发与测试

```bash
.venv/Scripts/python -m pytest          # 全部测试
.venv/Scripts/python -m ruff check .    # 代码检查
```

- 测试使用 `httpx.MockTransport` 与测试替身模拟外部接口，不需要真实 Key，也不会产生费用。
- `tests/test_ui_e2e.py` 会启动真实 Gradio 服务，用 `gradio_client` 驱动全流程。
- 项目全局规范见 [CLAUDE.md](CLAUDE.md)。

## 安全须知

- `.env` 已被 `.gitignore` 忽略，请勿提交。日志与诊断结果中的 Key 均已打码。
- 界面默认只监听 `127.0.0.1`，且没有登录鉴权。如果把 `GRADIO_SERVER_NAME` 改为 `0.0.0.0`，同一网络内任何人都能访问界面并使用你的 Key。
- 开启了系统代理（如 `127.0.0.1:7890`）时，启动程序会自动把本机地址加入 `NO_PROXY`，避免 Gradio 自检返回 502。
- 合规检查基于关键词匹配，只作提示，不构成法律意见。AI 生成的文案与画面在发布前请人工审核。

## 贡献指南

欢迎提交 Issue 和 Pull Request。提交前请确认：

1. 新功能附带 pytest 用例。
2. `pytest` 与 `ruff check .` 零报错。
3. 新增依赖使用精确版本号。
4. 面向用户的界面文案使用中文。
5. 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，例如 `feat(video): ...`。

提交贡献即表示你同意以 GPL-3.0 授权你的代码。

## 许可证

本项目采用 [GNU 通用公共许可证 v3.0（GPL-3.0）](LICENSE) 发布。你可以自由使用、修改和再发布本项目；再发布修改后的版本时，必须同样以 GPL-3.0 开源并附带源代码。

仓库中收录的第三方内容，版权归原作者所有，不适用本项目的许可证：

- `config/skills/video_prompt/official_h3_prompt_writing.md` 中的「Official Skill」原文，以及同目录 `references/base-en.txt`、`references/ref-en.txt`（来自 MiniMax-AI/MiniMax-H3）
- `.claude/skills/h3-prompt-writing/` 与 `.claude/skills/hypit/` 下的技能文件

## 致谢

- [MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)：官方 `h3-prompt-writing` 技能原文收录于 [config/skills/video_prompt/](config/skills/video_prompt/)
- [RightAPI](https://docs.rightapi.ai/)：LLM 与画图统一网关
- [Gradio](https://www.gradio.app/)、[Modal](https://modal.com/)、[Pydantic](https://docs.pydantic.dev/)、[httpx](https://www.python-httpx.org/)、[Pillow](https://python-pillow.org/)
- [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)（Apache-2.0）：剪映草稿生成
- [kajisho5/ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill)（MIT）：FCPXML / OTIO 时间线写法参考
