# -*- coding: utf-8 -*-
"""
音纹分析:在一首 BGM 里找出"最紧张/最激烈"的一段并裁出来。

做法(无额外依赖,只用 ffmpeg 解码 + numpy):
  1. ffmpeg 把整首歌解码成 单声道 / 8kHz / float32 裸 PCM。
  2. 以 0.1s 为帧计算两条包络:
       - loud  = 每帧 RMS(响度/能量)
       - bright= 每帧"一阶差分"的 RMS(高频/瞬态密度,鼓点/弦乐越密越大)
     两条各自归一化后相加 = intensity(紧张度)。响度大且亮/密的段落得分最高。
  3. 用目标窗口长度在 intensity 上滑窗求和,取最大的那一段 = 最紧张段。
  4. ffmpeg 从原曲裁出 [start, start+window],首尾各加一点淡入淡出避免硬切。

返回选中段的起始秒数(供日志);裁好的文件写到 out_mp3。
"""
import subprocess
import sys

sys.path.insert(0, ".")
from app.utils import utils


def _ffmpeg() -> str:
    return utils.get_ffmpeg_binary()


def most_tense_segment(src_audio: str, out_mp3: str, window: float = 30.0) -> float:
    import numpy as np

    sr = 8000
    # 解码成单声道 8k float32 裸流
    p = subprocess.run(
        [_ffmpeg(), "-v", "error", "-i", src_audio, "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"],
        capture_output=True,
    )
    data = np.frombuffer(p.stdout, dtype=np.float32)
    if data.size == 0:
        raise RuntimeError("ffmpeg 解码音频为空,无法做音纹分析")

    total = data.size / sr
    window = float(min(window, total))

    hop = int(sr * 0.1)                 # 0.1s 一帧
    n = data.size // hop
    if n < 2:
        # 太短,直接整首
        _cut(src_audio, 0.0, window, out_mp3)
        return 0.0

    frames = data[: n * hop].reshape(n, hop)
    loud = np.sqrt(np.mean(frames * frames, axis=1) + 1e-9)
    diff = np.diff(frames, axis=1)
    bright = np.sqrt(np.mean(diff * diff, axis=1) + 1e-9)

    def norm(x):
        rng = x.max() - x.min()
        return (x - x.min()) / rng if rng > 1e-12 else x * 0.0

    intensity = norm(loud) + norm(bright)   # 紧张度 = 响度 + 高频/瞬态

    fps = 10                                 # 每秒 10 帧
    win_frames = int(window * fps)
    if win_frames >= intensity.size:
        start = 0.0
    else:
        csum = np.cumsum(np.insert(intensity, 0, 0.0))
        sums = csum[win_frames:] - csum[:-win_frames]
        start = float(np.argmax(sums)) / fps

    _cut(src_audio, start, window, out_mp3)
    return start


def _cut(src_audio: str, start: float, window: float, out_mp3: str):
    fade_out_st = max(0.0, window - 0.8)
    af = f"afade=t=in:st=0:d=0.5,afade=t=out:st={fade_out_st:.2f}:d=0.8"
    subprocess.run(
        [_ffmpeg(), "-y", "-ss", f"{start:.3f}", "-i", src_audio,
         "-t", f"{window:.3f}", "-af", af, out_mp3],
        check=True, capture_output=True,
    )


if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    win = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    s = most_tense_segment(src, out, win)
    print(f"most tense segment start={s:.1f}s window={win}s -> {out}")
