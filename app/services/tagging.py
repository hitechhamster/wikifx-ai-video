"""
M2 Gemini video tagging + embedding pipeline.

run_tagging(): scan local_videos/, sha256-check against DB, call gemini-3.5-flash
               for structured tags, compute gemini-embedding-2 vector, write to DB.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from typing import Optional

from loguru import logger


TAGGING_PROMPT = """You are tagging video footage for a forex/financial media production system.
Analyze this video carefully and return ONLY a JSON object (no markdown fences, no extra text):
{
  "description": "one concise sentence describing the visual content in English",
  "tags": ["3-6 specific visual tags in English"],
  "topic_fit": ["relevant topics from this list only: forex, trading, chart, candlestick, currency, finance, office, technology, economy, banking"],
  "mood": "one of exactly: professional, tense, neutral, uplifting",
  "quality": <integer 1-10, production quality rating>,
  "has_watermark": <true or false>
}"""


def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_config() -> dict:
    import toml
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "..", "config.toml")
    return toml.load(cfg_path)["app"]


def _get_client():
    import google.genai as genai
    cfg = _load_config()
    return genai.Client(api_key=cfg["gemini_api_key"])


def _extract_frame_jpg(video_path: str, out_path: str, timestamp: float = 1.0) -> bool:
    """ffmpeg 截一帧静态图，用于"是否真实拍摄"快速判定——不用 Files API 上传整段
    视频，单帧判定够用而且快得多/便宜得多。"""
    from app.utils import utils

    ffmpeg_binary = utils.get_ffmpeg_binary()
    command = [
        ffmpeg_binary, "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-frames:v", "1",
        out_path,
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    return result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


_REAL_FOOTAGE_CACHE_FILE = "real_footage_cache.json"


def _real_footage_cache_path() -> str:
    from app.utils import utils
    return os.path.join(utils.storage_dir(), _REAL_FOOTAGE_CACHE_FILE)


def _load_real_footage_cache() -> dict:
    path = _real_footage_cache_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_real_footage_cache(cache: dict) -> None:
    # 原子写:并行跑多条视频时,直接 open(...,"w") 会先截断再写,另一个进程可能读到
    # 半截 JSON → _load 静默返回 {} → 整个判定缓存作废、所有素材重跑 Gemini 判定。
    # 先写同目录临时文件再 os.replace(Windows 上也是原子的),读者永远看到完整文件。
    path = _real_footage_cache_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"failed to save real_footage_cache: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


# 判定逻辑版本号:每次改 classify_footage 的判定 prompt/逻辑就 +1,让旧缓存自动失效。
# 否则升级了判定标准,之前判错的结论(比如把动效图判成"通过")会一直赖在缓存里被复用。
_CLASSIFY_LOGIC_VERSION = "v6"

# Gemini 相关性判定的网络超时(毫秒)+重试次数。
# 无超时会在连接挂住时无限死等 —— 批量并行时表现为某条视频永远卡在选材阶段
# (无 ffmpeg、输出不增长、日志停在 classify_footage),既不报错也不换下一个候选。
# 单帧判定很快,30s 足够;超时后有限重试,仍失败则由 except 分支优雅降级
# (图片候选→拒掉换下一张,视频候选→保守放行),绝不无限阻塞。
_CLASSIFY_TIMEOUT_MS = 30000
_CLASSIFY_RETRIES = 2


# 严格相关性开关(默认关)。开启后,相关性判定从"是否泛财经"收紧成"画面里是否清楚
# 出现主题本体"(黄金题材→必须真有金条/金币/金饰/金价屏,酒店/他国钞票/泛城市一律拒)。
# 视觉很具体的题材(黄金)开启;常规外汇新闻保持关闭(否则会把池子筛空)。
# 调用方(batch/task)在跑某条视频前 set,跑完 reset,避免污染同进程后续任务。
_STRICT_RELEVANCE = False


def set_strict_relevance(value: bool) -> None:
    global _STRICT_RELEVANCE
    _STRICT_RELEVANCE = bool(value)


def _footage_cache_key(sha: str, topic: str, strict: bool) -> str:
    """缓存键 = 逻辑版本 + 判定模式(松/严) + 文件 sha + 主题。真假判定与主题无关,但相关性
    判定依赖主题与松严模式,所以分别缓存;同一视频(同主题同模式)内重复命中的候选仍能复用。
    带版本号:判定逻辑一改,所有旧结论自动作废、重新判。严格模式单独缓存,不复用宽松模式的旧结论。"""
    topic_norm = (topic or "").strip().lower()
    base = f"{sha}|{topic_norm}" if topic_norm else sha
    mode = "strict" if (strict and topic_norm) else "std"
    return f"{_CLASSIFY_LOGIC_VERSION}|{mode}|{base}"



def _is_feng_shui_home_topic(topic: str) -> bool:
    t = (topic or "").lower()
    return (
        "feng shui" in t
        or "home interior" in t
        or "bedroom mirror" in t
        or "bed placement" in t
    )
def classify_footage(video_path: str, topic: str = "", strict: bool = None) -> bool:
    """
    一次 Gemini 调用同时判定:这段素材(取一帧)是否
      (1) 真实拍摄(非动画/插画/CG/motion-graphic),且
      (2) 与给定主题 topic 相关(财经/市场/经济/商业/货币/交易/银行/城市商务)。
    两者都满足才返回 True。

    为什么要带相关性:多样化搜索词会从图库捞回"贴合关键词但跟财经无关"的素材
    (例:非洲乡村妇女、野生动物、烹饪、体育),纯真假判定拦不住它们。相关性判定
    把这类明显跑题的素材挡掉,保持新闻画面切题。

    判定本身只做"宽松跑题过滤"(off-topic),不强求地标精确(stock 库里很难有真正的
    "美联储大楼",一栋普通写字楼算相关、放行;只有明显与财经无关才拒),否则会把池子
    筛空、镜头落空。

    topic 为空时退化为纯真假判定(向后兼容旧 classify_real_footage 行为)。
    判定服务故障时保守放行(True),避免分类器抖动连带触发"素材不足"硬失败。

    缓存(storage/real_footage_cache.json)按 sha+topic 键,同主题重复候选不重复调用。
    """
    # strict=None 时用全局开关;显式 True/False 可按调用点覆盖(视频段走宽松、图片插槽
    # 走严格——免费库黄金视频太少,视频段必须宽松才填得满;但强制插的那张图必须严格是黄金)。
    eff_strict = _STRICT_RELEVANCE if strict is None else bool(strict)
    try:
        sha = compute_sha256(video_path)
    except Exception:
        sha = None

    cache_key = _footage_cache_key(sha, topic, eff_strict) if sha else None
    if cache_key:
        cache = _load_real_footage_cache()
        if cache_key in cache:
            return cache[cache_key]

    result = _classify_footage_uncached(video_path, topic, eff_strict)

    if cache_key:
        cache = _load_real_footage_cache()
        cache[cache_key] = result
        _save_real_footage_cache(cache)

    return result


def classify_real_footage(video_path: str) -> bool:
    """向后兼容包装:只判真假、不判相关性(topic 为空)。"""
    return classify_footage(video_path, topic="")


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def _classify_footage_uncached(video_path: str, topic: str = "", strict: bool = False) -> bool:
    # 输入本身就是图片时直接读,不要再用 ffmpeg 抽帧:对静态图 `-ss` 抽帧会失败
    # (返回 0 字节),之前导致 ImageProvider 的图片永远抽帧失败→默认放行,相关性
    # 判定形同虚设(街头小贩这类跑题图全部漏过)。只有视频才需要抽一帧。
    is_image = os.path.splitext(video_path)[1].lower() in _IMAGE_EXTS
    frame_path = None
    if is_image:
        source_image_path = video_path
    else:
        frame_path = f"{video_path}.classify_frame.jpg"
        extracted = _extract_frame_jpg(video_path, frame_path, timestamp=1.0) or \
            _extract_frame_jpg(video_path, frame_path, timestamp=0.0)
        if not extracted:
            logger.warning(f"classify_footage: cannot extract frame from {video_path}, default to allow")
            return True
        source_image_path = frame_path

    try:
        from google.genai import types

        client = _get_client()
        model = _load_config().get("gemini_model_name", "gemini-3.5-flash")

        with open(source_image_path, "rb") as f:
            image_bytes = f.read()

        if topic and _is_feng_shui_home_topic(topic):
            question = (
                f"This frame is b-roll for an English Feng Shui home-layout explainer "
                f"about: \"{topic}\".\n"
                f"Answer GOOD only if the frame clearly shows an indoor residential "
                f"home/interior scene suitable for Feng Shui education: bedroom, bed, "
                f"wall mirror, wardrobe mirror, doorway/foyer inside a home, calm living "
                f"room, indoor plant by a window, warm lamp, or home hallway.\n"
                f"Answer REJECT if it shows cars, car mirrors, roads, streets, parking "
                f"garages, trains, buses, outdoor traffic, city exteriors, shops, offices, "
                f"hotels that do not read as home interiors, abstract water/nature shots, "
                f"unrelated people outside, text graphics, animation, cartoon, 3D render, "
                f"or anything not clearly home/interior Feng Shui related.\n"
                f"Answer with EXACTLY one word: GOOD or REJECT."
            )
        elif topic and strict:
            # 主题相关(中等严格):接受 (a) 真黄金/贵金属/金饰 或 (b) 明确的金融市场/
            # 交易画面(交易员看盘、K线行情屏、交易大厅、银行、金融数据)。这样大部分镜头
            # 用真实视频(黄金视频在免费库太少),又能挡掉明显跑题的(酒店/旅游/街景/他国
            # 钞票/比特币)。比"必须出现金条"宽,比纯泛财经(会放行酒店)严。
            question = (
                f"This frame is b-roll for a video about: \"{topic}\" (gold prices / gold market).\n"
                f"Answer GOOD if the frame shows EITHER:\n"
                f"  (a) actual gold, precious metal, gold jewelry, or gold bars/coins; OR\n"
                f"  (b) clearly financial-market or trading footage: traders at screens, market "
                f"price charts or candlesticks, a trading floor, a bank, or financial data.\n"
                f"Answer REJECT for anything off-topic: hotels, generic buildings, city streets, "
                f"tourism, shops, restaurants, festivals, nature, unrelated people, or banknotes "
                f"of an unrelated currency shown by themselves.\n"
                f"Also REJECT cryptocurrency / Bitcoin coins, novelty / commemorative coins, and "
                f"animation / cartoon / 3D render / illustration / motion-graphic.\n"
                f"Answer with EXACTLY one word: GOOD or REJECT."
            )
        elif topic:
            # 便宜模型(flash-lite)在"分类表"式长 prompt 下相关性判得很松(街头小贩
            # 都放行)。实测发现:换成"反问句 + 否决项 inline + 二元答案"的尖锐问法,
            # 同一个 lite 模型就能答对。所以这里刻意用 GOOD/REJECT 二元 + 反问。
            question = (
                f"Is this frame appropriate for a SERIOUS FINANCIAL MARKETS news report "
                f"about \"{topic}\" (trading, banks, money, corporate offices, stock/forex "
                f"markets, financial district)?\n"
                f"A street market, street vendor, informal retail, everyday shopping, "
                f"festival, toys, food, cooking, farming, nature, wildlife, sports, tourism, "
                f"children, or casual everyday people are NOT appropriate.\n"
                f"Cryptocurrency / Bitcoin / crypto: a Bitcoin or crypto coin, a Bitcoin/crypto "
                f"logo or ticker, or a crypto trading screen are NOT appropriate (this is about "
                f"traditional markets/gold, not crypto).\n"
                f"Animation / cartoon / 3D render / illustration / motion-graphic is also "
                f"NOT appropriate.\n"
                f"Answer with EXACTLY one word: GOOD if appropriate, or REJECT if not."
            )
        else:
            question = (
                "Is this image a real filmed photograph/video frame, or is it animated, "
                "illustrated, CG, motion-graphic, or cartoon content? Respond with exactly "
                "one word: REAL or ANIMATED."
            )

        # 带超时的 generate_content:超时后有限重试,彻底失败则抛出交给下方 except 降级。
        gen_config = types.GenerateContentConfig(
            http_options=types.HttpOptions(timeout=_CLASSIFY_TIMEOUT_MS)
        )
        response = None
        last_err = None
        for attempt in range(_CLASSIFY_RETRIES + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        question,
                    ],
                    config=gen_config,
                )
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    f"classify_footage: generate_content attempt {attempt + 1} failed ({e})"
                )
        if response is None:
            raise last_err or RuntimeError("classify_footage: generate_content failed")
        answer = (response.text or "").strip().upper()

        if topic:
            accepted = "GOOD" in answer and "REJECT" not in answer
            if not accepted:
                logger.info(
                    f"classify_footage: rejected {os.path.basename(video_path)} "
                    f"for topic '{topic[:40]}' (model said: {answer[:30]})"
                )
            return accepted

        is_real = "REAL" in answer and "ANIMATED" not in answer
        if not is_real:
            logger.info(
                f"classify_footage: rejected as non-real footage: "
                f"{os.path.basename(video_path)} (model said: {answer})"
            )
        return is_real
    except Exception as e:
        # 图片候选很多,判定失败(尤其 400「无法处理该图片」=图损坏/格式异常)时直接
        # 拒掉换下一张——否则会把坏图当通过、再喂给 ffmpeg 渲 Ken Burns,可能把 ffmpeg
        # 卡死、整个进程挂起。视频候选稀缺,仍保守放行(避免分类器抖动误报"素材不足")。
        if is_image:
            logger.warning(f"classify_footage: image classification failed ({e}), rejecting this image")
            return False
        logger.warning(f"classify_footage: classification failed ({e}), default to allow (video)")
        return True
    finally:
        # 只删 ffmpeg 抽出来的临时帧;输入本身是图片时不能删原图。
        if frame_path and os.path.exists(frame_path):
            try:
                os.remove(frame_path)
            except OSError:
                pass


def tag_one(path: str, client, tagging_model: str) -> Optional[dict]:
    """Upload one video to Files API, get Gemini structured tags. Returns None on failure."""
    from google.genai import types

    file_ref = None
    try:
        with open(path, "rb") as f:
            file_ref = client.files.upload(
                file=f,
                config=types.UploadFileConfig(
                    mime_type="video/mp4",
                    display_name=os.path.basename(path),
                ),
            )

        # Wait for ACTIVE
        for _ in range(20):
            file_ref = client.files.get(name=file_ref.name)
            state = str(file_ref.state)
            if "ACTIVE" in state:
                break
            if "FAILED" in state:
                logger.error(f"tagging: file processing failed: {path}")
                return None
            time.sleep(3)
        else:
            logger.error(f"tagging: timeout waiting for ACTIVE: {path}")
            return None

        response = client.models.generate_content(
            model=tagging_model,
            contents=[
                types.Part.from_uri(file_uri=file_ref.uri, mime_type="video/mp4"),
                TAGGING_PROMPT,
            ],
        )
        raw = response.text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:])
            if raw.endswith("```"):
                raw = raw[:-3].strip()
            elif "```" in raw:
                raw = raw.rsplit("```", 1)[0].strip()

        return json.loads(raw)

    except json.JSONDecodeError as e:
        logger.warning(f"tagging: JSON parse failed for {path}: {e} — raw: {raw[:200]}")
        return None
    except Exception as e:
        logger.error(f"tagging: error for {path}: {e}")
        return None
    finally:
        if file_ref is not None:
            try:
                client.files.delete(name=file_ref.name)
            except Exception:
                pass


def embed_text(text: str, client, embedding_model: str) -> Optional[list]:
    """Compute RETRIEVAL_DOCUMENT embedding for a material description+tags."""
    from google.genai import types
    try:
        result = client.models.embed_content(
            model=embedding_model,
            contents=text,
            config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
        )
        return list(result.embeddings[0].values)
    except Exception as e:
        logger.error(f"embed_text failed: {e}")
        return None


def reembed_all(db_path: str = None) -> int:
    """
    Re-compute embeddings for all already-tagged records using the current
    embedding model (does NOT re-call Gemini Files API / vision).
    Returns number of records re-embedded.
    """
    from app.services import library as lib

    cfg = _load_config()
    embedding_model = cfg.get("gemini_embedding_model", "gemini-embedding-001")
    client = _get_client()

    records = lib.list_all(db_path)
    tagged = [r for r in records if r.description and not r.description.startswith("[pending")]
    logger.info(f"reembed_all: {len(tagged)} tagged records → {embedding_model}")

    count = 0
    for r in tagged:
        embed_input = r.description + ". " + ", ".join(r.tags)
        vec = embed_text(embed_input, client, embedding_model)
        if vec:
            lib.update_material_tags(
                path=r.path,
                sha256=r.sha256,
                description=r.description,
                tags=r.tags,
                topic_fit=r.topic_fit,
                mood=r.mood,
                quality=r.quality,
                has_watermark=r.has_watermark,
                embedding=vec,
                db_path=db_path,
            )
            count += 1
            logger.info(
                f"  re-embedded [{r.id}] {os.path.basename(r.path)} dim={len(vec)}"
            )
    logger.success(f"reembed_all complete: {count}/{len(tagged)} records updated")
    return count


def run_tagging(local_dir: str = None, db_path: str = None, max_retries: int = 2) -> dict:
    """
    Tag all mp4 files in local_dir.
    Skips files whose sha256 already matches DB (incremental cache).
    Retries tag_one() up to max_retries times on failure (transient API
    errors, Files API hiccups) before counting a file as failed.
    Returns {"tagged": N, "skipped": N, "failed": N}.
    """
    from app.services import library as lib
    from app.utils.utils import storage_dir

    if local_dir is None:
        local_dir = os.path.join(storage_dir(), "local_videos")

    cfg = _load_config()
    tagging_model = cfg.get("gemini_model_name", "gemini-3.5-flash")
    embedding_model = cfg.get("gemini_embedding_model", "gemini-embedding-001")

    lib.init_db(db_path)
    existing = {r.path: r for r in lib.list_all(db_path)}

    mp4s = sorted(
        os.path.join(local_dir, f)
        for f in os.listdir(local_dir)
        if f.endswith(".mp4")
    )
    if not mp4s:
        logger.warning(f"tagging: no mp4 files found in {local_dir}")
        return {"tagged": 0, "skipped": 0, "failed": 0}

    client = _get_client()
    stats = {"tagged": 0, "skipped": 0, "failed": 0}

    for path in mp4s:
        sha = compute_sha256(path)
        rec = existing.get(path)

        if rec and rec.sha256 == sha and rec.sha256:
            logger.info(f"sha256 cache hit — skipping: {os.path.basename(path)}")
            stats["skipped"] += 1
            continue

        tag_data = None
        for attempt in range(1, max_retries + 2):
            logger.info(f"tagging: {os.path.basename(path)} (attempt {attempt}/{max_retries + 1}) ...")
            tag_data = tag_one(path, client, tagging_model)
            if tag_data is not None:
                break
            if attempt <= max_retries:
                wait = attempt * 5
                logger.warning(f"  attempt {attempt} failed, retrying in {wait}s ...")
                time.sleep(wait)

        if tag_data is None:
            logger.error(f"  giving up after {max_retries + 1} attempts: {os.path.basename(path)}")
            stats["failed"] += 1
            continue

        description = tag_data.get("description", "")
        tags = tag_data.get("tags", [])
        embed_input = description + ". " + ", ".join(tags)
        embedding = embed_text(embed_input, client, embedding_model)

        lib.update_material_tags(
            path=path,
            sha256=sha,
            description=description,
            tags=tags,
            topic_fit=tag_data.get("topic_fit", []),
            mood=tag_data.get("mood", "neutral"),
            quality=float(tag_data.get("quality", 5)),
            has_watermark=bool(tag_data.get("has_watermark", False)),
            embedding=embedding,
            db_path=db_path,
        )

        logger.success(
            f"  tagged: {os.path.basename(path)}\n"
            f"    desc: {description}\n"
            f"    tags: {tags}\n"
            f"    topic_fit: {tag_data.get('topic_fit', [])}\n"
            f"    mood={tag_data.get('mood')} quality={tag_data.get('quality')} "
            f"watermark={tag_data.get('has_watermark')} "
            f"embed_dim={len(embedding) if embedding else 0}"
        )
        stats["tagged"] += 1

    logger.info(
        f"tagging complete: tagged={stats['tagged']} "
        f"skipped={stats['skipped']} failed={stats['failed']}"
    )
    return stats

