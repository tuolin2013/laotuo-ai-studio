"""缓动曲线与弹簧：动效引擎里所有「怎么动」都从这里取，不在模板里手写曲线。

取值依据 config/skills/motion_director/references/motion_design_principles.md：
- 入场用减速（ease-out）：元素带着速度进来、轻轻落定；
- 退场用加速（ease-in）：退场时长约为入场的 50%~75%；
- 画面内移动用 in-out；只有持续漂移、旋转、颗粒可以用线性；
- 弹簧阻尼比 ζ ≥ 0.72 才不显得廉价，整支片子最多一个回弹（elastic）时刻。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable

Ease = Callable[[float], float]

_SAMPLES = 1024


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, p: float) -> float:
    return a + (b - a) * p


def progress(t: float, start: float, duration: float) -> float:
    """t 落在 [start, start + duration] 里的进度 0~1。"""
    if duration <= 0:
        return 1.0 if t >= start else 0.0
    return clamp((t - start) / duration)


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Ease:
    """CSS cubic-bezier(x1, y1, x2, y2)。x1 / x2 必须在 0~1 之间，保证 x(t) 单调。"""
    if not (0 <= x1 <= 1 and 0 <= x2 <= 1):
        raise ValueError("cubic-bezier 的 x1 / x2 必须在 0~1 之间")

    def bez(a: float, b: float, s: float) -> float:
        u = 1 - s
        return 3 * u * u * s * a + 3 * u * s * s * b + s * s * s

    xs = [bez(x1, x2, i / _SAMPLES) for i in range(_SAMPLES + 1)]
    ys = [bez(y1, y2, i / _SAMPLES) for i in range(_SAMPLES + 1)]

    def ease(p: float) -> float:
        if p <= 0:
            return 0.0
        if p >= 1:
            return 1.0
        i = bisect_left(xs, p)
        x0, x1_ = xs[i - 1], xs[i]
        f = (p - x0) / (x1_ - x0) if x1_ > x0 else 0.0
        return ys[i - 1] + (ys[i] - ys[i - 1]) * f

    return ease


def linear(p: float) -> float:
    return clamp(p)


# 入场：快进慢停（expo-out 手感），带速度落定
EASE_OUT = cubic_bezier(0.16, 1, 0.3, 1)
# 退场：慢起快走
EASE_IN = cubic_bezier(0.7, 0, 0.84, 0)
# 画面内从 A 到 B 的移动
EASE_IN_OUT = cubic_bezier(0.65, 0, 0.35, 1)
# 主角落地：约 4% 的轻微过冲，只用在最重要的一个元素上
LANDING = cubic_bezier(0.55, 0, 0.2, 1.12)


def power_in(n: float) -> Ease:
    return lambda p: clamp(p) ** n


def power_out(n: float) -> Ease:
    return lambda p: 1 - (1 - clamp(p)) ** n


# 动势衔接转场（cut-the-curve）：上一场 power4.in 加速离开，下一场 power4.out 接住速度
POWER4_IN = power_in(4)
POWER4_OUT = power_out(4)
POWER3_IN = power_in(3)


def expo_out(p: float) -> float:
    p = clamp(p)
    return 1.0 if p >= 1 else 1 - 2 ** (-10 * p)


def sine_in_out(p: float) -> float:
    """环境呼吸 / 漂移。"""
    return -(math.cos(math.pi * clamp(p)) - 1) / 2


# ---------------------------------------------------------------- 弹簧

# (stiffness, damping, mass)：k / c / m
SPRING_SNAPPY = (320.0, 30.0, 1.0)
SPRING_DEFAULT = (170.0, 26.0, 1.0)
SPRING_HEAVY = (120.0, 24.0, 1.0)
# 整支片子唯一的回弹时刻（如结尾价格胶囊），ζ≈0.4
SPRING_POP = (220.0, 10.0, 0.7)


def damping_ratio(stiffness: float, damping: float, mass: float = 1.0) -> float:
    return damping / (2 * math.sqrt(stiffness * mass))


def spring(t: float, stiffness: float = 170.0, damping: float = 26.0, mass: float = 1.0) -> float:
    """从 0 弹到 1 的阻尼弹簧位移（t 单位：秒）。欠阻尼时会过冲后回落。"""
    if t <= 0:
        return 0.0
    w0 = math.sqrt(stiffness / mass)
    zeta = damping_ratio(stiffness, damping, mass)
    if zeta < 1:
        wd = w0 * math.sqrt(1 - zeta * zeta)
        return 1 - math.exp(-zeta * w0 * t) * (math.cos(wd * t) + zeta * w0 / wd * math.sin(wd * t))
    if zeta == 1:
        return 1 - math.exp(-w0 * t) * (1 + w0 * t)
    r1 = -w0 * (zeta - math.sqrt(zeta * zeta - 1))
    r2 = -w0 * (zeta + math.sqrt(zeta * zeta - 1))
    return 1 - (r2 * math.exp(r1 * t) - r1 * math.exp(r2 * t)) / (r2 - r1)


def spring_settle_seconds(stiffness: float, damping: float, mass: float = 1.0, tolerance: float = 0.005) -> float:
    """弹簧稳定到 ±tolerance 以内所需的时间（数值求解，用于排期与自检）。"""
    t, step, last_out = 0.0, 1 / 240, 0.0
    while t < 10:
        if abs(spring(t, stiffness, damping, mass) - 1) > tolerance:
            last_out = t
        t += step
    return round(last_out + step, 3)
