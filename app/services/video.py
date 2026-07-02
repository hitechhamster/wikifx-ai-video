import glob
import itertools
import io
import math
import os
import random
import gc
import shutil
import subprocess
from contextlib import redirect_stdout
from functools import lru_cache
from typing import List
from loguru import logger
import numpy as np
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageDraw, ImageFont

from app.config import config
from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.utils import video_effects
from app.utils import file_security, utils

class SubClippedVideoClip:
    def __init__(
        self,
        file_path,
        start_time=None,
        end_time=None,
        width=None,
        height=None,
        duration=None,
        source_file_path=None,
        target_display=None,
    ):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        self.source_file_path = source_file_path or file_path
        # target_display:按句子对齐模式下,这一段画面应该在屏幕上播出的秒数
        # (= 对应那句话的音频时长)。设了它就用"逐段对齐"逻辑:每段速度单独算,
        # 让画面正好铺满那句话,不再统一加速/不再按 max_clip_duration 截断。
        self.target_display = target_display
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
# Docker 里的 ffmpeg/AAC 组合在默认配置下更容易出现音频质量波动，
# 这里显式抬高音频码率，避免成片阶段因为默认值过低而引入明显失真。
audio_bitrate = "192k"
fps = 30
_BGM_EXTENSIONS = (".mp3",)
_DEFAULT_VIDEO_CODEC = "libx264"
_SUPPORTED_VIDEO_CODECS = (
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
)
_runtime_disabled_video_codecs = set()


def _prioritize_unique_source_clips(
    subclipped_items: List[SubClippedVideoClip],
    concat_mode: VideoConcatMode,
) -> List[SubClippedVideoClip]:
    """
    优先让每个源素材只出现一次，降低成片里同一素材反复出现的概率。

    线上素材经常会遇到“一个长视频被切成多个短片段”的情况。旧逻辑在
    random 模式下直接打乱所有短片段，导致同一个源视频的多个切片可能
    分布在开头和中间，用户会感知为素材重复。本函数只调整片段顺序：
    先放每个源文件里最长的一个片段，剩余片段作为兜底；当素材总时长不足时，
    仍然允许后续片段补齐音频长度，避免破坏视频生成成功率。优先选择最长
    片段是为了避免随机选中视频尾部的零碎短片段，导致明明有足够素材却过早复用。
    """
    if not subclipped_items:
        return []

    concat_mode_value = getattr(concat_mode, "value", concat_mode)
    if concat_mode_value != VideoConcatMode.random.value:
        return subclipped_items

    grouped_items: dict[str, list[SubClippedVideoClip]] = {}
    for item in subclipped_items:
        grouped_items.setdefault(item.source_file_path, []).append(item)

    primary_items = []
    overflow_items = []
    for items in grouped_items.values():
        primary_item = max(items, key=lambda item: item.duration)
        primary_items.append(primary_item)
        overflow_items.extend(item for item in items if item is not primary_item)

    random.shuffle(primary_items)
    random.shuffle(overflow_items)
    logger.info(
        "prioritized unique video materials, "
        f"sources: {len(grouped_items)}, "
        f"primary clips: {len(primary_items)}, "
        f"fallback clips: {len(overflow_items)}"
    )
    return primary_items + overflow_items


def get_ffmpeg_binary():
    """
    兼容历史上直接从 video 服务读取 FFmpeg 路径的调用方。

    真正的解析逻辑已经抽到 `app.utils.utils.get_ffmpeg_binary()`，视频、语音
    和后续新增链路都应复用同一套优先级；这里保留薄包装，避免外部脚本或
    旧测试直接导入 `app.services.video.get_ffmpeg_binary` 时出现 AttributeError。
    """
    return utils.get_ffmpeg_binary()


