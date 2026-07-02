import os
import random
import ssl
import subprocess
import threading
import time
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    item.thumbnail = v.get("image", "")   # 预览缩略图
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    # 优先用该尺寸自带 thumbnail;没有就退到 hit 级 picture_id 缩略图
                    item.thumbnail = video.get("thumbnail", "") or v.get("userImageURL", "")
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Coverr (https://coverr.co) - free HD/4K stock videos,
    subject to Coverr license terms (https://coverr.co/license).

    Coverr API notes (based on official docs at api.coverr.co/docs/):
      - 鉴权: Authorization: Bearer <api_key>
      - 搜索端点: GET /videos?query=...,响应结构 {"hits": [...], ...}
      - 加 ?urls=true 在搜索响应里直接返回 mp4 直链
      - URL 是 signed JWT(绑定 API key,无过期时间)
      - Coverr 库以 16:9 横屏为主,9:16 portrait 占比极低(约 1%)
        因此本函数不做 aspect_ratio 过滤,由下游 video.py 的
        resize + letterbox 逻辑统一处理
      - duration 字段同时存在 number 和 string 两种形态,本函数都接受

    本函数使用 urls.mp4_download 字段作为下载地址 —— 按 Coverr 官方文档
    (https://api.coverr.co/docs/videos/#download-a-video) 的说法,
    GET 这个 URL 本身就被 Coverr 当作一次合法的 download 事件计入统计,
    无需再调用 PATCH /videos/:id/stats/downloads。
    """
    api_key = get_api_key("coverr_api_keys")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "query": search_term,
        "page_size": 20,
        "urls": "true",
        "sort": "popular",
    }
    query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items: List[MaterialInfo] = []

        if not isinstance(response, dict) or "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items

        for v in response["hits"]:
            # duration 在不同响应里可能是 number(11.625) 或 string("10.500000")
            try:
                duration = int(float(v.get("duration") or 0))
            except (TypeError, ValueError):
                continue
            if duration < minimum_duration:
                continue

            video_id = v.get("id")
            mp4_download_url = (v.get("urls") or {}).get("mp4_download")
            if not video_id or not mp4_download_url:
                continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.duration = duration
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


# 在线图库源注册表:(名称, 搜索函数, config 里的 key 字段名)。
# 想接更多免费图库(只要返回 MaterialInfo 列表)在这里加一行即可。
_STOCK_SOURCES = (
    ("pexels", search_videos_pexels, "pexels_api_keys"),
    ("pixabay", search_videos_pixabay, "pixabay_api_keys"),
    ("coverr", search_videos_coverr, "coverr_api_keys"),
)


def search_videos_multi(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    聚合所有"已配置 API key"的免费图库源,合并候选,扩大可选素材池。

    系列化产出外汇视频时,单靠 Pexels 一个源 + 真实素材过滤,候选池太小,不同
    视频反复撞同一批素材。把 Pixabay/Coverr 也并进来能把池子翻几倍。

    某个源的 key 未配置就静默跳过(不报错),所以用户还没填 Pixabay key 时,系统
    照常只用 Pexels 跑;单个源请求失败也只跳过该源,不影响其它源。
    """
    merged: List[MaterialInfo] = []
    used_sources: List[str] = []
    for name, search_fn, cfg_key in _STOCK_SOURCES:
        if not config.app.get(cfg_key):
            continue  # 该源 key 未配置,跳过
        try:
            items = search_fn(search_term, minimum_duration, video_aspect)
            if items:
                merged.extend(items)
                used_sources.append(f"{name}:{len(items)}")
        except Exception as e:
            logger.warning(f"stock source {name} failed for '{search_term}': {e}")

    logger.info(
        f"search_videos_multi('{search_term}'): {len(merged)} candidates "
        f"from [{', '.join(used_sources) or 'none'}]"
    )
    return merged


def search_images_pixabay(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    搜 Pixabay 图片库(和视频用同一个 key)。图片库体量比视频大一个数量级,做
    "偶尔插一张静态图(配 Ken Burns 运镜)"的素材来源,既丰富节奏又进一步扩池。

    image_type=photo 只取真实照片,天然排除插画/矢量图,所以不需要再走
    classify_real_footage(省一次 Gemini 调用)。MaterialInfo.url 存大图直链,
    provider 标 'pixabay_image' 以便下游区分(图片要走运镜渲染,不是直接当视频)。
    """
    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    orientation = "vertical" if video_height >= video_width else "horizontal"

    api_key = get_api_key("pixabay_api_keys")
    params = {
        "key": api_key,
        "q": search_term,
        "image_type": "photo",
        "orientation": orientation,
        "per_page": 50,
        "safesearch": "true",
    }
    query_url = f"https://pixabay.com/api/?{urlencode(params)}"
    logger.info(f"searching images: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        items: List[MaterialInfo] = []
        if "hits" not in response:
            logger.error(f"search images failed: {response}")
            return items
        for v in response["hits"]:
            url = v.get("largeImageURL") or v.get("webformatURL")
            if not url:
                continue
            item = MaterialInfo()
            item.provider = "pixabay_image"
            item.url = url
            item.duration = 0
            items.append(item)
        return items
    except Exception as e:
        logger.error(f"search images failed: {str(e)}")
    return []


def save_image(image_url: str, save_dir: str = "") -> str:
    """下载一张图片到缓存目录,按 URL 哈希去重缓存(和 save_video 同款策略)。"""
    if not save_dir:
        save_dir = utils.storage_dir("cache_images")
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = image_url.split("?")[0]
    ext = os.path.splitext(url_without_query)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    img_path = f"{save_dir}/img-{utils.md5(url_without_query)}{ext}"

    if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
        return img_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    last_error = None
    for attempt in range(3):
        try:
            content = requests.get(
                image_url, headers=headers, proxies=config.proxy,
                verify=_get_tls_verify(), timeout=(30, 120),
            ).content
            last_error = None
            break
        except (requests.exceptions.RequestException, ssl.SSLError) as e:
            last_error = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if last_error is not None:
        raise last_error

    with open(img_path, "wb") as f:
        f.write(content)
    if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
        return img_path
    return ""


def render_ken_burns_clip(
    image_path: str,
    duration: float,
    video_aspect: VideoAspect = VideoAspect.portrait,
    save_dir: str = "",
) -> str:
    """
    把一张静态图渲成带 Ken Burns 缓慢运镜(缓推+轻微平移)的小 mp4,让"静态图"
    看起来有呼吸感、不死板。输出是普通 mp4,下游 combine_videos 当成常规片段处理,
    管线其它部分完全无感。

    实现要点(zoompan 容易出锯齿,这里几个关键防抖):
    - 先把图按"填满竖屏"放大并裁切(force_original_aspect_ratio=increase + crop),
      保证无黑边、且源够大;zoompan 再在大图上做缩放,避免整数步进抖动。
    - 随机在"缓推/缓拉"之间挑一种 + 随机平移方向,系列化产出时多张图不雷同。
    """
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    aspect = VideoAspect(video_aspect)
    w, h = aspect.to_resolution()
    fps = 30
    total_frames = max(1, int(round(duration * fps)))
    # 工作分辨率放大到目标 2 倍,给 zoompan 足够像素做平滑缩放
    work_w, work_h = w * 2, h * 2

    out_path = (
        f"{save_dir}/kb-{utils.md5(image_path + str(duration))}.mp4"
    )
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    zoom_in = random.random() < 0.5
    if zoom_in:
        z_expr = f"min(1+0.0012*on,1.18)"
    else:
        # 缓拉:从放大状态慢慢回到原大小
        z_expr = f"if(eq(on,0),1.18,max(1.18-0.0012*on,1.0))"

    # 轻微平移(随机方向),让运镜不只是正中缩放
    pan = random.choice([
        ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),               # 居中
        ("(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),            # 向右
        ("(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),        # 向左
        ("iw/2-(iw/zoom/2)", "(ih-ih/zoom)*on/{n}"),            # 向下
    ])
    x_expr = pan[0].format(n=total_frames)
    y_expr = pan[1].format(n=total_frames)

    vf = (
        f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
        f"crop={work_w}:{work_h},"
        f"zoompan=z='{z_expr}':d={total_frames}:x='{x_expr}':y='{y_expr}':"
        f"s={w}x{h}:fps={fps}"
    )

    ffmpeg_binary = utils.get_ffmpeg_binary()
    command = [
        ffmpeg_binary, "-y",
        "-loop", "1",
        "-i", image_path,
        "-t", f"{duration}",
        "-vf", vf,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        out_path,
    ]
    # 加超时:个别异常/损坏图片会让 zoompan 卡住不退,无超时会挂死整条流水线。
    # 超时就当渲染失败返回空,上层换下一张候选。
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=120)
    except subprocess.TimeoutExpired:
        logger.error(f"render_ken_burns_clip timed out (>120s) for {image_path}")
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        return ""
    if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        stderr = (result.stderr or b"").decode("utf-8", "ignore")[-500:]
        logger.error(f"render_ken_burns_clip failed for {image_path}: {stderr}")
        return ""
    return out_path


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    # 瞬时网络故障(SSL 握手坏记录/连接被重置/超时)不应该直接打死整个任务——
    # 之前没有重试，一次偶发的 SSLError 就让整条任务线程未捕获崩溃。
    last_error = None
    content = None
    for attempt in range(3):
        try:
            content = requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
            ).content
            last_error = None
            break
        except (requests.exceptions.RequestException, ssl.SSLError) as e:
            last_error = e
            logger.warning(
                f"save_video: download attempt {attempt + 1}/3 failed for "
                f"{video_url}: {e}"
            )
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if last_error is not None:
        raise last_error

    with open(video_path, "wb") as f:
        f.write(content)

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
) -> List[str]:
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "coverr":
        search_videos = search_videos_coverr

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )

    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    按脚本文案顺序下载素材。

    默认下载逻辑会把所有关键词的候选素材合并成一个大列表；如果第一个
    关键词返回很多结果，最终下载时可能一直消耗这个关键词的素材，后续
    脚本主题就排不上时间线。这里按关键词分组后轮询下载：
    第 1 轮取每个关键词的第 1 个候选，第 2 轮取每个关键词的第 2 个候选。
    这样在不重写视频合成引擎的前提下，尽量保证素材顺序贴近文案顺序。
    """
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        term_items = []
        for item in video_items:
            if item.url in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(item.url)
            found_duration += item.duration

        if term_items:
            candidate_groups.append((search_term, term_items))

    logger.info(
        f"found total ordered video candidates: {sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    video_paths = []
    total_duration = 0.0
    candidate_index = 0
    while candidate_groups and total_duration <= audio_duration:
        has_candidate = False
        for search_term, term_items in candidate_groups:
            if candidate_index >= len(term_items):
                continue

            has_candidate = True
            item = term_items[candidate_index]
            try:
                logger.info(
                    f"downloading ordered video for '{search_term}': {item.url}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                logger.error(
                    f"failed to download ordered video: {utils.to_json(item)} => {str(e)}"
                )

        if not has_candidate:
            break
        candidate_index += 1

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
