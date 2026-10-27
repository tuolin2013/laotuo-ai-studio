# CLAUDE.md — 老庹 AI 电商内容工厂（laotuo-ai-studio）

本文件是项目的全局规范。所有代码改动必须遵守这里的约定；规范变更时先改本文件。

## 1. 产品定位

面向电商运营的一体化 AIGC 内容生产工具，产出五类素材：

- 主图（Main Images）
- 详情页（Detail Pages，按屏规划）
- 带货短视频（MiniMax H3 原生音画一体，含首帧锁定防变形）
- 配音（VoxCPM2 口播配音，独立成品：wav / mp3 / srt）
- 动效广告（零成本代码动效：用项目已有素材按节拍编排，本机渲染画面、配乐与音效，不调用生图 / 生视频接口）

产品负责人不写代码，所有命令由工程师（或 AI 工程师）执行。

品牌：老庹 AI 电商内容工厂（Laotuo AI E-Commerce Studio），出品方「老庹 AI 工作室（Laotuo AI Studio）」。「庹」读 tuǒ，不是「拓」，界面与文档一律写「老庹」；skill_id、文件名与仓库名沿用拼音 `laotuo`。

商业模式 Open-Core：工作台底座开源，行业高转化卡带（Pro 卡带包）与部署 / 定制服务收费。许可证为 Apache-2.0 + 附加条件（见 `LICENSE` 文末补充条款）。README 与界面中的卖点只写代码里真实存在的能力。以下内容不进开源仓库：`.env` / `.env.*`（`.env.example` 除外）、`modal/`（自有 GPU 部署脚本）、`config/skills_pro/`、`tests/pro/`、`workspace/`、`promo-film/`。

私人全量备份（`scripts/sync_private_backup.py`）：公开仓 `origin` 的 `main` 只放开源底座；私人仓 `private` 的 `main` = 本地 `master` 最新提交 + 磁盘上的 `modal/`、`config/skills_pro/`、`tests/pro/`、`promo-film/`（不含 `promo-film/out/`）。备份提交记在隐藏引用 `refs/backup/private-full`，用临时索引生成，不切换分支、不碰工作区与暂存区。脚本安装 `.git/hooks/pre-push`：私有目录推往 `private` 以外的远端一律拦截；任何远端都拦截 `.env` / `.env.*`、缓存产物、`workspace/` 运行产物、`.env` 与 `~/.modal.toml` 中密钥的原文和常见 Key 格式。不要把私有目录提交到 `master`。

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
.venv/Scripts/python scripts/sync_private_backup.py   # 提交后一键推送公开仓 + 私人全量备份（--private-only / --dry-run）
```

## 3. 系统架构

### 3.1 多路独立并发

- 界面使用 `gr.Tabs` 分为【主图】【详情页】【视频】【配音】【动效广告】五个选项卡。
- 每个选项卡的长任务都写成 `async def` + `yield` 的异步生成器，持续推送：
  1. 运行日志（Running Logs，实时滚动）
  2. 已完成的图片 / 视频（生成一张回显一张，不等全部完成）
- `demo.queue(default_concurrency_limit=...)` 开启队列；各选项卡的事件使用不同的 `concurrency_id`，互不阻塞。
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
├── prompts/                # 编译后的生图 / H3 提示词
├── edits/{edit_id}/        # 一键成片 mp4 与字幕 ASS；edits/ 下另存上传的背景音乐
├── voices/                 # 配音：references/ 参考音频（16kHz）、previews/ 试听、{segment_id}/ 逐段配音、{mix_id}/ 完整配音成品
├── motion/                 # 动效广告：upload_* 上传素材、detail_long_* 详情页拼接长图、stills/ 单屏预览、render_{时间}_{档位}/ 成片 + 联系表 + 自检报告
└── exports/                # 定稿 zip、剪映草稿 zip、达芬奇工程文件夹与 zip
```

- 写 JSON 先写临时文件再原子替换，避免崩溃时损坏。
- 启动时可从 `workspace/projects/` 列出并恢复历史项目。
- 音色库跨项目共用：`workspace/voice_library/library.json` + `{voice_id}/sample.wav`（`app/storage/voice_library.py`）。
- `workspace/` 内容不入 git。

### 3.3 目录结构

```
├── CLAUDE.md
├── .env.example
├── pyproject.toml
├── config/
│   ├── categories/      # 类目配置 JSON
│   ├── platforms/       # 平台配置 JSON
│   ├── skills/          # 插卡式 Skill（.md 卡带，开源）
│   │   ├── ecommerce_planner/
│   │   ├── video_scriptwriter/
│   │   ├── image_prompt/
│   │   ├── video_prompt/
│   │   ├── video_editor/
│   │   ├── voice_writer/
│   │   └── motion_director/
│   └── skills_pro/      # 商业 Pro 卡带包（可选，不入 git，结构同 skills/）
├── app/
│   ├── main.py          # Gradio 启动入口
│   ├── settings.py      # 读取 .env
│   ├── schemas/         # Pydantic 数据模型
│   ├── engine/          # 配置加载、Skill 加载器、编译器、生图路由
│   ├── providers/       # llm.py / image.py / video/ / voice/
│   ├── storage/         # 项目落盘
│   └── ui/              # Gradio 组件
├── workspace/
└── tests/
```

## 4. 插卡式 Skill 系统

