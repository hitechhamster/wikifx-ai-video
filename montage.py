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

MAX_MONTAGE_SECONDS = 9.8

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
                         clip_min=0.7, clip_max=1.4, speed=1.0,
                         transition_count=999):
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
            tense_transition_count=int(transition_count),
            threads=2,
        )
    finally:
        set_montage_flashy(False)                  # 复位,不影响同进程后续新闻任务
    return out_path



def _contains_rtl_script(text: str) -> bool:
    return any(
        "\u0600" <= ch <= "\u06ff" or  # Arabic/Urdu
        "\u0750" <= ch <= "\u077f" or
        "\u08a0" <= ch <= "\u08ff"
        for ch in str(text or "")
    )


def _contains_complex_script(text: str) -> bool:
    return any(
        _contains_rtl_script(ch) or
        "\u0900" <= ch <= "\u097f"     # Devanagari/Hindi
        for ch in str(text or "")
    )


def _resolve_hook_font(font: str, hooks) -> str:
    if font and os.path.isfile(font):
        return os.path.abspath(font)
    if font and not os.path.isabs(font) and os.path.isfile(os.path.join(PROJECT_ROOT, font)):
        return os.path.abspath(os.path.join(PROJECT_ROOT, font))
    sample = " ".join(str(h or "") for h in (hooks or []))
    if _contains_complex_script(sample):
        nirmala = os.path.join(PROJECT_ROOT, "resource", "fonts", "Nirmala.ttc")
        if os.path.isfile(nirmala):
            return nirmala
    return os.path.abspath(os.path.join(PROJECT_ROOT, "resource", "fonts", "MicrosoftYaHeiBold.ttc"))


def _display_hook_text(text: str) -> str:
    text = str(text or "").strip()
    if _contains_rtl_script(text):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            return get_display(arabic_reshaper.reshape(text))
        except Exception as e:
            logger.warning(f"Arabic/Urdu text shaping failed, using raw text: {e}")
            return text
    return text if _contains_complex_script(text) else text.upper()



