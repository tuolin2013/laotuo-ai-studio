# 老庹 AI 电商内容工厂 · Hugging Face Spaces 公共免 Key 在线演示（Docker SDK）
#
# 构建上下文是仓库根目录：
#   - 推到 Space 时由 scripts/push_hf_space.py 把本文件放到 Space 仓库根目录；
#   - 本机自测：docker build -f deploy/hf_space/Dockerfile -t laotuo-demo .
#               docker run --rm -p 7860:7860 laotuo-demo
FROM python:3.11-slim

# ffmpeg：动效广告编码、演示视频与一键成片；fonts-noto-cjk：思源黑体 / 宋体（简体中文字形），
# 动效大字、副标题与字幕不乱码。代码按文件名取 Noto CJK 字体集合里的简体中文（SC）字形
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces 用 uid 1000 运行容器：先建用户，再以该用户复制文件、安装依赖
RUN useradd -m -u 1000 user
USER user
# LAOTUO_PUBLIC_DEMO=1：公共免 Key 演示，锁定演示模式、不读取任何 Key、隐藏 API 配置（见 app/engine/public_demo.py）；
# IMAGEIO_FFMPEG_EXE：用上面 apt 安装的系统 ffmpeg，不用 imageio-ffmpeg 自带的二进制
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LAOTUO_PUBLIC_DEMO=1 \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860 \
    GRADIO_ANALYTICS_ENABLED=False \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg
# 目录由 user 自己创建：pip 安装要在这里写元数据，运行时要在 workspace/ 里写项目文件
RUN mkdir -p $HOME/app/workspace
WORKDIR $HOME/app

# 依赖版本全部锁定在 pyproject.toml；先只复制依赖声明与代码，改文档时可以复用这一层缓存
COPY --chown=user pyproject.toml ./
COPY --chown=user app ./app
RUN pip install --user -e .

COPY --chown=user . .

EXPOSE 7860
CMD ["python", "-m", "app.main"]