- 所有 AI 能力（电商策划、视频编剧、生图提示词、H3 视频提示词）都由 `config/skills/{slot}/*.md` 卡带驱动，代码里不写死提示词。
- 卡槽（`app/schemas/skill.py` 的 `SKILL_SLOTS`）：`concept_writer` / `story_scriptwriter` / `ecommerce_planner` / `video_scriptwriter` / `image_prompt` / `video_prompt` / `video_editor` / `voice_writer` / `motion_director`。卡槽由所在文件夹决定。
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
- 附属指南：front matter 的 `references: [references/x.txt]` 或正文中出现的 `references/*.txt` 会在加载时自动读入（只允许卡带同目录 `references/` 下，单文件 ≤200KB）；附属文件修改同样触发热重载。`default: true` 的卡带在下拉框中默认选中。
- 类目专属卡带：front matter 写 `categories: [category_id]`，只在对应类目下出现，并在切换到该类目时自动选中（优先于 `default: true`）；不写 `categories` 的为通用卡带，所有类目可见。每个类目一张专属卡带，命名 `laotuo_{类目}_{卡槽}_skill.md`，如 `ecommerce_planner/laotuo_zhangjiajie_meicha_planner_skill.md`（行业专属卡带属于 Pro 卡带包，见下条）。
- 商业 Pro 卡带包（Open-Core）：`config/skills_pro/{slot}/*.md` 与开源卡带目录结构相同，目录存在才加载（`SkillRegistry(pro_dir=…)`，默认为 `skills_dir` 同级的 `skills_pro/`），先开源后 Pro 扫描；`SkillCard.is_pro` 为真，`display_name` 前缀 `[👑 Pro]`（`PRO_BADGE`），下拉框选项用 `display_name`。`skill_id` 与开源卡带重复的 Pro 卡带报错、不覆盖开源卡带。Pro 卡带在自己目录找不到 `references/` 附属指南时，回落到开源同名卡槽的 `references/`（实际文件记在 `reference_files`）。`config/skills_pro/` 与依赖 Pro 卡带内容的测试 `tests/pro/` 均不入 git；张家界莓茶、辰荣贝康两张行业策划卡带即放在这里，开源仓库只保留通用卡带，公开测试不得依赖 Pro 卡带（需要类目专属卡带时在临时目录里自建）。
- 卡带继承（`app/engine/skill_inherit.py`）：front matter 写 `extends: 父卡带 skill_id`（同一卡槽）即获得父卡带全部区块。正文用一级标题尾标 `# 标题 {#区块id 标记}` 划分区块：同 id 整块替换，`append` / `prepend` 追加，`remove` 删除，新区块用 `after=` / `before=` 定位（默认插在父卡带第一个 `final` 区块之前）；`final` 区块子卡带只能追加，`abstract` 区块子卡带必须实现；`abstract: true` 的卡带只供继承，不出现在下拉框。元数据：`variables` / `references` 取并集，`description` / `version` / `target_models` / `source_url` 子卡带没写就继承，`categories` / `default` 不继承。开源卡带不能继承 Pro 卡带，禁止循环继承，`extends` 最多 3 层；父卡带改动后子卡带随 `refresh()` 一起更新，继承错误记入 `registry.errors`。发给模型前去掉区块标记；`python -m app.engine.skill_loader --resolve <skill_id>` 打印展开后的正文和每个区块的来源，Pro 面板列出继承链与本卡改动的区块。`default_planner` 已按区块组织（`task`、`output_format` 为 final），莓茶、辰荣贝康两张 Pro 策划卡带继承它，只写差异区块。不写 `extends` 也没有区块标记的卡带，行为与原来完全一样。
- 官方 H3 技能：`config/skills/video_prompt/official_h3_prompt_writing.md` 收录 MiniMax-AI/MiniMax-H3 `skills/h3-prompt-writing/SKILL.md` 原文（附 `references/base-en.txt`、`references/ref-en.txt` 原版），同一份原版也放在 `.claude/skills/h3-prompt-writing/` 供 Claude Code 使用。正文使用 `{{ h3_mode }}` 的卡带即支持双模式，**模式由系统自动选择，界面不让用户选**（`prompt_compiler.auto_h3_mode`）：分镜绑定了文件存在的商品参考图 → `ref2va`，否则 → `i2va`：
  - `i2va`：按 base-en.txt 输出 I2VA 指令行 + `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music`；视频后端只传锁定首帧。
  - `ref2va`：按 ref-en.txt 输出六段式 `subject_definitions` / `summary` / `retention_analysis` / `detailed_description` / `overall_soundscape` / `non_diegetic_music`；`<Picture 1>` = 锁定首帧，`<Picture 2>`… = 该分镜绑定的商品参考图（与后端传图顺序一致）。
  - 画面与声音用英文，台词与包装可见文字保留中文原文；时长取分镜时长并限制在 4–15 秒；输出经 `h3_structure_issues()` 校验字段与顺序。
  - `h3_prompt` 为视频生成实际使用的版本，`h3_prompt_mode` 记录其写法；旧版自定义卡带（无 `{{ h3_mode }}`）编译的提示词 `h3_official_mode` 为 None。

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
- 视觉设计三维体系：每个槽位另有 `aesthetic_style`（5 大美学流派）与 `composition_mode`（6 大构图模式），取值与中文名定义在 `app/schemas/plan.py`，依据 `config/skills/image_prompt/references/ecommerce_aesthetics_guide.md`。`auto` 表示交给 Skill 按指南匹配；LLM 输出无法识别的值时退回 `auto`，不让整套策划失败。
  - Step 1「🎨 全局视觉设计美学流派」选定后整套槽位统一使用该流派（策划指令末尾追加约束，结果再强制覆盖，单槽位重生成同样生效）；构图逐槽位选择。
  - 策划表格中两列显示中文名，可填中文名、代码或「自动」。
  - 生图 Skill 编译时把卡带声明的 `references/` 指南全文附在指令之后；演示模式按流派色板 / 字体与构图留白区绘制预览图（`app/providers/demo/aesthetics.py`）。
  - 上图字数：海报类槽位 ≤18 个汉字（主图 ≤14）。详情页信息屏（用法 / 问答 / 配料与保证值 / 资质 / 规格售后）放宽到 ≤120 字，逐行照抄 `copy_text`。`slot_prompt_data()` 给生图 Skill 传 `deliverable_type` 与代码数好的 `copy_text_hanzi_count`。系统没有后期排字环节，提示词不得要求卡片 / 文字框留空。
  - 中文排版两级优先：① 防变形底线（指南第三维 8 条：字数、逐字锁死、平面正视、标准字形、干净承托面、高反差、字号下限、零遮挡不留空），任何创意不得突破；② 底线之内按场景从「创意排版方案库」选型或组合（A 常规留白 / B 毛玻璃面板 / C 信息图表 / D 卡片表格 / E 大字报 / F 东方竖排 / G 色带价签 / H 实物承载），可创新，不默认只用常规留白。字体随美学流派为推荐而非强制。
