# 3 分钟上线：免 Key 公共在线演示

两部分：

- **后端**（跑工作台本身，访客不用填 Key，所有生成都走演示模式）：
  - 推荐 **Modal CPU 容器**（`modal/public_demo_service.py`）：一条 `modal deploy` 得到 HTTPS 网址，闲置 5 分钟自动缩到 0；
  - 备用 **Hugging Face Docker Space**（`deploy/hf_space/`）：新建 Docker Space 需要 HF PRO。
- **Vercel 门面页**（`deploy/vercel-landing/`，纯静态）：宣传片 + 内嵌在线体验，访客打开一个网址就能看片、上手操作。

```
访客 ──► Vercel 门面页（deploy/vercel-landing/）
            ├─ 🎬 宣传片：promo.mp4（封面 promo-poster.jpg）
            └─ 🚀 在线体验：<iframe> ──► Modal：https://tuolin2011--laotuo-public-demo.us-east.modal.run
                                          （备用：Hugging Face Space）
```

下文命令都在项目根目录的 Windows Git Bash 里执行。

## 上线前先看

- **Modal 费用**：Starter 套餐每月送 $30 额度，容器开着时按秒计费。脚本预留 1 个物理核（= 2 vCPU）和 8GB 内存，
  约 $0.11 / 小时；渲染时 CPU 最多用到 2 核，按实际用量计。闲置 5 分钟缩到 0，不计费。$30 大约够容器开 200 多个小时。
  建议在 Modal 后台 **Settings → Usage & Billing** 设一个每月额度上限（Workspace budget），到了上限自动停。
- **`modal/` 不在开源仓库里**：部署脚本只在本机和私人备份仓库（`scripts/sync_private_backup.py`）。
- **Vercel 免费版（Hobby）只允许非商业的个人使用**，商业用途要 Pro 套餐。
- **国内访问**：`*.modal.run`、`*.vercel.app`、`*.hf.space` 在国内网络下可能打不开或很慢（没有在国内网络测过）。
  面向国内客户时，建议给门面页绑定自己的域名；Modal 的自定义域名需要 Team 套餐。
- **没有登录鉴权**：知道网址的人都能用。公共演示模式不读取任何 Key、不调用付费接口，花的只是容器运行费。

## 第 1 步：部署后端（Modal）

1. 确认本机已登录 Modal（输出工作区名，例如 `tuolin2011`；没有登录先运行 `.venv/Scripts/modal setup`）：

   ```bash
   .venv/Scripts/modal profile current
   ```

2. 部署：

   ```bash
   .venv/Scripts/modal deploy modal/public_demo_service.py
   ```

   首次部署要构建镜像（装 ffmpeg、中文字体和 Python 依赖），几分钟。完成后输出里的网址形如
   `https://<工作区>--laotuo-public-demo.us-east.modal.run`（`us-east` 是默认地区），工作区 `tuolin2011` 就是
   <https://tuolin2011--laotuo-public-demo.us-east.modal.run>。换了工作区，要同步改 `deploy/vercel-landing/index.html`
   里的两处网址（全文搜索 `laotuo-public-demo`）。

3. 打开网址检查（第一次打开要等容器启动、绘制样板项目）：
   - 顶部显示「🌐 公共免 Key 在线演示」，没有「⚙️ API 密钥与接口配置」；
   - 项目信息里已经有一份「张家界莓茶」示例项目；
   - 切到「✨ 动效广告」，点「🎬 AI 编排动效方案」，再点「⚡ 快速预览（540P）」，能出成片和自检报告，中文字不乱码。

上传到镜像的是本机磁盘上的 `app/`、`config/`、`pyproject.toml`、`LICENSE`（没提交的改动也会上线），
不含 `.env`、`modal/`、`tests/`、`workspace/`、Pro 卡带包和 `config/categories/pet_herbal_otc.json`。

其他常用命令：

```bash
.venv/Scripts/modal app logs laotuo-public-demo     # 看运行日志
.venv/Scripts/modal app stop laotuo-public-demo     # 下线（网址随即失效）
.venv/Scripts/modal serve modal/public_demo_service.py   # 临时试跑，关掉命令即停止，网址多一个 -dev 后缀
```

## 第 2 步：提交并推送到 GitHub

Vercel 从 GitHub 读取门面页，所以改动要先提交、推送：

```bash
.venv/Scripts/python scripts/sync_private_backup.py   # 公开仓 origin（不含 modal/）+ 私人全量备份 private（含 modal/）
```

`deploy/vercel-landing/promo.mp4` 是压缩过的 1080P 宣传片（约 8.7MB，62 秒），已经提交进仓库，
会永久留在公开仓库的历史里。

## 第 3 步：Vercel 一键部署门面页

1. 打开 <https://vercel.com/new>，用 GitHub 登录，**Import** 仓库 `tuolin2013/laotuo-ai-studio`。
2. 配置页只改两项：
   - **Root Directory**：点 Edit，选 `deploy/vercel-landing`；
   - **Framework Preset**：`Other`（纯静态页，没有构建命令）。
3. 点 **Deploy**。几十秒后得到 `https://<项目名>.vercel.app`。

之后每次往 GitHub 的 `main` 推送，Vercel 都会自动重新部署。绑定自己的域名在项目 **Settings → Domains**。

