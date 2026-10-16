"""MiniMax H3 冒烟测试：调用已部署在 Modal 上的 minimax-h3-diffusers-service，生成一段示例视频。

用法（需要本机已登录 Modal，即存在 ~/.modal.toml）：
    py scripts/h3_smoke_test.py                 # 默认 t2va，9:16，124 帧（约 5 秒）
    py scripts/h3_smoke_test.py --ref a.png     # 带参考图走 ref2va

注意：会占用 Modal GPU 并产生费用（冷启动加载权重通常需要数分钟）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

APP_NAME = "minimax-h3-diffusers-service"
CLS_NAME = "H3BatchRunner"
OUT_DIR = Path(__file__).resolve().parents[1] / "workspace" / "h3_test"

DEFAULT_PROMPT = (
    "[Macro static] 透明玻璃杯放在原木桌面上，杯中是张家界莓茶干茶，叶面覆盖细密白霜，侧逆光。"
    "沸水从画面上方缓缓注入玻璃杯，干茶在水流中翻滚舒展，白霜慢慢溶开，杯中泛起细密气泡，"
    "汤色由浅渐变为清亮的黄绿色，蒸汽轻轻升起。"
    "声音：沸水注入玻璃杯的清亮水流声，随后杯壁轻响；背景是山林清晨轻微的鸟鸣。无人声，无背景音乐。"
    "画面比例 9:16，写实商业广告质感，无字幕，无水印。"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--ref", action="append", default=[], help="参考图路径，可多次传入（走 ref2va）")
    ap.add_argument("--frames", type=int, default=124, help="帧数，24fps；H3 允许 124~345")
    ap.add_argument("--ratio", default="9:16")
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20261016)
    args = ap.parse_args()

    import modal

    workflow = "ref2va" if args.ref else "t2va"
    images = [Path(p).read_bytes() for p in args.ref]
    runner = modal.Cls.from_name(APP_NAME, CLS_NAME)(workflow=workflow)

    print(f">>> 调用 {APP_NAME}.{CLS_NAME}(workflow={workflow}) | {args.ratio} | {args.frames} 帧 | "
          f"{args.steps} 步 | seed={args.seed} | 参考图 {len(images)} 张", flush=True)
    print(">>> 首次调用需冷启动并加载权重，请耐心等待…", flush=True)
    t0 = time.time()
    data = runner.render_single_shot.remote(
        args.prompt, images or None, args.frames, args.seed, args.steps, args.ratio
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"h3_{workflow}_{args.ratio.replace(':', 'x')}_{int(time.time())}.mp4"
    out.write_bytes(data)
    print(f">>> 完成，用时 {time.time() - t0:.0f}s，{len(data) / 1e6:.1f} MB -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