- 本草性状生图：类目配置 `ingredient_visuals` 按《中国药典》性状登记每味原料的外观（只写外观、不写功效），`product_lines[].hero_ingredients` 为推荐入画的 3~4 味。槽位 / 首帧的 `title` / `subtitle` / `visual_description` 点名原料时（`CategoryConfig.ingredients_in`，长名优先，「姜黄色」这类形容词不算），编译生图指令才附上 `image_prompt/references/tcm_herb_visual_guide.md` 与这几味的《原料性状图谱》，单图最多 4 味；图谱不进 `category_json`（`prompt_dict()`）。原料实拍参考图角色为 `herb_ingredients`。每味原料的 `source_url` 指向药典 2020 条文；给人看的按单品图谱 `docs/herb_atlas_{category_id}.md` 由 `python -m app.engine.herb_atlas` 从配置生成，不手改，测试检查二者一致。

### 5.3 RightAPI Key 与画图接口（以官方文档为准）

权威文档：https://docs.rightapi.ai/docs/rc_draw/ 。默认 Base URL：`https://www.rightapi.ai`（`normalize_base_url` 自动补 `https://`、去掉结尾 `/`、`/v1`、`/v1beta`；裸域名 `rightapi.ai` 下 `/v1/tasks` 不可达，自动改为 `www`）。

| Key | 用途 |
|---|---|
| `RIGHTAPI_LLM_KEY` | Claude LLM（AwsQ / 官渠），执行全部 Skill；`/v1/messages` 或 `/v1/chat/completions` |
| `RIGHTAPI_IMAGE_KEY` | 「image 高质量」渠道画图 Key，GPT Image 与 Nano Banana 共用。**生产默认**（全模型 4K、异步、不限并发） |
| `RIGHTAPI_IMAGE_STD_KEY` | 「image 普通」渠道画图 Key（选填）；该渠道 Base URL 可用 `RIGHTAPI_IMAGE_STD_BASE_URL` 单独指定，留空同 `RIGHTAPI_BASE_URL` |

RightAPI 的 Key 绑定渠道，一把 Key 只能调用所绑渠道的模型，所以两个画图渠道各用各的 Key，按模型所属渠道自动选择（`image_key(settings, model)` / `Settings.image_base_url(model)`），绝不拿一个渠道的 Key 调另一个渠道。真实 / 演示也按渠道分别判定（通道 `image` / `image_std`，`live_channel_of(model)`）。

**模型决定协议与渠道，绝不按名字猜测**。所有画图模型在 `app/providers/image_models.py` 的 `CATALOG` 中显式登记协议与渠道，未登记的模型直接报错拒绝调用（用错接口会 404 / 400）。两个渠道同名的 `gpt-image-2`：普通渠道一侧目录 ID 为 `gpt-image-2@image`（`api_model` 记录真实模型名，请求中只发 `gpt-image-2`）；界面下拉框以【高质量】/【普通】前缀区分，高质量渠道排在前面并默认选中：

| 渠道 | 模型 | 协议 | 接口 |
|---|---|---|---|
| image 高质量（生产默认） | `gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`、`gpt-image-2` | OpenAI 生成 | 0 张参考图 `POST /v1/images/generations`；≥1 张 `POST /v1/images/edits`（JSON `images` 数组传 data URL） |
| image 高质量（生产默认） | `gemini-3-pro-image-preview`、`gemini-nano-banana-2.1` | Gemini 生成 | `POST /v1beta/models/{model}:generateContent`，文生图与参考图编辑同一接口（`parts` = 文本 + 按序 `inline_data` 纯 base64）；禁止 `:streamGenerateContent` |
| image 普通 | `gpt-image-2`（目录 ID `gpt-image-2@image`）、`gpt-image-2-vip`、`gpt-image-2.5` | OpenAI 生成 | 同上 |
| image 普通 | `nano-banana-pro`、`nano-banana-2`、`nano-banana-2-lite` | Gemini 生成 | 同上 |