def _ffmpeg_filter_path(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/").replace(":", r"\:")


def _overlay_rtl_hooks_ffmpeg(video_path: str, out_path: str, hooks, font_path: str, duration: float):
    import subprocess
    from app.utils import utils

    work = os.path.dirname(os.path.abspath(out_path)) or "."
    text_files = []
    try:
        clean_hooks = [str(h or "").strip() for h in (hooks or []) if str(h or "").strip()]
        if not clean_hooks:
            os.replace(video_path, out_path)
            return out_path

        slot = duration / len(clean_hooks)
        vf_parts = []
        for i, hook in enumerate(clean_hooks):
            text_path = os.path.join(work, f"_rtl_hook_{i}.txt")
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(hook)
            text_files.append(text_path)
            start = i * slot
            end = duration if i == len(clean_hooks) - 1 else (i + 1) * slot
            vf_parts.append(
                "drawtext="
                f"fontfile='{_ffmpeg_filter_path(font_path)}':"
                f"textfile='{_ffmpeg_filter_path(text_path)}':"
                "text_shaping=1:"
                "fontcolor=white:"
                "fontsize=68:"
                "bordercolor=black:"
                "borderw=5:"
                "x=(w-text_w)/2:"
                "y=h*0.30:"
                f"enable='between(t,{start:.3f},{end:.3f})'"
            )

        cmd = [
            utils.get_ffmpeg_binary(), "-y", "-i", video_path,
            "-vf", ",".join(vf_parts),
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "copy", out_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return out_path
    finally:
        for text_path in text_files:
            try:
                os.remove(text_path)
            except OSError:
                pass
        try:
            os.remove(video_path)
        except OSError:
            pass


def _wrap_text_to_width(text: str, font, max_width: int) -> list[str]:
    from PIL import Image, ImageDraw

    words = str(text or "").split()
    if not words:
        return [""]
    probe = Image.new("RGBA", (max_width, 200), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    lines, current = [], ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        bbox = draw.textbbox((0, 0), _display_hook_text(candidate), font=font, stroke_width=5)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _make_rtl_hook_png(text: str, image_path: str, font_path: str, video_w: int):
    from PIL import Image, ImageDraw, ImageFont

    max_w = int(video_w * 0.90)
    font_size = 72
    line_gap = 12
    stroke = 5
    while font_size >= 44:
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_text_to_width(text, font, max_w)
        display_lines = [_display_hook_text(line) for line in lines]
        probe = Image.new("RGBA", (max_w, 600), (0, 0, 0, 0))
        draw = ImageDraw.Draw(probe)
        bboxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke) for line in display_lines]
        widths = [b[2] - b[0] for b in bboxes]
        heights = [b[3] - b[1] for b in bboxes]
        total_h = sum(heights) + line_gap * max(0, len(display_lines) - 1)
        if max(widths or [0]) <= max_w and total_h <= 360:
            break
        font_size -= 4

    img_h = max(120, total_h + 40)
    img = Image.new("RGBA", (max_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y = (img_h - total_h) / 2
    for line, bbox, h in zip(display_lines, bboxes, heights):
        w = bbox[2] - bbox[0]
        x = (max_w - w) / 2 - bbox[0]
        draw.text((x, y - bbox[1]), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += h + line_gap
    img.save(image_path)
    return image_path


def _overlay_rtl_hooks_images(video_path: str, out_path: str, hooks, font_path: str, duration: float):
    import subprocess
    from moviepy import VideoFileClip
    from app.utils import utils

    work = os.path.dirname(os.path.abspath(out_path)) or "."
    image_files = []
    try:
        clean_hooks = [str(h or "").strip() for h in (hooks or []) if str(h or "").strip()]
        if not clean_hooks:
            os.replace(video_path, out_path)
            return out_path

        with VideoFileClip(video_path) as probe:
            video_w = int(probe.size[0])

        cmd = [utils.get_ffmpeg_binary(), "-y", "-i", video_path]
        for i, hook in enumerate(clean_hooks):
            image_path = os.path.join(work, f"_rtl_hook_{i}.png")
            _make_rtl_hook_png(hook, image_path, font_path, video_w)
            image_files.append(image_path)
            cmd.extend(["-i", image_path])

        slot = duration / len(clean_hooks)
        chain = []
        prev = "[0:v]"
        for i in range(len(clean_hooks)):
            start = i * slot
            end = duration if i == len(clean_hooks) - 1 else (i + 1) * slot
            out_label = "[vout]" if i == len(clean_hooks) - 1 else f"[v{i + 1}]"
            chain.append(
                f"{prev}[{i + 1}:v]overlay=(main_w-overlay_w)/2:main_h*0.30:"
                f"enable='between(t,{start:.3f},{end:.3f})'{out_label}"
            )
            prev = out_label

        cmd.extend([
            "-filter_complex", ";".join(chain),
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "copy", out_path,
        ])
        subprocess.run(cmd, capture_output=True, check=True)
        return out_path
    finally:
        for image_path in image_files:
            try:
                os.remove(image_path)
            except OSError:
                pass
        try:
            os.remove(video_path)
        except OSError:
            pass


def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs -= 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return str(text or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _overlay_rtl_hooks_ass(video_path: str, out_path: str, hooks, font_path: str, duration: float):
    import subprocess
    from app.utils import utils

    video_path = os.path.abspath(video_path)
    out_path = os.path.abspath(out_path)
    work = os.path.dirname(out_path) or "."
    ass_path = os.path.join(work, "_rtl_hooks.ass")
    clean_hooks = [str(h or "").strip() for h in (hooks or []) if str(h or "").strip()]
    try:
        if not clean_hooks:
            os.replace(video_path, out_path)
            return out_path

        # libass/fontconfig resolves family names more reliably than Windows TTC paths.
        font_name = "Nirmala UI" if "nirmala" in os.path.basename(font_path).lower() else "Arial"
        slot = duration / len(clean_hooks)
        events = []
        for i, hook in enumerate(clean_hooks):
            start = i * slot
            end = duration if i == len(clean_hooks) - 1 else (i + 1) * slot
            # Raw Unicode text is intentional: libass shaping=complex does the bidi/OpenType shaping.
            text = r"{\an5\pos(540,650)}" + _ass_escape(hook)
            events.append(
                f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},Hook,,0,0,0,,{text}"
            )

        ass = "\n".join([
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Hook,{font_name},72,&H00FFFFFF,&H000000FF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,1,5,0,5,60,60,60,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ])
        with open(ass_path, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(ass)

        cmd = [
            utils.get_ffmpeg_binary(), "-y", "-i", video_path,
            "-vf", "subtitles=filename='_rtl_hooks.ass'",
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "copy", out_path,
        ]
        subprocess.run(cmd, cwd=work, capture_output=True, check=True)
        return out_path
    finally:
        try:
            os.remove(video_path)
        except OSError:
            pass


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
    rtl_hooks = any(_contains_rtl_script(h) for h in hooks)
    font_abs = _resolve_hook_font(font, hooks)
    if hooks and not rtl_hooks:
        slot = dur / len(hooks)
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
            complex_script = _contains_complex_script(h)
            txt = (TextClip(text=_display_hook_text(h), font=font_abs, font_size=(68 if complex_script else 80),
                            color="white", method="caption", size=(int(W * 0.90), 500 if complex_script else 460),
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
    stem = os.path.splitext(os.path.basename(out_path))[0]
    work_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    temp_audio = os.path.join(work_dir, f"_{stem}_temp_audio.m4a")
    render_path = os.path.join(work_dir, f"_{stem}_no_text.mp4") if rtl_hooks else out_path
    try:
        final.write_videofile(
            render_path,
            fps=30,
            codec="libx264",
            audio_codec="aac",
            preset="medium",
            logger=None,
            temp_audiofile=temp_audio,
            remove_temp=False,
        )
    finally:
        final.close()
        base.close()
        try:
            bgm.close()
        except Exception:
            pass
        if "vo" in locals():
            try:
                vo.close()
            except Exception:
                pass
        try:
            os.remove(temp_audio)
        except OSError:
            pass
    if rtl_hooks:
        _overlay_rtl_hooks_ass(render_path, out_path, hooks, font_abs, dur)
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
                     voice_name="gemini:Puck-Male", voice_rate=1.0,
                     transition_count=999, hook_font=""):
    keywords = keywords or DEFAULT_KEYWORDS
    target_seconds = min(float(target_seconds), MAX_MONTAGE_SECONDS)
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
                         clip_min=clip_min, clip_max=clip_max, speed=speed,
                         transition_count=transition_count)
    overlay_hooks_and_bgm(silent, bgm_cut, hooks, out_path, font=hook_font or "", voiceover_path=vo_path)
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