宣传片放在别处：把视频传到你的 CDN / 对象存储，把 `index.html` 里 `data-src="promo.mp4"` 改成完整网址即可。

## 备用：Hugging Face Space

Hugging Face 官方文档 2026-07 起写明，新建 Gradio / Docker 这类跑在算力上的 Space 需要 PRO（个人）或
Team / Enterprise（组织）；CPU basic 硬件本身不按小时收费。用 Space 时，把 `index.html` 里的两处 Modal 网址换成
`https://<用户名>-<Space 名>.hf.space`。

1. 打开 <https://huggingface.co/settings/tokens>，新建一个 **Write** 权限的 Token，登录（Token 会存进 git 凭据）：

   ```bash
   .venv/Scripts/hf auth login --token <你的 Token> --add-to-git-credential
   ```

2. 创建 Space（也可以在网页 <https://huggingface.co/new-space> 上选 Docker → Blank → CPU basic → Public）：

   ```bash
   .venv/Scripts/hf repos create tuolin2013/laotuo-ai-studio --type space --sdk docker --public
   ```

3. 推送。Space 仓库根目录必须有 `Dockerfile`，`README.md` 必须带 Space 的配置头（`sdk: docker`、`app_port: 7860`）。
   `scripts/push_hf_space.py` 把 `deploy/hf_space/` 里的这几个文件放到根目录，再加上 `master` 已提交的 `app/`、
   `config/`、`pyproject.toml`、`LICENSE` 生成一个提交，然后 git push 到 Space。它不切换分支，不碰工作区和暂存区，
   推送前会检查密钥。

   ```bash
   git remote add hf https://huggingface.co/spaces/tuolin2013/laotuo-ai-studio
   .venv/Scripts/python scripts/push_hf_space.py --dry-run   # 先看看要推哪些文件
   .venv/Scripts/python scripts/push_hf_space.py
   ```

   推完 Space 自动构建，首次约 5~10 分钟，构建日志在 Space 页面的 **Logs**。

## 以后更新

```bash
# 改完代码、提交之后
.venv/Scripts/python scripts/sync_private_backup.py        # GitHub（Vercel 门面页随之更新）+ 私人备份
.venv/Scripts/modal deploy modal/public_demo_service.py    # 在线体验更新
# 用 Hugging Face Space 时改为：.venv/Scripts/python scripts/push_hf_space.py
```

## 公共演示模式做了什么

Modal 脚本和 Dockerfile 都设置了 `LAOTUO_PUBLIC_DEMO=1`（代码在 `app/engine/public_demo.py`）：

| 项目 | 做法 |
|---|---|
| Key | 一律走演示模式，环境变量里有 Key 也不读取；不显示 API 配置面板，不能写 `.env`，不能做连通性诊断 |
| 示例素材 | 启动时用演示模式绘制一套张家界莓茶主图（4 张）与详情页（4 屏），每位访客打开页面复制一份自己的项目 |
| 隐私 | 访客之间互相看不到项目；不显示历史项目、不提供共用的音色库；卡带指令只能查看、不能临时覆盖 |
| 动效时长 | 单条最长 30 秒，超过时自动减中间几屏的拍数（仍是整数拍），不够再删中间的屏 |
| 排队 | 动效渲染同一时间只跑 1 条，排队的最多 5 条，满了提示稍后再试；整个事件队列上限 120，上传文件单个 ≤20MB |
| 磁盘 | 超过 1 小时没有操作的访客项目整个删除，还在用的项目里过期的成片和单屏预览也删除；最多同时保留 200 份访客项目 |

Modal 只开 1 个容器：访客项目和渲染排队都在这个容器的内存与磁盘里，多开容器访客会被分到看不到自己项目的容器上。

本机自测公共演示模式（不用 Modal / Docker）：

```bash
LAOTUO_PUBLIC_DEMO=1 .venv/Scripts/python -m app.main
```

数据放在 `workspace/public_demo/`，和本机的真实项目分开，清理只在这里进行。

## 常见问题

- **打开在线体验等了很久**：Modal 闲置 5 分钟缩到 0，下一位访客打开时重新启动容器、绘制样板项目；
  Hugging Face 的 CPU basic 硬件 48 小时没人访问会休眠，唤醒要一两分钟。
- **重启后示例项目没了**：容器的磁盘不持久保存，重启后重新生成样板项目，访客项目清空。这正是公共演示需要的。
- **门面页里的在线体验是空白**：先单独打开后端网址，确认已启动；再确认 `index.html` 里的地址与 `modal deploy` 输出的一致。
- **要等多久**（2026-10-29 实测）：部署后第一次打开约 4 分钟（拉取镜像），之后从缩容状态唤醒约 1 分钟；
  30 秒的快速预览（540P）约 20 秒，1080P 成片约 80 秒。从国内网络访问时连接偶尔会被重置，刷新一下即可。
- **账单比预想的高**：在 Modal 后台看 `laotuo-public-demo` 的运行时长。浏览器标签页开着时页面和服务器之间保持长连接，
  容器可能不会缩容；不需要时用 `modal app stop laotuo-public-demo` 下线。
- **推送 Space 时提示要输入密码**：重新执行「备用：Hugging Face Space」第 1 步的 `hf auth login ... --add-to-git-credential`。