参数规则（`app/providers/image.py`）：
- 统一异步：请求体固定 `"async": true` → 返回 `task_id` → 每 3~5 秒轮询 `GET /v1/tasks/{task_id}`，整体上限 600 秒。完成态不带 `status`：OpenAI 取 `data[].url`，Gemini 取 `candidates[].content.parts[].text`（文本即 URL）；`failed / expired / canceled` 按失败处理；提交即失败时响应 `status=failed` 并带 `error`。
- 上游繁忙自动重试：失败原因含 `temporarily busy` / `try again later` / 限流等字样，或提交时 HTTP 429 / 503，视为 `UpstreamBusyError`，等待约 15、30、45 秒（带随机抖动）后重新提交新任务，次数由 `RIGHTAPI_IMAGE_BUSY_RETRIES`（默认 3，0 关闭）控制，日志逐次提示；其他失败与超时不重试。等待期间占用并发名额，不额外加压上游。
- 结果只有图片 URL（不返回 base64、`response_format` 不生效），由客户端下载保存；下载 CDN 时不携带 API Key。
- GPT Image 尺寸用像素值：`gpt_image_size()` 把「比例 + 1K/2K/4K」换算为 16 的倍数、长边 ≤3840、总像素 655,360~8,294,400 的合法值；超过 2560x1440 为实验性大图。`quality` 中 `xhigh / max` 仅目录标记 `supports_xhigh` 的 `gpt-image-2.5-flare / sunburst` 可用，其他 GPT 模型（含普通渠道 `gpt-image-2.5`）自动降为 `high`。每次请求 `n=1`，候选图通过并发多个任务获得。
- Gemini 用 `generationConfig.imageConfig = {aspectRatio, imageSize}`，`imageSize` 为大写 `1K / 2K / 4K`。
- 参考图长边 >2048 或 >4MB 时先压缩再上传。
- 路由依据是槽位**实际绑定**的参考图数量，而不是策划时的模式字段。
- LLM Key 只用于文本，Image Key 只用于画图；Key 只从环境变量读取，日志与诊断结果中只显示打码后的 Key。旧变量名 `RIGHTAPI_OPENAI_IMAGE_KEY` / `RIGHTAPI_GEMINI_IMAGE_KEY` 仅作读取兼容。
- 「🩺 连通性诊断」（`app/engine/diagnostics.py`）：LLM 发一条极短消息；画图两个渠道分别用各自的 Key 查询一个不存在的任务 `GET /v1/tasks/{随机id}` 来验证，不提交画图任务、不消耗额度。

### 5.4 视频：MiniMax H3 原生音画一体

- H3 生成的视频**原生自带声音**。本项目不设计画外音（Voiceover）轨道，数据模型中不出现 voiceover 字段。
- 每个分镜 `StoryboardShot` 的声音字段：
  - `audio_mode`: `native_dialogue` | `ambient_sfx` | `dialogue_and_ambient` | `silent`
  - `character_dialogue`: 镜头内人物同期声对白 + 语气描述（含对白的模式必填）
  - `ambient_sound_description`: 原生环境音与物理动作音效（含环境音的模式必填）
- 防变形链路：
  `选类型/比例 + 选填想法 → 编剧 Skill 生成分镜剧本 → 用户编辑并确认剧本 → 生成首帧图（比例强制与视频比例一致）→ 用户勾选确认首帧 → 生成视频（未编写 H3 提示词时自动先编写）`
- 未确认剧本不能生成首帧；未确认首帧不能编译 H3 提示词或生成视频。
- 视频统一由 Modal MiniMax H3 直接生成（已移除 ComfyUI 人工协同）。Step 3 两种方式：① 逐个分镜依次生成（全部已锁定首帧的分镜，可跳过已有视频的分镜）；② 选分镜生成（勾选若干分镜）。多个分镜在同一事件里串行排队（`concurrency_limit=1`，与服务端单容器一致），单个分镜失败不影响后续分镜。
- 视频版本：同一分镜重复生成时追加到 `videos`（文件名带时间戳、每个分镜单独目录），绝不覆盖；界面每个分镜一个独立播放器显示最新版本，旧版本在「全部视频版本」画廊中。

### 5.4.1 Step 4：剪辑大师一键成片与草稿导出

