# -*- coding: utf-8 -*-
"""
给成片加"酷炫快速片头"(WIKIFX 品牌标题卡, whoosh 滑入→定格→滑出 + whoosh 音效)
并在结尾无缝拼接固定的 WIKIFX 片尾视频。

用法: python make_intro_outro.py <main.mp4> <intro_top> <intro_bottom> <outro.mp4> <out.mp4>
三段统一到 1080x1920 / 30fps / aac 44100 stereo 再拼,避免衔接错位。
"""
import os
import subprocess
import sys

sys.path.insert(0, ".")
from app.utils import utils

W, H = 1080, 1920
FPS = 30
FONT = os.path.abspath("resource/fonts/MicrosoftYaHeiBold.ttc")
ACCENT = "#E11D2A"      # 警示红
BRAND_YELLOW = "#F2B705"
INK = (12, 14, 18)      # 近黑底
INTRO_DUR = 1.0


def _ffmpeg() -> str:
    return utils.get_ffmpeg_binary()


def make_whoosh(out_wav: str):
    """用 ffmpeg 合成一个 ~1s 的 whoosh 音效(带通噪声 + 快进慢出包络)。"""
    cmd = [
        _ffmpeg(), "-y",
        "-f", "lavfi", "-i", "anoisesrc=d=1.0:c=pink:a=0.9",
        "-af", (
            "highpass=f=250,lowpass=f=5000,"
            "afade=t=in:st=0:d=0.12,afade=t=out:st=0.45:d=0.55,volume=2.2"
        ),
        "-ar", "44100", "-ac", "2",
        out_wav,
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def make_intro(text_top: str, text_bottom: str, whoosh_wav: str, out_path: str):
    from moviepy import ColorClip, CompositeVideoClip, TextClip, AudioFileClip

    def slide_x(t):
        # 0.18s 快速滑入(ease-out) → 0.62s 定格 → 0.20s 快速滑出(ease-in)
        t_in, t_hold, t_out = 0.18, 0.62, 0.20
        if t < t_in:
            p = t / t_in
            e = 1 - (1 - p) ** 3
            return int(W * (1 - e))
        if t < t_in + t_hold:
            return 0
        p = (t - (t_in + t_hold)) / t_out
        return int(-W * (p ** 3))

    bg = ColorClip(size=(W, H), color=INK).with_duration(INTRO_DUR)

    # method="caption" + 给足高度的文字框,避免 label 模式把字形上下裁切。
    # 文字框宽度=W,水平自动居中;放到对应 y。
    top = (TextClip(text=text_top, font=FONT, font_size=170, color="white",
                    method="caption", size=(W, 300), text_align="center",
                    stroke_color="black", stroke_width=2)
           .with_duration(INTRO_DUR).with_position((0, int(H * 0.36))))
    # 品牌黄下划线(在 WIKIFX 文字框正下方)
    line = (ColorClip(size=(560, 14), color=(242, 183, 5)).with_duration(INTRO_DUR)
            .with_position(("center", int(H * 0.36) + 300)))
    # 底部文字自适应字号:长文案(如 FOREX SCAM ALERT)缩小,避免换行/超宽。
    bottom_fs = 96 if len(text_bottom) <= 12 else 78
    bottom = (TextClip(text=text_bottom, font=FONT, font_size=bottom_fs, color=ACCENT,
                       method="caption", size=(W, 220), text_align="center",
                       stroke_color="black", stroke_width=2)
              .with_duration(INTRO_DUR).with_position((0, int(H * 0.36) + 330)))

    card = CompositeVideoClip([top, line, bottom], size=(W, H)).with_duration(INTRO_DUR)
    card = card.with_position(lambda t: (slide_x(t), 0))

    final = CompositeVideoClip([bg, card], size=(W, H)).with_duration(INTRO_DUR)
    try:
        audio = AudioFileClip(whoosh_wav).subclipped(0, min(INTRO_DUR, AudioFileClip(whoosh_wav).duration))
        final = final.with_audio(audio)
    except Exception as e:
        print("whoosh audio skipped:", e)

    final.write_videofile(
        out_path, fps=FPS, codec="libx264", audio_codec="aac",
        preset="medium", logger=None,
    )


def make_image_intro(image_path: str, text: str, whoosh_wav: str, out_path: str,
                     duration: float = 1.6, reveal_t: float = 0.55):
    """以一张图片为背景做片头,在上方空白处叠"从左到右擦入"的文字动效。
    擦入用一个从左往右长大的遮罩(mask)实现:文字逐列显现,像新闻片头标题划入。"""
    import numpy as np
    from moviepy import (ImageClip, TextClip, CompositeVideoClip, AudioFileClip,
                         VideoClip)

    bg = ImageClip(image_path).with_duration(duration)
    if tuple(bg.size) != (W, H):
        bg = bg.resized((W, H))

    txt = TextClip(text=str(text).strip().upper(), font=FONT, font_size=76,
                   color="#16233a", method="caption", size=(int(W * 0.86), 260),
                   text_align="center", stroke_color="white", stroke_width=1)
    tw, th = txt.size
    base_mask = txt.mask  # 文字本身的 alpha(只有字形不透明,背景透明)

    def mask_frame(t):
        # 擦入门控(左→右长大) × 文字本身 alpha → 只把"字"逐列显出来,不显黑底
        p = min(max(t / max(reveal_t, 0.001), 0), 1)
        gate = np.zeros((th, tw), dtype=float)
        cut = int(tw * p)
        if cut > 0:
            gate[:, :cut] = 1.0
        if base_mask is not None:
            gate = gate * base_mask.get_frame(t)
        return gate

    mask = VideoClip(frame_function=mask_frame, is_mask=True).with_duration(duration)
    txt = (txt.with_mask(mask).with_duration(duration)
           .with_position(("center", int(H * 0.20))))

    clip = CompositeVideoClip([bg, txt], size=(W, H)).with_duration(duration)
    try:
        au = AudioFileClip(whoosh_wav)
        clip = clip.with_audio(au.subclipped(0, min(duration, au.duration)))
    except Exception as e:
        print("intro whoosh skipped:", e)

    clip.write_videofile(out_path, fps=FPS, codec="libx264", audio_codec="aac",
                         preset="medium", logger=None)
    return out_path


def extract_clip(src: str, start: float, end: float, out_path: str, freeze_tail: float = 0.0):
    """从一条品牌视频里截取 [start, end] 秒做片头/片尾(wikigold 投放尾板:0-1s 当片头、
    片尾截到下载 CTA 完整出现)。重编码到统一规格,首尾各加 0.2s 音视频淡入淡出避免硬切。
    freeze_tail>0 时:截完把最后一帧再定格 freeze_tail 秒(下载 CTA 整版+应用商店角标
    在素材最后才出齐,定格一下让它看全、读得清),此时不做尾部淡出。
    concat() 之后还会再归一化一次,这里只需保证截得准、带音频。"""
    start = float(start); end = float(end); freeze_tail = float(freeze_tail or 0.0)
    dur = max(0.1, end - start)
    fo = max(0.0, dur - 0.2)
    base_v = "scale=1080:1920,setsar=1,fps=30,format=yuv420p,fade=t=in:st=0:d=0.2"
    base_a = "afade=t=in:st=0:d=0.2"
    if freeze_tail > 0:
        # 定格最后一帧 freeze_tail 秒(视频克隆末帧 + 音频补静音),不做尾部淡出。
        vf = base_v + f",tpad=stop_mode=clone:stop_duration={freeze_tail:.2f}"
        af = base_a + f",apad=pad_dur={freeze_tail:.2f}"
    else:
        vf = base_v + f",fade=t=out:st={fo:.2f}:d=0.2"
        af = base_a + f",afade=t=out:st={fo:.2f}:d=0.2"
    # -ss/-t 放在 -i 前 = 对"输入"裁剪(只读 [start, start+dur]);输出不再加 -t,
    # 否则 -t 会把 tpad 定格的尾巴一起截掉(总长被卡回 dur)。tpad/apad 决定最终时长。
    cmd = [
        _ffmpeg(), "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
        "-vf", vf, "-af", af,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
        "-c:a", "aac", "-ar", "44100", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, check=False)
    if r.returncode != 0:
        print(r.stderr.decode("utf-8", "ignore")[-800:])
        raise RuntimeError("extract_clip failed")
    return out_path


def concat(intro: str, main: str, outro: str, out_path: str, outro_seconds: float = None):
    """三段统一规格后拼接(concat 滤镜,逐段归一化 fps/分辨率/音频)。
    outro_seconds:把片尾截到这么长(片尾后半段几乎静止,默认截短)。None=用整段。"""
    # 片尾按需截断(trim 后重置时间戳),其余归一化不变。
    if outro_seconds and outro_seconds > 0:
        v2 = (f"[2:v]trim=0:{outro_seconds},setpts=PTS-STARTPTS,"
              f"scale=1080:1920,setsar=1,fps=30,format=yuv420p[v2];")
        a2 = (f"[2:a]atrim=0:{outro_seconds},asetpts=PTS-STARTPTS,"
              f"aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a2];")
    else:
        v2 = "[2:v]scale=1080:1920,setsar=1,fps=30,format=yuv420p[v2];"
        a2 = "[2:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a2];"
    fc = (
        "[0:v]scale=1080:1920,setsar=1,fps=30,format=yuv420p[v0];"
        "[1:v]scale=1080:1920,setsar=1,fps=30,format=yuv420p[v1];"
        + v2 +
        "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a0];"
        "[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a1];"
        + a2 +
        "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[outv][outa]"
    )
    cmd = [
        _ffmpeg(), "-y", "-i", intro, "-i", main, "-i", outro,
        "-filter_complex", fc, "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
        "-c:a", "aac", "-ar", "44100", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, check=False)
    if r.returncode != 0:
        print(r.stderr.decode("utf-8", "ignore")[-1500:])
        raise RuntimeError("concat failed")


if __name__ == "__main__":
    main_v, top, bottom, outro_v, out_v = sys.argv[1:6]
    work = os.path.dirname(out_v) or "."
    whoosh = os.path.join(work, "_whoosh.wav")
    intro = os.path.join(work, "_intro.mp4")
    make_whoosh(whoosh)
    make_intro(top, bottom, whoosh, intro)
    concat(intro, main_v, outro_v, out_v)
    print("DONE:", out_v)
