import random

from moviepy import Clip, ColorClip, CompositeVideoClip, concatenate_videoclips, vfx


# FadeIn
def fadein_transition(clip: Clip, t: float) -> Clip:
    return clip.with_effects([vfx.FadeIn(t)])


# FadeOut
def fadeout_transition(clip: Clip, t: float) -> Clip:
    return clip.with_effects([vfx.FadeOut(t)])


# SlideIn
def slidein_transition(clip: Clip, t: float, side: str) -> Clip:
    width, height = clip.size

    # MoviePy 内置 SlideIn 在当前这条处理链里对全屏素材不稳定，
    # 会出现“逻辑上应用了转场，但画面几乎看不出变化”的情况。
    # 这里改成显式黑底 + 位移动画，保证转场效果可见且行为可控。
    def position(current_time: float):
        progress = min(max(current_time / max(t, 0.001), 0), 1)

        if side == "left":
            return (-width + width * progress, 0)
        if side == "right":
            return (width - width * progress, 0)
        if side == "top":
            return (0, -height + height * progress)
        if side == "bottom":
            return (0, height - height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# SlideOut
def slideout_transition(clip: Clip, t: float, side: str) -> Clip:
    width, height = clip.size
    transition_start = max(clip.duration - t, 0)

    # SlideOut 同样改成显式位移，保证片段末尾能稳定滑出画面。
    def position(current_time: float):
        if current_time <= transition_start:
            return (0, 0)

        progress = min(
            max((current_time - transition_start) / max(t, 0.001), 0), 1
        )

        if side == "left":
            return (-width * progress, 0)
        if side == "right":
            return (width * progress, 0)
        if side == "top":
            return (0, -height * progress)
        if side == "bottom":
            return (0, height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# ---------------------------------------------------------------------------
# "快速紧张" 风格转场:只用在 1~2 个位置,时长 0.15~0.25s 量级,不是常规的
# 1s+ crossfade。其余片段一律硬切(不调用这些函数)。
# ---------------------------------------------------------------------------

# QuickZoom — 快速 punch-in:片段开头从放大状态快速收回到正常大小
def quick_zoom_transition(clip: Clip, t: float) -> Clip:
    width, height = clip.size

    def scale(current_time: float):
        progress = min(max(current_time / max(t, 0.001), 0), 1)
        # 从 1.15 倍快速收回到 1.0 倍，制造冲击感；和 slidein/slideout 一样
        # 用固定尺寸的 CompositeVideoClip 兜底，避免放大后画面超出画布。
        return 1.15 - 0.15 * progress

    zoomed = clip.resized(scale).with_position("center")
    return CompositeVideoClip([zoomed], size=(width, height)).with_duration(clip.duration)


# Whip — 快速甩入，复用 slidein 的位移逻辑，只是时长极短(0.15~0.25s 而不是 1s)
def whip_pan_transition(clip: Clip, t: float, side: str) -> Clip:
    return slidein_transition(clip, t, side)


# Flash — 片段开头插入一段极短纯白闪光帧，模拟快速剪辑里的闪白转场。
# 不用 alpha 渐隐(MoviePy 这个版本的 with_opacity 不接受随时间变化的回调，
# 强行做容易在合成阶段出问题)，改用最朴素也最稳的手法:插入几帧纯白再接
# 正常画面，效果和专业剪辑里的"闪白卡点"一致。
def white_flash_transition(clip: Clip, t: float) -> Clip:
    width, height = clip.size
    flash = ColorClip(size=(width, height), color=(255, 255, 255)).with_duration(t)
    return concatenate_videoclips([flash, clip])


# ---------------------------------------------------------------------------
# 炫富混剪"夸张特效"(montage flashy):每刀都要很冲。比新闻的温和转场狠得多。
# ---------------------------------------------------------------------------

def continuous_push(clip: Clip, zoom_end: float = 1.12) -> Clip:
    """整段缓慢推进(Ken Burns push),让画面全程不静止。"""
    width, height = clip.size
    dur = clip.duration

    def scale(t: float):
        p = min(max(t / max(dur, 0.001), 0), 1)
        return 1.0 + (zoom_end - 1.0) * p

    return CompositeVideoClip(
        [clip.resized(scale).with_position("center")], size=(width, height)
    ).with_duration(dur)


def zoom_punch_strong(clip: Clip, t: float = 0.18, start_scale: float = 1.45) -> Clip:
    """强力 punch-in:开头从大幅放大快速收回,冲击感比 quick_zoom 强很多。"""
    width, height = clip.size

    def scale(tt: float):
        p = min(max(tt / max(t, 0.001), 0), 1)
        return start_scale - (start_scale - 1.0) * p

    return CompositeVideoClip(
        [clip.resized(scale).with_position("center")], size=(width, height)
    ).with_duration(clip.duration)


def shake_transition(clip: Clip, t: float = 0.2, intensity: int = 36) -> Clip:
    """开头一小段画面抖动(高能感)。先放大盖住边缘,避免抖出黑边。"""
    width, height = clip.size

    def pos(tt: float):
        if tt < t:
            return (random.randint(-intensity, intensity), random.randint(-intensity, intensity))
        return (0, 0)

    return CompositeVideoClip(
        [clip.resized(1.12).with_position(pos)], size=(width, height)
    ).with_duration(clip.duration)


def montage_flashy_effect(clip: Clip) -> Clip:
    """炫富混剪逐刀特效:整段缓慢推进 + 随机一个强力入场(冲击/抖动/闪白/甩切)。"""
    clip = continuous_push(clip, zoom_end=random.uniform(1.08, 1.16))
    kind = random.choice(["punch", "punch", "flash", "shake", "whip"])
    if kind == "punch":
        return zoom_punch_strong(clip, t=random.uniform(0.14, 0.22),
                                 start_scale=random.uniform(1.35, 1.5))
    if kind == "flash":
        return white_flash_transition(clip, t=0.06)
    if kind == "shake":
        return shake_transition(clip, t=0.18)
    return whip_pan_transition(clip, 0.15, random.choice(["left", "right", "top", "bottom"]))