- 调研结论（2026-10）：GitHub 上没有现成的「带货短视频剪辑决策」Skill（现有的 ffmpeg / Remotion 类 Skill 只负责执行剪辑，不负责决策，或依赖 Node），所以剪辑决策用自写卡带 `config/skills/video_editor/default_video_editor_skill.md`，渲染与导出用纯 Python + ffmpeg 实现。
- 流程：`Step 3 生成分镜视频 → 生成剪辑方案（EditPlan）→ 表格微调 → 一键成片 mp4 / 导出剪映草稿 / 导出达芬奇工程`。剪辑方案不需要「确认」状态，可随时重新生成；重新生成时沿用字幕、花字、背景音乐等成片选项。
- 素材：每个分镜的**最新一版**视频，按剧本顺序；没有视频的分镜不加入剪辑。分镜视频更新后，状态栏提示重新生成方案（`stale_shots()`）。
- 台词定位：`edit_engine.analyze_media()` 用 ffmpeg `silencedetect`（-30 dB / 0.3 秒）找出有声段落，因为 H3 的台词常常不从第 0 秒开始，按剧本计划时长硬切会剪断台词。`assemble_plan()` 强制：入点不晚于台词开头、出点不早于台词结尾，钩子镜头排第一，未知转场改硬切，花字去违禁词并截到 ≤14 字。环境音贯穿全片、无法定位台词时，按 4.5 字 / 秒的语速估算。
- 字幕：台词字幕直接取自 `character_dialogue` 引号内的原文（`spoken_text()`），不做语音识别；按标点切成每条 ≤14 字，在台词时间窗内按字数分配时长。
- 时间轴（`app/schemas/edit.py` 的 `build_timeline()`）：统一 24 fps 整数帧（与 H3 输出一致，不做帧率转换），片段首尾相接；转场借用出点之后、入点之前的余量画面，**不改变总时长**；余量不足时自动缩短，完全没有余量时退化为硬切。成片、剪映草稿、达芬奇时间线三者共用这条时间轴。
- 一键成片（`edit_render.py`）：ffmpeg 一条滤镜图完成缩放裁切、concat / xfade + acrossfade 转场、ASS 字幕与花字（libass，按字数自动缩字号，竖屏字幕在下方约 1/5 处、花字在上方约 1/8 处，避开平台按钮区）、背景音乐循环、说话时 sidechaincompress 自动压低音乐，并用 loudnorm 统一到 -14 LUFS。ffmpeg 在输出目录运行，滤镜里只用相对文件名，避开 Windows 盘符冒号的转义问题。输出追加到 `EditPlan.renders`，不覆盖旧成片。
- 剪映草稿（`draft_export.write_jianying_draft()`，依赖 `pyJianYingDraft`，Apache-2.0）：每次导出都新建一个草稿，绝不覆盖同名草稿；剪映打开后会把草稿转存为自己的格式，所以不回读修改旧草稿。素材复制进草稿文件夹 `materials/`；`draft_content.json` 同时复制为 `draft_info.json`（新版剪映读取后者）；`draft_meta_info.json` 换新的 `draft_id` 并补上名称与时长。草稿文件夹由 `JIANYING_DRAFT_DIR` 指定，留空时自动查找 `%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft`（或 CapCut 同名目录），都没有时只打包 zip 供下载。
- 达芬奇工程（`draft_export.export_resolve()`）：手写 FCPXML 1.10（Resolve 18 起都能导入）+ OTIO JSON（`Clip.1`，Resolve 18.5+）+ 台词字幕 / 卖点花字 SRT（UTF-8 BOM）+ `media/` 素材（ASCII 文件名）+ 中文导入说明。不依赖 `opentimelineio` 包（没有 Python 3.14 wheel），也不用 Resolve 脚本 API（免费版自 19.1 起不支持外部脚本）。时间线 `tcStart="0s"`，与 SRT 时间对齐。
- 已验证：真实 H3 片段的成片渲染、草稿文件结构与时间值。**未在真实的剪映 / DaVinci Resolve 中打开验证**（开发机未安装），首次使用时需要人工确认能正常导入。

### 5.4.2 【配音】选项卡：VoxCPM2 口播配音

- 配音是**独立成品**（wav / mp3 / srt / zip），不写入视频分镜、不进剪辑时间线：§5.4 的「不设画外音」约束不变，`StoryboardShot` 仍然没有 voiceover 字段。运营可把 mp3 拖进剪映 / 达芬奇自行使用。
- 写法依据 VoxCPM2 官方《最佳实践》（https://voxcpm.readthedocs.io/zh-cn/latest/cookbook.html ），整理在 `config/skills/voice_writer/references/voxcpm2_best_practices.md`，由 `voice_writer` 卡带附带：
  - 风格指令写在正文前的括号里，服务端拼成 `(指令)正文`，所以指令里的括号一律去掉（`clean_control()`）；方言只在指令前加方言名（`LANGUAGES` 第二列），正文要用地道方言书写；外语不加语言标签。
  - 非语言标签只用官方推荐的 11 个（`RECOMMENDED_TAGS`，大小写照抄）；多音字写成「重{chong2}」，`tts_text()` 发给模型时换成 `{chong2}`，`display_text()` 给字幕时保留原字。
  - 正文里的「（笑）」这类括号会被读出来：段首括号自动搬进「本段风格」（`split_leading_note()`），其他位置的括号由 `segment_warnings()` 提醒。
- 四步：① 文案（AI 撰写 / 粘贴按句分段 `split_script()` / 导入视频剧本台词 `spoken_text()`，表格逐段编辑）→ ② 音色 → ③ 逐段生成 → ④ 合成完整配音。
- 分段：官方建议长文切短段分别合成再拼接。每段约 8~40「字」，按 `spoken_len()` 计算：汉字 1、数字 1.5、外文单词 1.7。太短的句子并入相邻句。
- 音色一致：不给参考音频时 VoxCPM2 每次随机一个声音。
  - 声音设计：先试听，再「锁定」样音，之后每段都以样音为参考音频（默认高保真克隆）。没锁定就开始生成时，自动先生成一段样音并锁定。
  - 克隆：参考音频转成 16kHz 单声道，最长截取 30 秒；逐字文本可自动识别，但要人工核对（实测 SenseVoice 把「莓茶」识别成「梅茶」）。
  - 音色库：跨项目共用。
  - 已生成的段落记录 `voice_key`，换了音色或改了文案，「生成未完成的段落」会自动重做。
