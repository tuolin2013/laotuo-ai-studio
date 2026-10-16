# CLAUDE.md — 电商 AIGC 内容工厂（laotuo-ai-studio）

本文件是项目的全局规范。所有代码改动必须遵守这里的约定；规范变更时先改本文件。

## 1. 产品定位

面向电商运营的一体化 AIGC 内容生产工具，产出三类素材：

- 主图（Main Images）
- 详情页（Detail Pages，按屏规划）
- 带货短视频（MiniMax H3 原生音画一体，含首帧锁定防变形）

产品负责人不写代码，所有命令由工程师（或 AI 工程师）执行。

## 2. 技术栈

- Python 3.11+（当前开发环境 3.14），100% 纯 Python。
- Web 界面：Gradio（版本锁定在 `pyproject.toml`）。无 Node.js、无 Redis、无 Docker。
- 数据模型：Pydantic v2。HTTP：httpx（异步）。图像：Pillow。配置：python-dotenv + PyYAML + JSON。
- 测试：pytest + pytest-asyncio；代码检查：ruff。
- 依赖一律使用精确版本号。

### 常用命令（Windows Git Bash）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m app.main        # 单命令启动 Web 界面
```

## 3. 系统架构

### 3.1 三路独立并发

- 界面使用 `gr.Tabs` 分为【主图】【详情页】【视频】三个选项卡。
- 每个选项卡的长任务都写成 `async def` + `yield` 的异步生成器，持续推送：
  1. 运行日志（Running Logs，实时滚动）
  2. 已完成的图片 / 视频（生成一张回显一张，不等全部完成）
- `demo.queue(default_concurrency_limit=...)` 开启队列；三个选项卡的事件使用不同的 `concurrency_id`，互不阻塞。
- 切换选项卡不得中断任务。禁止在事件处理函数中使用阻塞 IO（`time.sleep`、同步 `requests` 等），统一使用 `asyncio` / `httpx.AsyncClient`。

### 3.2 本地自动持久化

所有状态实时写入 `workspace/projects/{project_id}/`：

```
workspace/projects/{project_id}/
├── project.json            # Project 模型完整快照（策划案、剧本、状态、编辑记录）
├── edit_log.jsonl          # 用户编辑记录（追加写）
├── references/             # 用户上传的参考图
├── candidates/{slot_id}/   # 生成的候选图
├── first_frames/{shot_id}/ # 视频首帧图
├── videos/{shot_id}/       # 生成或回传绑定的视频
└── prompts/                # 编译后的生图 / H3 提示词
```

- 写 JSON 先写临时文件再原子替换，避免崩溃时损坏。
- 启动时可从 `workspace/projects/` 列出并恢复历史项目。
- `workspace/` 内容不入 git。

### 3.3 目录结构

```
├── CLAUDE.md
├── .env.example
├── pyproject.toml
├── config/
│   ├── categories/      # 类目配置 JSON
│   ├── platforms/       # 平台配置 JSON
│   └── skills/          # 插卡式 Skill（.md 卡带）
│       ├── ecommerce_planner/
│       ├── video_scriptwriter/
│       ├── image_prompt/
│       └── video_prompt/
├── app/
│   ├── main.py          # Gradio 启动入口
│   ├── settings.py      # 读取 .env
│   ├── schemas/         # Pydantic 数据模型
│   ├── engine/          # 配置加载、Skill 加载器、编译器、生图路由
│   ├── providers/       # llm.py / image.py / video/
│   ├── storage/         # 项目落盘
│   └── ui/              # Gradio 组件
├── workspace/
└── tests/
```

## 4. 插卡式 Skill 系统

- 所有 AI 能力（电商策划、视频编剧、生图提示词、H3 视频提示词）都由 `config/skills/{slot}/*.md` 卡带驱动，代码里不写死提示词。
- 4 个卡槽：`ecommerce_planner` / `video_scriptwriter` / `image_prompt` / `video_prompt`。卡槽由所在文件夹决定。
- 卡带格式：顶部 YAML front matter + Markdown 正文模板。

```markdown
---
skill_id: my_planner_v2          # 必填，全局唯一，小写字母/数字/下划线
name: 我的策划 Skill              # 必填，界面显示名
description: 一句话说明            # 选填
version: "1.0"                   # 选填
target_models: [claude-opus-5-5] # 选填，适用模型
variables: [product_name]        # 选填，声明正文使用的变量
---
正文……可以使用 {{ product_name }} 这样的变量占位。
```

- 变量语法：`{{ var }}`。未提供的变量渲染时报错（`SkillRenderError`），避免把未填充的占位符发给模型。
- `SkillRegistry` 负责扫描、解析、热重载（按文件修改时间检测）。新卡带放进文件夹即可被识别，无需改代码。
- 解析失败的卡带不影响其他卡带，错误记录在 `registry.errors` 中供界面展示。
- 界面支持预览卡带正文，并可临时覆盖（override）当前会话使用的指令，覆盖内容不写回 `.md` 文件。

## 5. 核心业务流程

### 5.1 策划三步式工作流（主图 / 详情页 / 视频通用）

1. 输入「我的策划想法（选填）」`user_creative_idea`。填写时作为最高优先级约束；留空则 AI 根据类目 + 平台配置自主策划。
2. 生成策划案 → 状态 `plan_status = "draft"`。草稿状态下可以：
   - 修改想法后重新生成整套策划，或只重新生成某一个槽位；
   - 手工编辑槽位的标题、文案、画面描述、生图模式、参考图需求；
   - 增加、删除、调整槽位顺序。
3. 点击「确认策划方案」→ `confirm_plan()` → `plan_status = "confirmed"`，并基于**最终版本**重新计算去重后的《所需参考图清单》。
   - `confirmed` 状态下禁止编辑，必须 `unlock_plan()` 退回 `draft` 才能修改。
   - 未确认的策划案禁止进入传图与渲染阶段（`require_confirmed()` 拦截）。

### 5.2 生图模式判定

| 模式 | 参考图数量 | 典型场景 |
|---|---|---|
| `text_to_image` | 0 | 痛点场景、产区风光、氛围图 |
| `single_image_edit` | 1 | 单主体海报、微距特写（保真商品） |
| `multi_image_fusion` | 2~3 | 包装 + 茶汤 / 内料 / 检测报告同框 |

- 模式与 `required_references` 数量必须一致，由模型校验器强制保证。
- 参考图按「角色」（如 `product_package`、`tea_soup`、`inspection_report`）登记；同一角色上传一次，自动绑定到所有需要该角色的槽位，也允许单槽位单独增删。
- 每个槽位可设置候选图数量 1~4，并发生成供勾选定稿。
- 主图 / 详情页张数不锁死：平台配置给出默认建议值，用户可自由指定数量与比例。

### 5.3 RightAPI 三把独立 Key

| Key | 用途 | 接口 |
|---|---|---|
| `RIGHTAPI_LLM_KEY` | Claude LLM，执行全部 Skill | RightAPI LLM 接口 |
| `RIGHTAPI_OPENAI_IMAGE_KEY` | gpt-image-2 / 2.5-flare / 2.5-sunburst | 参考图 0 张 → `POST /v1/images/generations`；≥1 张 → `POST /v1/images/edits` |
| `RIGHTAPI_GEMINI_IMAGE_KEY` | gemini-3-pro-image-preview / nano-banana-2.1 | `POST /v1beta/models/{model}:generateContent`（禁止 `:streamGenerateContent`）；参考图以 base64 `inline_data` 放入 `contents.parts`；支持 `"async": true` + `task_id` 轮询 |

- 路由依据是槽位**实际绑定**的参考图数量，而不是策划时的模式字段。
- 三把 Key 互不混用；Key 只从环境变量读取，日志中不得打印 Key。

### 5.4 视频：MiniMax H3 原生音画一体

- H3 生成的视频**原生自带声音**。本项目不设计画外音（Voiceover）轨道，数据模型中不出现 voiceover 字段。
- 每个分镜 `StoryboardShot` 的声音字段：
  - `audio_mode`: `native_dialogue` | `ambient_sfx` | `dialogue_and_ambient` | `silent`
  - `character_dialogue`: 镜头内人物同期声对白 + 语气描述（含对白的模式必填）
  - `ambient_sound_description`: 原生环境音与物理动作音效（含环境音的模式必填）
- 防变形链路：
  `选类型/比例 + 选填想法 → 编剧 Skill 生成分镜剧本 → 用户编辑并确认剧本 → 生成首帧图（比例强制与视频比例一致）→ 用户勾选确认首帧 → H3 Prompt Skill 编译音画提示词 → 生成视频或复制到 ComfyUI`
- 未确认剧本不能生成首帧；未确认首帧不能编译 H3 提示词或生成视频。
- ComfyUI 协同：第 1 分镜（以及每个分镜和全局汇总）提供醒目的「一键复制 H3 提示词」与「下载锁定首帧原图」，并支持把外部生成的视频回传绑定到分镜。

### 5.5 可扩展 VideoProvider

- 统一接口位于 `app/providers/video/base.py`：`warmup()`（Serverless 冷启动预热）、`generate()`、默认 600 秒超时。
- 实现：`ModalMiniMaxH3Provider`（Modal 上的 H3）、`ManualComfyUIBridgeProvider`（人工复制粘贴桥接）、`FalAIVideoProvider`（预留，暂未实现）。
- 新增视频后端只需继承 `VideoProvider` 并注册，不改业务代码。

## 6. 编码规范

- 代码与注释用中文或英文均可，面向用户的界面文案一律中文。
- 所有业务状态变更走模型方法（如 `add_slot` / `update_slot` / `confirm_plan`），UI 层不直接改字段，便于统一校验与落盘。
- 新功能必须附带 pytest 用例；提交前 `pytest` 与 `ruff check .` 必须零报错。
- 网络调用必须设置超时；外部返回内容视为不可信数据。
- 界面默认只监听 `127.0.0.1`。如需局域网访问须提醒：当前无登录鉴权。
- 提交信息遵循 Conventional Commits，例如 `feat(step1): ...`。