def _get_configured_video_codec() -> str:
    """
    读取用户配置的视频编码器。

    该配置面向高级用户，用于尝试启用 NVENC/AMF/QSV/VideoToolbox 等硬件
    编码。这里刻意只允许固定白名单，避免开放任意 FFmpeg 参数后，用户填错
    参数导致输出格式不可控，甚至让生成任务在后续阶段才失败。
    """
    configured_codec = str(
        config.app.get("video_codec", _DEFAULT_VIDEO_CODEC) or _DEFAULT_VIDEO_CODEC
    ).strip()
    if configured_codec not in _SUPPORTED_VIDEO_CODECS:
        logger.warning(
            f"unsupported video codec configured: {configured_codec}, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC
    return configured_codec


@lru_cache(maxsize=16)
def _ffmpeg_encoder_exists(ffmpeg_binary: str, codec: str) -> bool:
    """
    检查当前 FFmpeg 是否声明支持指定编码器。

    这只能证明 FFmpeg 编译时包含该 encoder，不能证明当前机器硬件和驱动
    一定可用。因此实际编码失败时仍会再回退到 libx264。
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {str(exc)}"
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {(result.stderr or result.stdout or '').strip()}"
        )
        return False
    return codec in result.stdout


def _get_effective_video_codec(preferred_codec: str | None = None) -> str:
    """
    返回本次实际使用的视频编码器。

    用户选择硬件编码器时，先做 FFmpeg encoder 列表检测；如果本进程里已经
    实际编码失败过，也直接回退，避免一个任务里每个片段都重复失败。
    """
    selected_codec = preferred_codec or _get_configured_video_codec()
    if selected_codec == _DEFAULT_VIDEO_CODEC:
        return _DEFAULT_VIDEO_CODEC

    if selected_codec in _runtime_disabled_video_codecs:
        logger.warning(
            f"video codec {selected_codec} was disabled after a runtime failure, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    ffmpeg_binary = utils.get_ffmpeg_binary()
    if not _ffmpeg_encoder_exists(ffmpeg_binary, selected_codec):
        logger.warning(
            f"ffmpeg encoder {selected_codec} is not available, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    return selected_codec


def _disable_runtime_video_codec(codec: str, reason: str):
    if codec == _DEFAULT_VIDEO_CODEC:
        return
    _runtime_disabled_video_codecs.add(codec)
    logger.warning(
        f"video codec {codec} failed, fallback to {_DEFAULT_VIDEO_CODEC}. "
        f"reason: {reason}"
    )


def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):
    """
    硬件编码失败后用 libx264 重试，只有重试成功才禁用该硬件编码器。

    Windows 上 FFmpeg 失败原因比较复杂：可能是显卡/驱动不支持，也可能是输出
    文件被占用、目录权限、杀软拦截等通用 IO 问题。只有 libx264 能成功写出时，
    才能判断原始失败大概率来自硬件编码器本身，避免误伤后续任务。
    """
    clip.write_videofile(output_file, codec=_DEFAULT_VIDEO_CODEC, **kwargs)
    _disable_runtime_video_codec(failed_codec, reason)
    return _DEFAULT_VIDEO_CODEC


def _write_videofile_with_codec_fallback(clip, output_file: str, codec: str, **kwargs):
    """
    使用指定编码器写出视频，失败时自动用 libx264 重试一次。

    硬件编码器是否可用不仅取决于 FFmpeg，还取决于显卡、驱动和当前运行环境。
    生成任务不能因为高级编码器不可用而整体失败，所以这里把回退集中处理。
    """
    effective_codec = _get_effective_video_codec(codec)
    try:
        clip.write_videofile(output_file, codec=effective_codec, **kwargs)
        return effective_codec
    except PermissionError as exc:
        # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
        # Windows 上 MoviePy write_videofile() 写完正片后，最后一步清理
        # xxxTEMP_MPY_wvf_snd.mp4 临时音频文件时，偶发会撞上文件还被
        # ffmpeg 子进程占用、句柄没及时释放，抛出 PermissionError。
        # 这个异常发生在视频主体已经写完*之后*——这里之前没有捕获，
        # 会让整条任务线程未捕获崩溃(task_manager.run_task 没有 try/except)，
        # 任务状态永远卡在"进行中"不会变成完成或失败，即使 final-*.mp4
        # 其实已经是完整可播放的文件(已用 ffmpeg -f null 验证过)。
        # 只有输出文件确实已经写完(存在且非空)才把这个异常降级成警告；
        # 真正的写入失败(文件不存在或为空)依然要 raise，不能被这条吞掉。
        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            logger.warning(
                f"write_videofile: 清理临时音频文件时遇到 Windows 文件占用("
                f"已知问题)，但输出文件已经写完，忽略这个异常: {exc}"
            )
            return effective_codec
        raise
    except Exception as exc:
        if effective_codec == _DEFAULT_VIDEO_CODEC:
            raise
        return _fallback_write_videofile(
            clip,
            output_file,
            failed_codec=effective_codec,
            reason=str(exc),
            **kwargs,
        )


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer 使用单引号包裹路径，路径中的单引号需要先转义。
    return file_path.replace("'", "'\\''")


def _format_ffmpeg_concat_path(file_path: str) -> str:
    """
    生成 concat demuxer 文件列表中的路径。

    FFmpeg 官方文档要求 concat list 中的特殊字符和空格需要转义；Windows
    绝对路径里的反斜杠也容易被解析成转义字符。这里统一转成正斜杠形式，
    让 `C:\\Users\\...` 变成 `C:/Users/...`，再处理单引号，兼容 macOS/Linux。
    """
    absolute_path = os.path.abspath(file_path)
    return _escape_ffmpeg_concat_path(absolute_path.replace("\\", "/"))


def concat_video_clips_with_ffmpeg(
    clip_files: List[str], output_file: str, threads: int, output_dir: str
):
    concat_list_file = os.path.join(output_dir, "ffmpeg-concat-list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as fp:
        for clip_file in clip_files:
            fp.write(f"file '{_format_ffmpeg_concat_path(clip_file)}'\n")

    def build_command(codec: str) -> list[str]:
        return [
            utils.get_ffmpeg_binary(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_file,
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
            output_file,
        ]

    def run_concat(codec: str):
        command = build_command(codec)
        # 使用 ffmpeg 只做一次串联与编码，避免 MoviePy 逐段合并时反复重编码，
        # 从而降低画质劣化与颜色偏移风险。
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
        return codec

    try:
        effective_codec = _get_effective_video_codec()
        try:
            return run_concat(effective_codec)
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
            return result_codec
    finally:
        delete_files(concat_list_file)


def _sanitize_image_file(image_path: str) -> str:
    # 某些本地图片虽然能被 Pillow 打开，但会因为损坏的 EXIF/eXIf 元数据导致
    # ImageClip 在解析阶段直接抛异常。这里重新导出一份“干净图片”，把坏元数据剥离掉。
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # 统一导出为 PNG，避免 JPEG/PNG 不同元数据路径继续把坏块带过去。
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # 优先直接打开原始图片；如果因为损坏元数据失败，再尝试生成无元数据副本。
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path


def _open_video_clip_quietly(video_path: str, audio: bool = False) -> VideoFileClip:
    """
    安静地打开视频文件，避免 MoviePy 2.1.x 把 ffmpeg 探测信息直接打印到 stdout。

    背景：
    当前依赖版本的 `FFMPEG_VideoReader` 内部存在 `print(self.infos)` 和
    `print(ffmpeg command)`，读取无音轨的中间视频时会输出
    `audio_found: False`。这只是输入素材 metadata，不代表最终成片没有音频，
    但会误导 WebUI/终端用户以为生成失败。

    实现：
    1. 只在打开 VideoFileClip 的短窗口内重定向 stdout；
    2. 默认 `audio=False`，因为项目视频素材阶段不需要保留素材原声，
       最终音频会在 `generate_video()` 阶段统一挂载；
    3. 如果依赖库确实输出了内容，降级为 debug 日志，便于必要时排查。
    """
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        clip = VideoFileClip(video_path, audio=audio)

    moviepy_stdout = captured_stdout.getvalue().strip()
    if moviepy_stdout:
        logger.debug(
            "suppressed MoviePy video reader stdout for "
            f"{video_path}, chars: {len(moviepy_stdout)}"
        )

    return clip


def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    for file in files:
        try:
            os.remove(file)
        except Exception as e:
            logger.debug(f"failed to delete file {file}: {str(e)}")


def _resolve_bgm_file_path(song_dir: str, bgm_file: str) -> str:
    # 背景音乐只允许读取 resource/songs 目录内的文件，避免用户输入任意路径后
    # 被 MoviePy 打开。这里兼容两种常见输入：
    # 1. output000.mp3：来自 BGM 列表或用户只填写文件名
    # 2. ./resource/songs/output000.mp3：用户按项目目录结构填写的相对路径
    # 两种写法最终都会再次通过 resource/songs 白名单校验，不能绕过目录限制。
    try:
        return file_security.resolve_path_within_directory(song_dir, bgm_file)
    except ValueError as song_dir_exc:
        if os.path.isabs(bgm_file):
            raise song_dir_exc

        project_relative_file = os.path.join(utils.root_dir(), bgm_file)
        try:
            return file_security.resolve_path_within_directory(
                song_dir, project_relative_file
            )
        except ValueError as root_dir_exc:
            raise ValueError(str(root_dir_exc)) from song_dir_exc


def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file:
        song_dir = utils.song_dir()
        try:
            resolved_bgm_file = _resolve_bgm_file_path(song_dir, bgm_file)
        except ValueError as exc:
            # API 请求里的 bgm_file 来自用户输入，不能直接把任意绝对路径交给
            # MoviePy 打开。这里强制限制到 resource/songs 目录，阻止读取
            # /etc/passwd、配置文件、密钥等非背景音乐文件。
            logger.warning(
                f"reject unsafe bgm file: {bgm_file}, song_dir: {song_dir}, error: {str(exc)}"
            )
            return ""

        if not resolved_bgm_file.lower().endswith(_BGM_EXTENSIONS):
            logger.warning(f"reject unsupported bgm file extension: {resolved_bgm_file}")
            return ""

        return resolved_bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        # 当背景音乐目录为空时，直接回退为“不使用 BGM”，避免 random.choice([]) 抛异常。
        if not files:
            logger.warning(f"no bgm files found in song directory: {song_dir}")
            return ""
        return random.choice(files)

    return ""


# 炫富混剪"夸张特效"开关:montage.py 在拼接前置 True,让每刀都用狠特效;
# 新闻模式保持 False(只在 1~2 个点用温和快速转场,保持严肃)。
_MONTAGE_FLASHY = False


def set_montage_flashy(value: bool):
    global _MONTAGE_FLASHY
    _MONTAGE_FLASHY = bool(value)


def _select_tense_transition_fn():
    if _MONTAGE_FLASHY:
        return video_effects.montage_flashy_effect  # 整段推进 + 强力入场,逐刀都炸
    side = random.choice(["left", "right", "top", "bottom"])
    return random.choice([
        lambda c: video_effects.quick_zoom_transition(c, 0.2),
        lambda c: video_effects.whip_pan_transition(c, 0.2, side),
        lambda c: video_effects.white_flash_transition(c, 0.08),
    ])


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
    clip_speed_factor: float = 1.0,
    tense_transition_count: int = 0,
    min_clip_duration: float = None,
    random_clip_duration: bool = True,
    clip_durations: list = None,
) -> str:
    # clip_durations:按句子对齐模式。给定时,video_paths 是"每句一个素材、按文案
    # 顺序排列",clip_durations[i] 是第 i 句的音频时长。此模式下每段画面对齐到对应
    # 那句话的时间窗、按顺序播放(不打乱、不快切混填),实现"华盛顿那句出现华盛顿"。
    aligned_mode = bool(clip_durations) and len(clip_durations) == len(video_paths)

    audio_clip = AudioFileClip(audio_file)
    try:
        # 这里只需要读取旁白音频时长来决定素材视频拼接长度；后续不会再使用
        # audio_clip。读取完成后立即关闭，避免早退或异常路径泄漏文件句柄。
        audio_duration = audio_clip.duration
    finally:
        close_clip(audio_clip)
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")

    # 片段时长按"播出时长"(屏幕上实际看到的秒数)来控制,而不是源窗口长度。
    # 关键:素材片段后面会被 clip_speed_factor 加速,所以源窗口 = 播出时长 * 加速倍数。
    # 之前直接把源窗口当播出时长,1.2s 窗口经 1.3x 加速后只剩 0.92s,出现"不到一秒"
    # 的碎片镜头。这里 min_clip_duration/max_clip_duration 一律按播出秒数解释。
    speed = clip_speed_factor if clip_speed_factor and clip_speed_factor > 0 else 1.0
    onscreen_min = min_clip_duration if min_clip_duration else max_clip_duration * 0.66
    onscreen_min = max(0.8, onscreen_min)
    onscreen_max = max(onscreen_min + 0.4, max_clip_duration)
    win_min = onscreen_min * speed   # 源窗口下限
    win_max = onscreen_max * speed   # 源窗口上限
    use_random_clip = bool(random_clip_duration) and onscreen_min < onscreen_max
    logger.info(
        f"clip duration (on-screen): "
        f"[{onscreen_min:.2f}, {onscreen_max:.2f}]s, speed={speed:.2f}, "
        f"random={use_random_clip}"
    )

    # 兼容 API 直接调用时未传转场模式的情况，避免后续访问 .value 时崩溃。
    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    is_tense_mode = transition_value == VideoTransitionMode.tense.value
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    video_duration = 0

    if aligned_mode:
        # 按句子对齐:每个 video_path 对应一句话,这一段画面要正好播 clip_durations[i] 秒
        # (那句话的音频时长)。从源素材取一个窗口(长度 = 播出秒数 * 加速倍数,不够就取全段),
        # 后面 _process_subclip 会按 target_display 单独算速度铺满。顺序保持不打乱。
        for video_path, target in zip(video_paths, clip_durations):
            clip = _open_video_clip_quietly(video_path)
            src_dur = clip.duration
            clip_w, clip_h = clip.size
            close_clip(clip)
            target = max(0.5, float(target))
            want = target * speed
            if src_dur <= want:
                start_t, end_t = 0.0, src_dur          # 源不够长就整段用(后面放慢铺满)
            else:
                start_t = random.uniform(0, src_dur - want)  # 长源随机取一段,增加多样性
                end_t = start_t + want
            subclipped_items.append(
                SubClippedVideoClip(
                    file_path=video_path, start_time=start_t, end_time=end_t,
                    width=clip_w, height=clip_h, source_file_path=video_path,
                    target_display=target,
                )
            )
    else:
        for video_path in video_paths:
            clip = _open_video_clip_quietly(video_path)
            clip_duration = clip.duration
            clip_w, clip_h = clip.size
            close_clip(clip)

            start_time = 0

            while start_time < clip_duration:
                window = random.uniform(win_min, win_max) if use_random_clip else win_max
                end_time = start_time + window
                # 尾段吸收:如果切完这一段后剩下的不足一个最短窗口,就把残料并进当前段,
                # 避免切出 <min 的碎片镜头(经加速后会变成一闪而过的半秒镜头)。
                if clip_duration - end_time < win_min:
                    end_time = clip_duration
                end_time = min(end_time, clip_duration)

                # 保留有效分段(过滤掉可能出现的近零长度尾巴)。
                if end_time - start_time >= 0.3:
                    subclipped_items.append(
                        SubClippedVideoClip(
                            file_path=video_path,
                            start_time=start_time,
                            end_time=end_time,
                            width=clip_w,
                            height=clip_h,
                            source_file_path=video_path,
                        )
                    )

                start_time = end_time
                if video_concat_mode.value == VideoConcatMode.sequential.value:
                    break

        # 对齐模式严格按句子顺序播放,绝不打乱;只有混填模式才重排素材顺序。
        subclipped_items = _prioritize_unique_source_clips(
            subclipped_items=subclipped_items,
            concat_mode=video_concat_mode,
        )

    logger.debug(f"total subclipped items: {len(subclipped_items)}")

    # "快速紧张"模式:整段视频只在随机挑出的 N 个片段边界插入快速转场
    # (quick zoom / whip pan / white flash 之一)，其余全部硬切。位置从 1 开始
    # 取(第一个片段前不需要转场)。
    # 候选范围不能用 len(subclipped_items) 全量(33个)——下面的消耗循环一旦
    # 音频时长被填满就会提前 break，实际只会用到其中一部分(比如14个)。如果
    # 候选范围覆盖了用不到的尾部片段，随机抽中的转场位置可能根本不会被处理，
    # 转场静默失效。这里按 (音频时长 / 加速后单片时长) 估算实际会用到的片段数，
    # 把候选范围收紧到这个估计值内，让转场大概率真的落在最终视频里。
    tense_indices = set()
    if is_tense_mode and len(subclipped_items) > 1:
        if aligned_mode:
            # 对齐模式每段都会用上,实际片段数就是 len。
            estimated_clip_count = len(subclipped_items)
        else:
            # 用"播出时长"均值估算实际会用到的片段数(播出时长已含加速因素)。
            effective_clip_len = (
                (onscreen_min + onscreen_max) / 2 if use_random_clip else onscreen_max
            )
            estimated_clip_count = max(1, math.ceil(audio_duration / max(effective_clip_len, 0.1)))
        usable_range = min(estimated_clip_count, len(subclipped_items))
        candidate_positions = list(range(1, usable_range))
        n = min(max(tense_transition_count, 0), len(candidate_positions))
        tense_indices = set(random.sample(candidate_positions, n))
        logger.debug(
            f"tense transitions: estimated {estimated_clip_count} clips will be used, "
            f"placing {n} transition(s) at indices {sorted(tense_indices)}"
        )

    def _process_subclip(i, subclipped_item):
        try:
            clip = _open_video_clip_quietly(subclipped_item.file_path).subclipped(
                subclipped_item.start_time, subclipped_item.end_time
            )
            clip_duration = clip.duration
            # 缩放铺满竖屏 + 居中裁剪(cover),而不是留黑边适配(contain)。
            # 之前横向素材(如一张横向地图)会用 contain 逻辑缩到刚好放进竖屏,上下留
            # 大块黑边,在竖屏新闻里很难看。改成取较大的缩放系数让画面铺满整个 9:16,
            # 多出来的部分居中裁掉——竖屏短视频的标准做法,无黑边。
            clip_w, clip_h = clip.size
            if clip_w != video_width or clip_h != video_height:
                logger.debug(
                    f"cover-resize clip {clip_w}x{clip_h} -> {video_width}x{video_height}"
                )
                scale_factor = max(video_width / clip_w, video_height / clip_h)
                new_width = math.ceil(clip_w * scale_factor)
                new_height = math.ceil(clip_h * scale_factor)
                clip = clip.resized(new_size=(new_width, new_height))
                if new_width != video_width or new_height != video_height:
                    x1 = (new_width - video_width) / 2
                    y1 = (new_height - video_height) / 2
                    clip = clip.cropped(
                        x1=x1, y1=y1, width=video_width, height=video_height
                    )

            # 素材是无声 B-roll(_open_video_clip_quietly 默认 audio=False)，
            # 加速只影响画面，不会动到后面统一挂载的配音/BGM 轨道。
            if subclipped_item.target_display:
                # 对齐模式:这一段速度单独算,让它正好播 target_display 秒(铺满那句话)。
                # 源够长时速度≈clip_speed_factor;源偏短时速度<1(轻微慢放)把时间窗补满。
                eff_speed = clip.duration / subclipped_item.target_display
                eff_speed = max(0.5, min(eff_speed, 3.0))  # 防止极端慢放/快放
                clip = clip.with_speed_scaled(eff_speed)
            elif clip_speed_factor and clip_speed_factor != 1.0:
                clip = clip.with_speed_scaled(clip_speed_factor)

            if is_tense_mode:
                if i in tense_indices:
                    clip = _select_tense_transition_fn()(clip)
                # 其余位置硬切，不调用任何转场函数。
            else:
                shuffle_side = random.choice(["left", "right", "top", "bottom"])
                if transition_value in (None, VideoTransitionMode.none.value):
                    clip = clip
                elif transition_value == VideoTransitionMode.fade_in.value:
                    clip = video_effects.fadein_transition(clip, 1)
                elif transition_value == VideoTransitionMode.fade_out.value:
                    clip = video_effects.fadeout_transition(clip, 1)
                elif transition_value == VideoTransitionMode.slide_in.value:
                    clip = video_effects.slidein_transition(clip, 1, shuffle_side)
                elif transition_value == VideoTransitionMode.slide_out.value:
                    clip = video_effects.slideout_transition(clip, 1, shuffle_side)
                elif transition_value == VideoTransitionMode.shuffle.value:
                    transition_funcs = [
                        lambda c: video_effects.fadein_transition(c, 1),
                        lambda c: video_effects.fadeout_transition(c, 1),
                        lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                        lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
                    ]
                    shuffle_transition = random.choice(transition_funcs)
                    clip = shuffle_transition(clip)

            # 对齐模式下片段时长就是那句话的音频时长(可能 >max_clip_duration),不能截断,
            # 否则画面会比那句话短、后面错位。只有混填模式才按 max_clip_duration 封顶。
            if not subclipped_item.target_display and clip.duration > max_clip_duration:
                clip = clip.subclipped(0, max_clip_duration)

            # wirte clip to temp file
            clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
            _write_videofile_with_codec_fallback(
                clip,
                clip_file,
                codec=_get_configured_video_codec(),
                logger=None,
                fps=fps,
            )

            # Store clip duration before closing
            clip_duration_saved = clip.duration
            close_clip(clip)

            return SubClippedVideoClip(
                file_path=clip_file,
                duration=clip_duration_saved,
                width=clip_w,
                height=clip_h,
                source_file_path=subclipped_item.source_file_path,
            )

        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
            return None

    # Add clips until the duration of the audio has been reached.
    # 对齐模式:每段对应一句话,必须全部处理,不能因为时长累计到音频长度就提前 break
    # (各段时长之和≈音频时长,提前 break 会漏掉最后一句的画面)。
    next_idx = 0
    for i, subclipped_item in enumerate(subclipped_items):
        if not aligned_mode and video_duration >= audio_duration:
            break

        next_idx = i + 1
        logger.debug(
            f"processing clip {i+1}: {subclipped_item.width}x{subclipped_item.height}, "
            f"source: {os.path.basename(subclipped_item.source_file_path)}, "
            f"current duration: {video_duration:.2f}s, "
            f"remaining: {audio_duration - video_duration:.2f}s"
        )

        result = _process_subclip(i, subclipped_item)
        if result:
            processed_clips.append(result)
            video_duration += result.duration

    # 素材硬性不复用约束:音频还没填满时，不再用 itertools.cycle 复制已经
    # 渲染好的片段(那样会产生完全相同的重复画面)。改成优先消耗
    # subclipped_items 里还没用过的剩余窗口——同一个源文件如果够长，
    # 这些窗口本来就是不同的时间段，画面不同，符合"同源不同片段允许、
    # 完全相同片段不许"的策略。
    if video_duration < audio_duration and next_idx < len(subclipped_items):
        logger.info(
            f"audio not yet covered ({audio_duration - video_duration:.2f}s short), "
            f"drawing from {len(subclipped_items) - next_idx} remaining unused windows "
            f"(distinct time ranges, not repeats)"
        )
        for i in range(next_idx, len(subclipped_items)):
            if video_duration >= audio_duration:
                break
            result = _process_subclip(i, subclipped_items[i])
            if result:
                processed_clips.append(result)
                video_duration += result.duration

    if video_duration < audio_duration:
        logger.warning(
            f"insufficient unique visual material: video covers {video_duration:.2f}s "
            f"but audio narration is {audio_duration:.2f}s ({audio_duration - video_duration:.2f}s "
            f"short). Per the hard no-repeat policy this will NOT loop an already-used "
            f"clip to pad the gap — the final video will simply be shorter than the "
            f"narration. Add more local materials covering this topic, or widen the "
            f"Pexels search terms so more distinct results are available."
        )

    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path
    
    # if there is only one clip, use it directly
    if len(processed_clips) == 1:
        logger.info("using single clip directly")
        shutil.copy(processed_clips[0].file_path, combined_video_path)
        delete_files([processed_clips[0].file_path])
        logger.info("video combining completed")
        return combined_video_path

    clip_files = [clip.file_path for clip in processed_clips]
    logger.info(f"concatenating {len(clip_files)} clips with ffmpeg")
    concat_video_clips_with_ffmpeg(
        clip_files=clip_files,
        output_file=combined_video_path,
        threads=threads,
        output_dir=output_dir,
    )
    
    # clean temp files
    delete_files(clip_files)
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # 字幕换行必须在真正创建 TextClip 前完成，否则 MoviePy 只会按原始文本
    # 计算渲染区域。这里用 PIL 按当前字体和字号测量宽度，确保每一行都尽量
    # 控制在视频可用宽度内，避免大字号或中文长句直接溢出画面。
    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, fontsize
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    def split_long_token(token):
        # 当一个 token 本身就超宽时（常见于中文无空格长句，或英文超长单词），
        # 退化为字符级拆分。关键点是：检测到 candidate 超宽时，先提交上一个
        # 仍然合法的 current，再把当前字符放入下一行，不能把超宽字符塞回上一行。
        lines = []
        current = ""
        for char in token:
            candidate = f"{current}{char}"
            candidate_width, _ = get_text_size(candidate)
            if candidate_width <= max_width or not current:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
        return lines

    lines = []
    current = ""
    words = text.split(" ")
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        word_width, _ = get_text_size(word)
        if word_width <= max_width:
            current = word
        else:
            lines.extend(split_long_token(word))
            current = ""

    if current:
        lines.append(current)

    line_start_punctuation = "，。！？；：、,.!?;:)]}）】》」』”’"
    for index in range(1, len(lines)):
        # 中文长句按字符拆分时，最后一个句号、逗号等闭合标点可能被单独
        # 放到下一行，导致字幕背景被异常撑高，视觉上像一个小点掉在正文
        # 下方。这里在不重新设计换行算法的前提下，把上一行最后一个字
        # 移到标点行前面，让标点跟随文字显示，兼容中英文常见闭合标点。
        if not lines[index] or lines[index][0] not in line_start_punctuation:
            continue
        if len(lines[index - 1]) <= 1:
            continue

        candidate = f"{lines[index - 1][-1]}{lines[index]}"
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            lines[index] = candidate
            lines[index - 1] = lines[index - 1][:-1]

    result = "\n".join(line.strip() for line in lines if line.strip()).strip()
    height = len(lines) * height
    return result, height


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    # 字幕背景色来自 API/WebUI 参数，可能为空或格式不规范。这里统一只接受
    # #RRGGBB 形式，非法值回退为黑色，避免 PIL 渲染阶段抛出异常中断任务。
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        try:
            return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        except ValueError:
            pass
    return (0, 0, 0)


def _rounded_subtitle_background_clip(
    width: int,
    height: int,
    color: str,
    alpha: int = 140,
    radius: int = 16,
) -> ImageClip:
    # 新字幕背景仅在用户显式开启时使用：通过 RGBA 图片绘制圆角半透明底板，
    # 再交给 MoviePy 作为透明 ImageClip 参与合成。这样默认路径完全不变，
    # 同时可以低成本试验更柔和的字幕视觉效果。
    rgb = _hex_to_rgb(color)
    safe_alpha = max(0, min(255, int(alpha)))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, max(0, width - 1), max(0, height - 1)],
        radius=max(0, int(radius)),
        fill=(rgb[0], rgb[1], rgb[2], safe_alpha),
    )
    return ImageClip(np.array(img), transparent=True)


def _get_visible_center_position(
    text_clip: TextClip,
    container_width: int,
    container_height: int,
) -> tuple[int, int]:
    """
    按文字真实可见像素把 TextClip 放到背景容器中心。

    MoviePy 的 TextClip 会按字体行高和 baseline 创建透明画布。很多字体的
    可见字形并不在这个画布的几何中心，直接 `with_position("center")`
    会把整块透明画布居中，导致字幕看起来偏上或偏下。这里读取 TextClip
    的透明 mask，只根据实际有像素的 bbox 计算偏移，让用户看到的文字
    在字幕背景里视觉居中。
    """
    x = int(round((container_width - text_clip.w) / 2))
    y = int(round((container_height - text_clip.h) / 2))

    try:
        if text_clip.mask is None:
            return x, y

        mask_frame = text_clip.mask.get_frame(0)
        ys, _ = np.where(mask_frame > 0.01)
        if len(ys) == 0:
            return x, y

        visible_top = int(ys.min())
        visible_bottom = int(ys.max())
        visible_height = visible_bottom - visible_top + 1
        y = int(round((container_height - visible_height) / 2 - visible_top))
    except Exception as exc:
        logger.debug(f"failed to center subtitle text by visible mask: {str(exc)}")

    return x, y


# ---------------------------------------------------------------------------
# "财经突发新闻"包装(news_mode，2026-06-18):lower-third标题条 + 底部ticker +
# 角标 + 新闻样式字幕(复用既有字幕背景逻辑，只改位置)。配色沿用项目墨色/
# 墨蓝(#16191c / #1c3a5b)，不做地方台式的大红大黄。
# ---------------------------------------------------------------------------

_NEWS_INK_BG = "#16191c"
_NEWS_ACCENT = "#1c3a5b"


def _news_ticker_height(video_height: int) -> int:
    return max(40, int(video_height * 0.035))


def _news_lower_third_height(video_height: int) -> int:
    return max(70, int(video_height * 0.06))


def _build_news_corner_badge_clip(
    badge_text: str, video_width: int, video_height: int, duration: float, font_path: str
):
    from datetime import datetime as _dt

    pad_x, pad_y = 18, 10
    date_text = _dt.now().strftime("%b %d, %Y").upper()
    badge_font_size = max(20, int(video_height * 0.018))
    date_font_size = max(14, int(video_height * 0.013))

    badge_text_clip = TextClip(text=badge_text, font=font_path, font_size=badge_font_size, color="#FFFFFF")
    date_text_clip = TextClip(text=date_text, font=font_path, font_size=date_font_size, color="#C7D0DA")

    box_w = max(badge_text_clip.w, date_text_clip.w) + pad_x * 2
    box_h = badge_text_clip.h + date_text_clip.h + pad_y * 3
    bg = _rounded_subtitle_background_clip(box_w, box_h, color=_NEWS_ACCENT, alpha=222, radius=6)

    badge_text_clip = badge_text_clip.with_position((pad_x, pad_y))
    date_text_clip = date_text_clip.with_position((pad_x, pad_y * 2 + badge_text_clip.h))

    composite = CompositeVideoClip(
        [bg, badge_text_clip, date_text_clip], size=(box_w, box_h)
    ).with_duration(duration)
    margin = int(video_height * 0.025)
    return composite.with_position((margin, margin))


def _build_news_ticker_text(subtitle_entries: list, subject: str) -> str:
    phrases = []
    for _times, text in subtitle_entries:
        cleaned = (text or "").strip().rstrip(".!?,").replace("\n", " ")
        if not cleaned:
            continue
        words = cleaned.split()
        phrases.append(" ".join(words[:9]).upper())
    if not phrases:
        phrases = [(subject or "MARKET UPDATE").upper()]
    joined = "   ●   ".join(phrases)
    # 拼接几遍，保证整段视频时长内 ticker 不会出现滚动到空白
    return (joined + "   ●   ") * 4


def _build_news_ticker_clip(
    ticker_text: str, video_width: int, video_height: int, duration: float, font_path: str
):
    ticker_h = _news_ticker_height(video_height)
    font_size = max(18, int(ticker_h * 0.46))

    bg = _rounded_subtitle_background_clip(video_width, ticker_h, color=_NEWS_INK_BG, alpha=228, radius=0)
    text_clip = TextClip(text=ticker_text, font=font_path, font_size=font_size, color="#E8EDF1")
    text_clip = text_clip.with_duration(duration)

    scroll_speed_px_per_s = 110
    loop_width = max(1, text_clip.w)

    def ticker_pos(t):
        x = video_width - (t * scroll_speed_px_per_s) % (loop_width + video_width)
        y = (ticker_h - text_clip.h) / 2
        return (x, y)

    text_clip = text_clip.with_position(ticker_pos)
    composite = CompositeVideoClip([bg, text_clip], size=(video_width, ticker_h)).with_duration(duration)
    return composite.with_position((0, video_height - ticker_h)), ticker_h


_NEWS_BRAND_YELLOW = "#F2B705"


def _build_news_lower_third_brand_clip(
    video_width: int,
    video_height: int,
    duration: float,
    bottom_offset: int,
    font_path: str,
    brand_text: str = "WikiFX News",
):
    """
    简化版 lower-third:不再按段显示要点标题(那样会和字幕内容完全重复)，
    改成固定的黄色品牌滚动条，整段视频只需要一个 clip，不用对齐字幕时间轴。
    """
    lt_h = _news_lower_third_height(video_height)
    font_size = max(24, int(lt_h * 0.4))

    bg = _rounded_subtitle_background_clip(video_width, lt_h, color=_NEWS_BRAND_YELLOW, alpha=235, radius=0)

    scroll_unit = brand_text.upper() + "   ★   "
    scroll_text = scroll_unit * 6
    text_clip = TextClip(text=scroll_text, font=font_path, font_size=font_size, color=_NEWS_INK_BG)
    text_clip = text_clip.with_duration(duration)

    scroll_speed_px_per_s = 90
    loop_width = max(1, text_clip.w)

    def brand_pos(t):
        x = video_width - (t * scroll_speed_px_per_s) % (loop_width + video_width)
        y = (lt_h - text_clip.h) / 2
        return (x, y)

    text_clip = text_clip.with_position(brand_pos)
    composite = CompositeVideoClip([bg, text_clip], size=(video_width, lt_h)).with_duration(duration)
    y = video_height - bottom_offset - lt_h
    composite = composite.with_position((0, y))
    return composite, lt_h


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def resolve_subtitle_background_color():
        # 兼容历史参数：API 里 `text_background_color` 既可能是布尔值，
        # 也可能是实际颜色字符串。统一在这里归一化，避免把 True/False
        # 直接传给 TextClip 后出现不可预期的渲染结果。
        if isinstance(params.text_background_color, bool):
            return "#000000" if params.text_background_color else None
        return params.text_background_color

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )
        has_subtitle_background = bool(bg_color)
        pad_x = int(params.font_size * 0.6) if has_subtitle_background else 0
        # 字幕背景需要给文字左右留出明确内边距。先从可用宽度中扣除
        # padding 再换行，避免长英文或大字号刚好撑满 90% 视频宽度后，
        # 文字贴到背景框边缘，看起来像被裁切。普通矩形背景和圆角背景
        # 都走这条逻辑；无背景字幕则保持原有最大宽度。
        text_max_width = max(1, int(max_width) - 2 * pad_x)
        wrapped_txt, txt_height = wrap_text(
            phrase,
            max_width=text_max_width,
            font=font_path,
            fontsize=params.font_size,
        )
        interline = int(params.font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(params.font_size * 0.35)
        text_clip_margin_y = max(
            int(params.font_size * 0.3), int(params.stroke_width * 2)
        )
        # MoviePy 在 `method=label` 下会自动收缩文本框高度，遇到多行字幕、
        # 描边或背景色时，容易把最后一行的下半部分裁掉。这里显式传入
        # 一个更保守的高度，把行间距和额外上下留白一并算进去，保证字幕
        # 背景框与文字本身都能完整渲染出来。
        clip_h = int(txt_height + vertical_padding + (interline * line_count))

        if rounded_bg_enabled:
            # 圆角背景需要贴合文字宽度，而不是沿用 90% 视频宽度。这里先用
            # PIL 测量最长一行文字，再加水平内边距，避免短字幕出现过宽底板。
            try:
                font = ImageFont.truetype(font_path, params.font_size)
                text_w = max(
                    int(font.getbbox(line)[2] - font.getbbox(line)[0])
                    for line in wrapped_txt.split("\n")
                )
            except Exception as exc:
                logger.warning(
                    f"failed to measure subtitle text width, fallback to max width: {str(exc)}"
                )
                text_w = int(max_width)

            box_w = max(1, min(int(max_width), text_w + 2 * pad_x))
            radius = max(8, int(params.font_size * 0.4))
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(box_w, None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            clip_h = max(clip_h, text_clip.h)
            bg_clip = _rounded_subtitle_background_clip(
                width=box_w,
                height=clip_h,
                color=bg_color,
                alpha=140,
                radius=radius,
            )
            text_position = _get_visible_center_position(text_clip, box_w, clip_h)
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=(box_w, clip_h),
            )
        elif bg_color:
            size = (
                int(max_width),
                clip_h,
            )
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(int(max_width), None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            size = (size[0], max(size[1], text_clip.h))
            bg_clip = _rounded_subtitle_background_clip(
                width=size[0],
                height=size[1],
                color=bg_color,
                alpha=255,
                radius=0,
            )
            text_position = _get_visible_center_position(text_clip, size[0], size[1])
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=size,
            )
        else:
            size = (
                int(max_width),
                clip_h,
            )
            _clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=size,
                text_align="center",
            )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            if getattr(params, "news_mode", False):
                # news_mode 下字幕要让位给下方的 lower-third + ticker，往上挪。
                news_stack_h = _news_ticker_height(video_height) + _news_lower_third_height(video_height)
                _clip = _clip.with_position(("center", video_height - news_stack_h - _clip.h - 14))
            else:
                _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    video_clip = _open_video_clip_quietly(video_path)
    audio_clip = AudioFileClip(audio_path).with_effects(
        [afx.MultiplyVolume(params.voice_volume)]
    )

    def make_textclip(text):
        return TextClip(
            text=text,
            font=font_path,
            font_size=params.font_size,
        )

    if subtitle_path and os.path.exists(subtitle_path):
        sub = SubtitlesClip(
            subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
        )
        text_clips = []
        for item in sub.subtitles:
            clip = create_text_clip(subtitle_item=item)
            text_clips.append(clip)

        news_overlay_clips = []
        if getattr(params, "news_mode", False):
            # lower-third/ticker/角标都复用字幕的句级时间轴(sub.subtitles)，
            # 不需要额外从 orchestrator 拉取分镜数据——脚本断句本来就和
            # orchestrator 的分段逻辑一致，字幕条目本身已经是"按段对齐"的。
            total_duration = video_clip.duration
            badge_text = getattr(params, "news_badge_text", "FOREX MARKET NEWS") or "FOREX MARKET NEWS"

            badge_clip = _build_news_corner_badge_clip(
                badge_text, video_width, video_height, total_duration, font_path
            )
            news_overlay_clips.append(badge_clip)

            ticker_text = _build_news_ticker_text(sub.subtitles, params.video_subject)
            ticker_clip, ticker_h = _build_news_ticker_clip(
                ticker_text, video_width, video_height, total_duration, font_path
            )
            news_overlay_clips.append(ticker_clip)

            lower_third_clip, _lt_h = _build_news_lower_third_brand_clip(
                video_width, video_height, total_duration, ticker_h, font_path
            )
            news_overlay_clips.append(lower_third_clip)

            logger.info("news_mode: added corner badge + ticker + WikiFX News brand banner")

        video_clip = CompositeVideoClip([video_clip, *text_clips, *news_overlay_clips])

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            bgm_clip = AudioFileClip(bgm_file).with_effects(
                [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                    afx.AudioLoop(duration=video_clip.duration),
                ]
            )
            audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")

    video_clip = video_clip.with_audio(audio_clip)
    # 显式沿用输入音频的采样率；如果取不到，再回退到 MoviePy 默认的 44100Hz。
    # 这样可以减少不同运行环境，尤其是 Docker 环境中再次重采样带来的音质波动。
    output_audio_fps = int(getattr(audio_clip, "fps", 0) or 44100)
    _write_videofile_with_codec_fallback(
        video_clip,
        output_file=output_file,
        codec=_get_configured_video_codec(),
        audio_codec=audio_codec,
        audio_fps=output_audio_fps,
        audio_bitrate=audio_bitrate,
        temp_audiofile_path=output_dir,
        threads=params.n_threads or 2,
        logger=None,
        fps=fps,
    )
    video_clip.close()
    del video_clip


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    # WebUI 在某些二次生成场景下可能传入空素材列表，这里直接返回空结果，避免抛出 NoneType 异常。
    if not materials:
        return []

    # 仅返回通过预处理校验的素材，避免低分辨率图片继续进入后续的视频合成流程。
    valid_materials = []
    local_videos_dir = utils.storage_dir("local_videos", create=True)

    for material in materials:
        if not material.url:
            continue

        try:
            material_source_path = file_security.resolve_path_within_directory(
                local_videos_dir, material.url
            )
        except ValueError as exc:
            # local video_source 的素材路径来自 API 参数，必须限制在专用素材目录。
            # 允许用户传文件名，也兼容历史返回的绝对路径，但不允许逃逸到系统
            # 其他目录，避免任意文件读取或通过 MoviePy 探测本地敏感文件。
            logger.warning(
                f"skip unsafe local material: {material.url}, "
                f"local_videos_dir: {local_videos_dir}, error: {str(exc)}"
            )
            continue

        ext = utils.parse_extension(material_source_path)
        try:
            # 图片素材直接按图片方式读取，避免先走 VideoFileClip 误判后触发不稳定的回退分支。
            if ext in const.FILE_TYPE_IMAGES:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            else:
                clip = _open_video_clip_quietly(material_source_path)
        except Exception:
            # 非标准扩展名或探测失败时再回退到图片模式，兼容历史上直接传本地图片路径的情况。
            try:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            except Exception as exc:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
        try:
            width = clip.size[0]
            height = clip.size[1]
            if width < 480 or height < 480:
                logger.warning(f"low resolution material: {width}x{height}, minimum 480x480 required")
                # 探测到低分辨率素材后立即关闭资源，并且不要把该素材返回给后续流程。
                close_clip(clip)
                continue

            if ext in const.FILE_TYPE_IMAGES:
                logger.info(f"processing image: {material_source_path}")
                # 探测尺寸时已经打开过一次素材，这里先释放探测句柄，再重新创建用于导出的图片 clip。
                close_clip(clip)
                # Create an image clip and set its duration to 3 seconds
                clip = (
                    ImageClip(material_source_path)
                    .with_duration(clip_duration)
                    .with_position("center")
                )
                # Apply a zoom effect using the resize method.
                # A lambda function is used to make the zoom effect dynamic over time.
                # The zoom effect starts from the original size and gradually scales up to 120%.
                # t represents the current time, and clip.duration is the total duration of the clip (3 seconds).
                # Note: 1 represents 100% size, so 1.2 represents 120% size.
                zoom_clip = clip.resized(
                    lambda t: 1 + (clip_duration * 0.03) * (t / clip.duration)
                )

                # Optionally, create a composite video clip containing the zoomed clip.
                # This is useful when you want to add other elements to the video.
                final_clip = CompositeVideoClip([zoom_clip])

                # Output the video to a file.
                video_file = f"{material_source_path}.mp4"
                final_clip.write_videofile(video_file, fps=30, logger=None)
                close_clip(clip)
                close_clip(final_clip)
                material.url = video_file
                logger.success(f"image processed: {video_file}")
            else:
                # 普通视频素材只需要读取尺寸做校验，校验完成后立即释放句柄即可。
                close_clip(clip)
                # Update url to the resolved absolute path so that downstream
                # stages (combine_videos) can open the file without re-resolving.
                material.url = material_source_path
        except Exception:
            close_clip(clip)
            raise

        valid_materials.append(material)

    return valid_materials