- 版本：每段重复生成时追加 `takes`（文件名带时间戳），绝不覆盖，可在版本间切换（`active_take_id`）；同一段重做时种子自动 +1，否则结果完全相同。完整配音每次新建 `VoiceMix`。
- 拼接（`voice_engine.mix()`）：每段去首尾静音，再加「段后停顿」；ffmpeg loudnorm 统一到 -16 LUFS（口播常用值，后期叠背景音乐有余量），导出 wav、mp3（libmp3lame 192k）、srt（UTF-8 BOM，每段字幕在该段的有声时间内按字数分配），zip 里含分段音频与配音文案。
- 数字读法：VoxCPM 自带的整句规范化有两个问题（2026-10-23 实测）：会跨逗号把数字连起来（「买2送1，24小时」→「买二送一百二十四小时」），也会改坏拼音（「{chong2}」→「{chong二}」）。所以服务端 `normalize_text()` 按小句分别规范化，原样保留 `{…}` 与 `[标签]`，参考文本也一起规范化；「🔢 预览读法」调用服务端只用 CPU 的 `preview_normalize`，不启动 GPU。

### 5.4.3 VoiceProvider 与 Modal VoxCPM2 服务

- 统一接口 `app/providers/voice/base.py`：`synthesize(VoiceRequest) -> wav bytes`、`transcribe(audio) -> str`、`warmup()`（只查找，不启动 GPU）、`normalize_preview()`。
- 实现：`ModalVoxCPMProvider` 与 `DemoVoiceProvider`。后者本地纯 Python 合成「按字发声」的提示音，不是真实人声；同一音色描述 / 参考音频得到同一音高。
- 服务端 `modal/voxcpm_service.py`（属于 `modal/`，不入 git）：
  - 应用 `voxcpm2-tts-service`，类 `VoxCPM2Service`；A10G，权重在卷 `voxcpm2-weights` 上（VoxCPM2 + ZipEnhancer 降噪 + SenseVoice 识别），VoxCPM 源码固定到某个 commit。
  - 开启 torch.compile。不开 `@modal.concurrent`：模型是 batch=1 的静态 KV 缓存，而且 CUDA Graph 要求在 `@modal.enter` 的同一线程执行。
  - 实测：冷启动约 2 分钟，热态约 1 倍实时（2~3 秒音频用 2.5~3.5 秒）。
- 调用约定：
  - `reference_audio` 是参考音频字节，`prompt_text` 非空时走高保真克隆（同一段音频同时作 prompt 与 reference），此时风格指令不生效。
  - 返回 48kHz 单声道 16-bit wav。
  - 配置项：`MODAL_VOXCPM_APP` / `MODAL_VOXCPM_CLASS` / `MODAL_VOXCPM_TIMEOUT_SECONDS`。
- 已验证：真实服务的声音设计、高保真克隆、识别、读法预览。演示模式下跑通了端到端全流程。**真实服务的整条配音拼接还没有人工试听验收。**

### 5.4.4 【动效广告】选项卡：零成本代码动效（Motion Graphics）

