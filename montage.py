# -*- coding: utf-8 -*-
"""
炫富生活流"纯音乐混剪"生成器(无配音):
  名表/豪车/海边/游艇/豪宅 等画面 → 卡 TikTok 音乐长度的高能快切 →
  几乎每刀酷炫转场(缩放冲击/速度甩切/闪白)→ 叠大字金句 hook。

和新闻管线的区别:无脚本/无 TTS,视频长度跟 BGM 走;素材判定只判真假不判"是否财经"
(topic="",否则海滩豪车会被当跑题毙掉);转场密度拉满;最后叠大字 hook 而非字幕。

复用 combine_videos(产出无声高能拼接)+ providers(搜素材),再自己挂 BGM + 叠 hook。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from loguru import logger

# 默认炫富搜索词(模板 config 可覆盖)
DEFAULT_KEYWORDS = [
    "luxury sports car driving", "rolex watch closeup", "private yacht ocean",
    "luxury mansion interior", "beach sunset paradise", "private jet interior",
    "stack of cash money", "designer shopping bags", "infinity pool villa",
    "champagne celebration party", "supercar showroom", "rooftop city skyline night",
]


def _aspect_res(aspect="9:16"):
    from app.models.schema import VideoAspect
    a = VideoAspect(aspect)
    return a, *a.to_resolution()


def fetch_clips(keywords, aspect, n_sources=12, min_dur=1.4):
    """搜并下载一批炫富素材源(只判真假不判财经相关)。"""
    from app.services.providers import PexelsProvider
    prov = PexelsProvider(require_real_footage=True)
    used, paths = set(), []
    i = 0
    attempts = 0
    while len(paths) < n_sources and attempts < n_sources * 4:
        attempts += 1
        term = keywords[i % len(keywords)]
        i += 1
        r = prov.fetch(term, aspect, min_dur, exclude_urls=used, topic="")  # topic="" → 只判真假
        if not r:
            continue
        paths.append(r.path)
        url = (r.metadata or {}).get("url")
        if url:
            used.add(url)
    logger.info(f"montage: fetched {len(paths)} luxury source clips")
    return paths


def build_silent_montage(clips, bgm_path, out_path, aspect,
                         clip_min=0.7, clip_max=1.4, speed=1.0):
    """用 combine_videos 产出卡 BGM 长度、几乎每刀转场的无声高能拼接。"""
    from app.services.video import combine_videos, set_montage_flashy
    from app.models.schema import VideoTransitionMode, VideoConcatMode
    set_montage_flashy(True)   # 逐刀夸张特效(强力zoom punch/抖动/闪白/甩切+整段推进)
    try:
        combine_videos(
            combined_video_path=out_path,
            video_paths=clips,
            audio_file=bgm_path,                   # 只用来定时长(产出无声)
            video_aspect=aspect,
            video_concat_mode=VideoConcatMode.random,
            video_transition_mode=VideoTransitionMode.tense,
            max_clip_duration=clip_max,
            min_clip_duration=clip_min,
            random_clip_duration=True,
            clip_speed_factor=speed,
            tense_transition_count=999,            # 拉满 → 每刀都炸
            threads=2,
        )
    finally:
        set_montage_flashy(False)                  # 复位,不影响同进程后续新闻任务
    return out_path


def make_voiceover(text, out_mp3, voice_name="gemini:Puck-Male", rate=1.0):
    """生成开头画外音(英文短句,如 'This is what forex trading gave me.')。"""
    from app.services import voice
    voice.tts(text=str(text).strip(), voice_name=voice_name, voice_rate=rate,
              voice_file=out_mp3, voice_volume=1.0)
    return out_mp3 if (os.path.isfile(out_mp3) and os.path.getsize(out_mp3) > 0) else ""


def overlay_hooks_and_bgm(silent_video, bgm_path, hooks, out_path,
                          font="resource/fonts/MicrosoftYaHeiBold.ttc",
                          voiceover_path=""):
    """给无声混剪挂上 BGM(+开头画外音)+ 叠大字金句 hook(依次显示)。
    有画外音时:开头那几秒把 BGM 压低(ducking),让人声清楚,之后 BGM 恢复。"""
    from moviepy import (VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip,
                         CompositeAudioClip, concatenate_audioclips)
    base = VideoFileClip(silent_video)
    W, H = base.size
    # 成片时长卡到"无声混剪"和"BGM"两者较短的那个:combine_videos 会在最后一刀
    # 边界把无声片略微超出 target(如 7.63s),而 BGM 已被精确截到 target(7.0s),
    # 直接用 base.duration 去 subclip BGM 会越界报错(end_time>bgm时长)。取 min 兜住。
    bgm = AudioFileClip(bgm_path)
    dur = min(base.duration, bgm.duration)
    if bgm.duration > dur:
        bgm = bgm.subclipped(0, dur)

    layers = [base]
    hooks = [h for h in (hooks or []) if h and str(h).strip()]
    if hooks:
        slot = dur / len(hooks)
        font_abs = os.path.abspath(font)
        def pop_scale(t):
            # 弹入: 0.5→1.12 过冲 → 回落 1.0, 之后保持
            if t < 0.16:
                return 0.5 + (1.12 - 0.5) * (t / 0.16)
            if t < 0.28:
                return 1.12 - 0.12 * ((t - 0.16) / 0.12)
            return 1.0

        for i, h in enumerate(hooks):
            t0 = i * slot
            seg = slot if i < len(hooks) - 1 else (dur - t0)
            # caption 给足高度的文字框(否则多行金句下面会被裁),文字在框内垂直居中。
            # 再加弹入缩放动画,让大字"砸"进画面,更夸张。
            txt = (TextClip(text=str(h).strip().upper(), font=font_abs, font_size=80,
                            color="white", method="caption", size=(int(W * 0.86), 460),
                            text_align="center", stroke_color="black", stroke_width=5)
                   .with_start(t0).with_duration(seg)
                   .resized(pop_scale)
                   .with_position(("center", int(H * 0.30))))
            layers.append(txt)

    audio = bgm
    if voiceover_path and os.path.isfile(voiceover_path):
        vo = AudioFileClip(voiceover_path)
        vo_dur = min(vo.duration, dur)
        # ducking:画外音时段 BGM 压到 0.35,之后恢复满音量
        bgm_low = bgm.subclipped(0, vo_dur).with_volume_scaled(0.35)
        if dur > vo_dur:
            bgm_mixed = concatenate_audioclips([bgm_low, bgm.subclipped(vo_dur, dur)])
        else:
            bgm_mixed = bgm_low
        audio = CompositeAudioClip([bgm_mixed, vo.with_start(0)])

    final = CompositeVideoClip(layers, size=(W, H)).with_duration(dur).with_audio(audio)
    final.write_videofile(out_path, fps=30, codec="libx264", audio_codec="aac",
                          preset="medium", logger=None)
    base.close()
    return out_path


def _trim_bgm(bgm_path, target_seconds, out_wav):
    """把 BGM 截到 target_seconds(短视频 15-30s),并在结尾加 0.4s 淡出。
    截出来的这段同时用于混剪定时长和最终配乐,保证音画长度一致。"""
    import subprocess
    from app.utils import utils
    fade_st = max(0.0, target_seconds - 0.4)
    cmd = [
        utils.get_ffmpeg_binary(), "-y", "-i", bgm_path, "-t", f"{target_seconds}",
        "-af", f"afade=t=out:st={fade_st}:d=0.4", "-ar", "44100", "-ac", "2", out_wav,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out_wav


def generate_montage(out_path, bgm_path, hooks, keywords=None, aspect="9:16",
                     clip_min=0.7, clip_max=1.4, speed=1.0, n_sources=12,
                     target_seconds=25.0, voiceover="",
                     voice_name="gemini:Puck-Male", voice_rate=1.0):
    keywords = keywords or DEFAULT_KEYWORDS
    a, W, H = _aspect_res(aspect)
    work = os.path.dirname(out_path) or "."

    # 截 BGM 到目标短视频长度(TikTok 感 15-30s),否则会按整首歌填出 2-3 分钟的超长片
    bgm_cut = os.path.join(work, "_bgm_cut.wav")
    _trim_bgm(bgm_path, target_seconds, bgm_cut)

    # 开头画外音(可选):英文短句,如 "This is what forex trading gave me."
    vo_path = ""
    if voiceover and str(voiceover).strip():
        vo_path = make_voiceover(str(voiceover).strip(),
                                 os.path.join(work, "_vo.mp3"),
                                 voice_name=voice_name, rate=voice_rate)

    clips = fetch_clips(keywords, a, n_sources=n_sources, min_dur=clip_max)
    if not clips:
        raise RuntimeError("montage: 没搜到可用素材")
    silent = os.path.join(work, "_montage_silent.mp4")
    build_silent_montage(clips, bgm_cut, silent, a,
                         clip_min=clip_min, clip_max=clip_max, speed=speed)
    overlay_hooks_and_bgm(silent, bgm_cut, hooks, out_path, voiceover_path=vo_path)
    for tmp in (silent, bgm_cut, vo_path):
        if tmp:
            try: os.remove(tmp)
            except OSError: pass
    print("MONTAGE DONE:", out_path)
    return out_path


if __name__ == "__main__":
    # 自测: python montage.py <bgm.mp3> <out.mp4> "HOOK1|HOOK2|HOOK3"
    bgm, out = sys.argv[1], sys.argv[2]
    hooks = sys.argv[3].split("|") if len(sys.argv) > 3 else [
        "POV: YOUR LIFE AFTER FOREX", "STOP TRADING TIME FOR MONEY", "THIS IS FREEDOM",
    ]
    generate_montage(out, bgm, hooks)