- 定位：用项目里已经产出的素材做一条按节拍剪辑的带货动效广告。**只有「编排」调用 LLM**（`motion_director` 卡带，未填 Key 走演示 `app/providers/demo/motion.py`）；画面、配乐、音效全部由本机代码渲染（Pillow 逐帧 + numpy 合成音频 + ffmpeg 编码），不调用任何生图 / 生视频接口。
- 动效法则：整理自 LottieFiles/motion-design-skill、heygen-com/hyperframes、whaleyxbt/claude-motion、haidrrrry/claude-remotion-skill、howseen-ai/claude-motion-design、charlie947/motion-graphics-skills（MIT / Apache-2.0），只引用规则与数值，写在 `config/skills/motion_director/references/motion_design_principles.md`。引擎自动执行，卡带只决定结构、文案、素材、拍数、镜头与转场。
- 数据模型 `app/schemas/motion.py`：`MotionAd`（比例 / BPM / 主题 / 分镜 / 素材 / 渲染记录）、`MotionScene`（7 类：hook 钩子大字、hero 商品主视觉、features 卖点清单、stat 数据冲击、gallery 图集、showcase 长图滚动、cta 收尾行动）。分镜长度是**整数拍**（1 拍 = 60 / BPM 秒），所以每个剪辑点都落在拍点上。文字上限：主标题 ≤16、副标题 ≤28、条目 ≤14、按钮 ≤8、数字 / 胶囊 ≤12；强调词必须是主标题原文的一部分。阅读时间按每秒 8 个字（外文单词 / 数字算 1.5 字，副标题按一半）+ 0.6 秒入场。
- 素材（`MotionEngine.collect_assets()`）：主图 / 详情页定稿图（未勾选取最新）、详情页前 10 屏拼接长图（`stitch_long_image()`，按内容哈希缓存）、视频锁定首帧、分镜最新一版视频、参考图、本页上传（可标为界面截图，放进浏览器外框）。编号 A1、A2…；重新编排 / 重新收集时已有素材编号不变（`refresh_assets()`），新素材追加。发给 Skill 的素材清单只有编号、来源、形状，不含本机路径。
- 商品信息：表单没改动时保存的是占位示例（如「如：买二送一」），`real_product_info()` 把这类值过滤掉，绝不当成事实写进广告。
- Skill 输出纠正（`motion_engine.assemble_plan()`）：未知类型丢弃；文案去违禁词并截到上限；不存在的素材编号剔除，缺素材时补一个没用过的（showcase 优先长图）；拍数限制在类型范围内且至少能读完文字；钩子 ≤3 秒排第一、收尾行动 ≥3 秒排最后（缺了自动补）；未知转场（含叠化）改动势推移，色块擦除全片最多一次，转场种类 ≤3 种，最后一屏硬切。
- 渲染（`app/engine/motion/`）：`easing.py` 缓动与弹簧（入场 (0.16,1,0.3,1)、退场 (0.7,0,0.84,0)、移动 (0.65,0,0.35,1)，弹簧 ζ≥0.72，回弹全片只用一次）；`typeset.py` 中文断行（空格优先、各行尽量等宽、「·」分隔符落在断行处省略、行首不放标点）与逐字遮罩上升；`scenes.py` 七类版式（三层运动，文字落定后静止）；`renderer.py` 连续背景 + 动势衔接转场（push / zoom / wipe / cut，矢量方向一致，快速位移加运动模糊、不跨剪辑点）+ 暗角颗粒，入场略早于剪辑点；`audio.py` 按 BPM 与主题调式本机合成配乐（钩子后起势进鼓点、侧链呼吸）与音效（呼啸峰值对准剪辑点、一次起势接重击、逐条提示音按音阶升高）；`encode.py` 多线程逐帧 → ffmpeg（H.264 CRF 17、BT.709、loudnorm -14 LUFS）；`review.py` 联系表 + 11 项自检（节拍、黄金 3 秒、收尾 ≥3 秒、阅读时间、落定停留、安全区、遮挡、转场种类、第 0 帧、突跳 / 单帧闪烁、响度）。
- 档位：快速预览 540P（无颗粒、CRF 23）与 1080P 成片。每次渲染写进新的 `render_{时间}_{档位}/`，追加到 `MotionAd.renders`，绝不覆盖。渲染使用方案快照，渲染中继续改表格不影响本次结果。
- 实测（开发机 2 核）：1080P 30fps 约 6 帧 / 秒（55 秒成片约 5 分钟）；540P 预览约 30 帧 / 秒。

### 5.5 可扩展 VideoProvider

- 统一接口位于 `app/providers/video/base.py`：`warmup()`（Serverless 冷启动预热）、`generate()`、默认 600 秒超时。
- 实现：`ModalMiniMaxH3Provider`（通过 Modal SDK 调用已部署的 `minimax-h3-diffusers-service.H3BatchRunner`，见下）、`FalAIVideoProvider`（预留，暂未实现）、`DemoVideoProvider`（本地 ffmpeg 合成演示）。
- 新增视频后端只需继承 `VideoProvider` 并注册，不改业务代码。
- `ModalMiniMaxH3Provider` 调用约定（服务端源码 `modal/h3_diffusers_service.py`，属于用户自有部署脚本，本项目不修改）：
  - 按 `MODAL_H3_APP` / `MODAL_H3_CLASS` 用 `modal.Cls.from_name` 查找，凭证为本机 Modal 登录，没有 Web 端点、没有 URL。
  - 锁定首帧后统一走 `workflow="ref2va"`：`images[0]` = 锁定首帧（`<Picture 1>`），其后为该分镜绑定的商品参考图（`<Picture 2>`…，总数 ≤9）；官方卡带提示词原样发送（自带 `<Picture N>` 定义），`i2va` 只传首帧、`ref2va` 传首帧 + 参考图；旧版卡带提示词前自动声明各 Picture 的角色。
  - 帧数 = 时长 × 24fps，向上取到 17n+5，范围 124~345（约 5.2~14.4 秒；短于 5.2 秒的分镜会被拉长到 5.2 秒）。
  - `aspect_ratio` 直接传剧本比例；同一剧本使用同一 seed。
  - 「检查服务」只做 `hydrate()` 查找，不启动 GPU。

### 5.6 真实 API / 零 Key 演示双模式

- 每个通道（`llm` / `image` / `image_std` / `modal_h3` / `modal_voxcpm`）独立判定：填写了 Key / 地址 → 真实接口；未填写 → 演示兜底。判定入口统一为 `EngineRuntime.is_live(channel)`。
- `DEMO_MODE=true` 或界面「本地演示测试模式」开关可强制全部走演示。
- 演示模式输出的 JSON 结构与真实 Skill 输出**完全一致**（`app/providers/demo/content.py`），后续编辑 / 确认 / 渲染流程无差别；演示图由 PIL 绘制真实中文排版并合成参考图缩略图，右上角带「DEMO 演示预览」角标。
- LLM 同时兼容 `/v1/messages`（Anthropic 原生）与 `/v1/chat/completions`（OpenAI 兼容），`RIGHTAPI_LLM_API_FORMAT=auto` 时自动探测。

## 5.7 引擎分层（`app/engine/`）

| 模块 | 职责 |
|---|---|
| `runtime.py` | 持有配置、Skill 注册表、类目/平台、存储；决定真实 / 演示模式；创建各通道客户端 |
| `planner_engine.py` | 执行 `ecommerce_planner` Skill → draft 策划案；支持单槽位重生成；以参考图数量自动纠正生图模式 |
| `script_engine.py` | 执行 `video_scriptwriter` Skill → draft 分镜剧本；第 1 镜强制为 ≤3 秒钩子；画外音字段自动转为镜头内对白 |
| `prompt_compiler.py` | 执行 `image_prompt` / `video_prompt` Skill；生成每镜 H3 提示词（官方卡带自动选择 I2VA / Ref2VA） |
| `render_engine.py` | 并发生成候选图 / 首帧 / 视频，异步生成器逐条产出事件，每张图完成立即落盘 |
| `project_service.py` | 新建项目、跨模块复用参考图、定稿导出 zip、详情页长图序列 |
| `compliance.py` | 广告法 / 类目违禁词检查 |
| `edit_engine.py` | 分析分镜视频（时长、画面、有声段落）→ 执行 `video_editor` Skill → 纠正为合法的 `EditPlan` |
| `edit_render.py` | 按剪辑方案用 ffmpeg 合成成片 mp4（转场、ASS 字幕 / 花字、背景音乐避让、响度统一），实时产出进度事件 |
| `draft_export.py` | 导出剪映草稿（pyJianYingDraft）与达芬奇工程（FCPXML 1.10 / OTIO / SRT），同步 IO，UI 用 `asyncio.to_thread` 调用 |
| `motion_engine.py` | 收集项目素材 → 执行 `motion_director` Skill → 纠正为合法的 `MotionAd`；调用 `app/engine/motion/` 本机渲染动效广告（成片 + 联系表 + 自检报告） |
| `voice_engine.py` | 执行 `voice_writer` Skill → 配音文案；参考音频处理、试听 / 锁定音色、音色库；逐段调用 VoiceProvider（串行、带心跳日志）；拼接完整配音并导出 wav / mp3 / srt / zip |

## 5.8 界面（`app/ui/`）

- `header.py`：项目管理、类目/平台联动、类目专属商品信息表单 + 实时合规检查、Skill 管理、API 配置（写回 `.env`，白名单键）。
- `image_tab.py`：主图与详情页共用的三步式工作流。`video_tab.py`：视频剧本 → 首帧锁定 → Modal H3 生成视频（逐个 / 选分镜，每个分镜独立播放器），H3 提示词折叠在可选的查看 / 微调区。
- `edit_panel.py`：视频 Step 4（剪辑方案表格、成片选项、一键成片、导出剪映 / 达芬奇）。布局在 Step 3 之后，事件在运行日志创建后绑定；刷新输出独立于 `video_tab` 的 `refresh_outputs`，不改变其位置顺序（端到端测试按位置读取）。成片与导出都使用剪辑方案快照，处理过程中继续改表格不影响本次结果。
- `voice_tab.py`：【配音】四步。刷新输出的顺序由 `REFRESH_KEYS` 定义，端到端测试按名字取位置。音色设定改动后立即保存。生成类事件的 `concurrency_id` 为 `voice_gen`，`concurrency_limit=1`，与服务端单容器一致；试听、识别也走这条队列。
- `motion_tab.py`：【动效广告】三步（编排方案 / 素材 / 渲染）。刷新输出顺序由 `REFRESH_KEYS` 定义，端到端测试按名字取位置。渲染事件 `concurrency_id` 为 `motion_render`，`concurrency_limit=1`（渲染很吃 CPU）；比例、节拍、主题在保存 / 渲染时直接生效，不必重新编排。
- `theme.py`：界面主题与 CSS 唯一来源（`THEME` / `CSS`，由 `app/main.py` 传给 `launch()`）。界面代码只用 `elem_classes`（`step-card`、`terminal-log`、`info-grid`、`plan-table` 等）引用样式，不写内联样式；步骤标题用 `step_header()`。
- `pro_panel.py`：品牌栏右侧「👑 老庹 AI 工坊 · 定制行业专属卡带 / 商业版」小徽章（`pro-badge`），点开才显示面板（`pro-panel`，默认隐藏）：按卡槽统计开源 / Pro 卡带、列出已加载的 Pro 卡带与加载错误，以及三档版本与商务联系方式（`CONTACTS`，与 README「📬 商务合作」同步）。
- 浏览器端 `gr.State` 只存 `project_id`；`Project` 对象由服务端 `Workspace` 缓存并按项目加锁。
- 关键事件设置了 `api_name`，`tests/test_ui_e2e.py` 用 `gradio_client` 启动真实服务端到端驱动全流程。
- 系统代理（如 127.0.0.1:7890）会导致 Gradio 本机自检 502：`app/main.py` 启动时自动把本机地址加入 `NO_PROXY`。

## 6. 编码规范

- 代码与注释用中文或英文均可，面向用户的界面文案一律中文。
- 所有业务状态变更走模型方法（如 `add_slot` / `update_slot` / `confirm_plan`），UI 层不直接改字段，便于统一校验与落盘。
- 新功能必须附带 pytest 用例；提交前 `pytest` 与 `ruff check .` 必须零报错。
- 网络调用必须设置超时；外部返回内容视为不可信数据。
- 界面默认只监听 `127.0.0.1`。如需局域网访问须提醒：当前无登录鉴权。
- 提交信息遵循 Conventional Commits，例如 `feat(step1): ...`。

