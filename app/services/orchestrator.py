"""
Core orchestration engine (replaces task.py's material-selection logic).

Flow:
  split_script → [segments]
  per segment: generate_visual_intent → LocalProvider → (fallback) PexelsProvider
  ensure_min_local (hard constraint)
  return ordered [video_paths], [ShotAssignment log]
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from loguru import logger

from app.models.schema import VideoAspect
from app.services.providers import (
    ImageProvider,
    LocalProvider,
    MaterialResult,
    PexelsProvider,
)


# 单条视频额外补抓的独立 b-roll 上限,控制下载量与判定调用成本(每条多几次而已)。
_MAX_EXTRA_BROLL_CLIPS = 10


@dataclass
class ShotAssignment:
    segment_index: int
    segment_text: str
    visual_intent: str
    material_path: str = ""
    source: str = ""       # "local" | "local_forced" | "pexels" | "pending"
    score: float = 0.0


# ---------------------------------------------------------------------------
# Script segmentation
# ---------------------------------------------------------------------------

def split_script(script: str) -> List[str]:
    """
    Split script at sentence boundaries (CN + EN terminators).
    Very short fragments (< 10 chars) are merged into the preceding segment.
    """
    # 中文句末标点(。！？)后面无论有无空格都断句;英文 .!? 只在"后面跟空白"时才断,
    # 这样 "1.15"、"U.S." 这种小数/缩写里的点不会被误当句号(之前 "below 1.15" 会被
    # 切成 "below 1." 一个 1 秒的截断碎片)。
    parts = re.split(r"(?<=[。！？])\s*|(?<=[.!?])(?=\s)", script.strip())
    segments: List[str] = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        buf = (buf + p) if buf else p
        if len(buf) >= 10:
            segments.append(buf)
            buf = ""
    if buf:
        if segments:
            segments[-1] += buf
        else:
            segments.append(buf)
    return segments or [script.strip()]


# ---------------------------------------------------------------------------
# Visual intent generation
# ---------------------------------------------------------------------------

def generate_visual_intent(segment: str, subject: str, diversify: bool = True) -> str:
    """
    Ask LLM for 3 English stock-footage search terms for this segment.
    Falls back to simple keyword extraction if LLM fails.

    diversify:开启"泛 b-roll"模式,不再把视频主题词(forex/dollar)硬塞进每个
    搜索词,而是产出更具体、更多样、可拍摄的财经/商业/城市/人物画面。系列化
    产出时这能显著扩大同一批源里的可用素材,缓解视频间撞车(详见 llm.generate_terms)。
    """
    try:
        from app.services.llm import generate_terms
        terms = generate_terms(
            video_subject=subject,
            video_script=segment,
            amount=3,
            diversify_broll=diversify,
        )
        if isinstance(terms, list) and terms:
            return ", ".join(str(t) for t in terms)
        if isinstance(terms, str) and terms:
            return terms
    except Exception as exc:
        logger.warning(f"intent LLM failed: {exc}")

    # Fallback: first 5 non-trivial words
    words = [w for w in re.findall(r"[a-zA-Z一-鿿]+", segment) if len(w) > 1]
    return " ".join(words[:5]) or subject


# ---------------------------------------------------------------------------
# Min-local hard constraint
# ---------------------------------------------------------------------------

def _ensure_min_local(
    shots: List[ShotAssignment],
    min_local: int,
    already_used: set = None,
) -> List[ShotAssignment]:
    local_count = sum(1 for s in shots if s.source == "local")
    if local_count >= min_local:
        return shots

    needed = min_local - local_count
    logger.info(
        f"min_local constraint: {local_count} local < {min_local} required, "
        f"forcing {needed} more"
    )

    from app.services import library as lib

    # Build exclusion set from already-assigned local paths
    used_paths = {s.material_path for s in shots if s.source == "local"}
    if already_used:
        used_paths |= already_used

    # Candidate pool (larger than needed to have room for semantic picking)。
    # min_duration 放宽到 1.0:get_top_quality 默认按 3.0 过滤,会把短品牌片误筛掉,
    # 导致 min_local 静默失效(实际镜头只需 ~2.2s,不该要求素材 >=3s)。
    pool = lib.get_top_quality(n=20, min_duration=1.0)
    pool = [r for r in pool if r.path not in used_paths]

    if not pool:
        logger.warning(
            "min_local: 本地库没有可用素材(文件可能已从磁盘删除,或库为空)。"
            "本条视频将全部使用在线素材。如需保证品牌镜头出镜,请先导入本地素材。"
        )
        return shots

    # Replace non-local shots — lowest score first, pick semantically best local for each
    non_local = sorted(
        [(i, s) for i, s in enumerate(shots) if s.source not in ("local", "local_forced")],
        key=lambda x: x[1].score,
    )

    assigned_in_this_pass: set = set()
    for idx, shot in non_local[:needed]:
        available = [r for r in pool if r.path not in assigned_in_this_pass]
        if not available:
            break

        # M2: pick by semantic relevance to the shot's visual_intent
        best_rec, best_score = lib.find_best_match_for_intent(shot.visual_intent, available)
        if best_rec is None:
            break

        old = shots[idx].source
        shots[idx].material_path = best_rec.path
        shots[idx].source = "local_forced"
        shots[idx].score = best_score
        assigned_in_this_pass.add(best_rec.path)
        logger.info(
            f"  forced local into seg[{idx}] (was {old}): "
            f"{os.path.basename(best_rec.path)} semantic_score={best_score:.3f}"
        )

    return shots


# ---------------------------------------------------------------------------
# Shared planning steps (no network downloads — safe to call for preview)
# ---------------------------------------------------------------------------

def _plan_local(script: str, params) -> List[ShotAssignment]:
    """
    Steps 1-3: segment script, generate per-segment visual intent (LLM call),
    match against local library, apply min_local hard constraint.
    No Pexels download happens here — unresolved shots stay source="pending".
    """
    local_threshold: float = getattr(params, "local_threshold", 0.3)
    min_local: int = getattr(params, "min_local_segments", 2)
    aspect: VideoAspect = params.video_aspect or VideoAspect.portrait
    clip_duration: float = params.video_clip_duration or 5

    local_prov = LocalProvider(threshold=local_threshold)

    segments = split_script(script)
    logger.info(f"[orchestrator] {len(segments)} segments, threshold={local_threshold}, min_local={min_local}")

    diversify: bool = getattr(params, "diversify_broll", True)
    shots: List[ShotAssignment] = []
    used_local_paths: set = set()
    for i, seg in enumerate(segments):
        intent = generate_visual_intent(seg, params.video_subject, diversify=diversify)
        logger.info(f"  seg[{i}] intent: '{intent}'  ('{seg[:50]}')")

        result: Optional[MaterialResult] = local_prov.fetch(
            intent, aspect, clip_duration, exclude_paths=used_local_paths
        )
        if result:
            used_local_paths.add(result.path)
            shots.append(ShotAssignment(
                segment_index=i,
                segment_text=seg,
                visual_intent=intent,
                material_path=result.path,
                source="local",
                score=result.score,
            ))
        else:
            shots.append(ShotAssignment(
                segment_index=i,
                segment_text=seg,
                visual_intent=intent,
                source="pending",
            ))

    shots = _ensure_min_local(shots, min_local, used_local_paths)
    return shots


def preview(script: str, params) -> List[ShotAssignment]:
    """
    Same planning as orchestrate() steps 1-3, but stops before Pexels download.
    For the M4b "编排预览" UI: shows segments/intents/local matches BEFORE
    burning Pexels API calls or running the synthesis pipeline.
    Shots still "pending" after local matching are reported as source="pexels_preview"
    (no actual download — caller knows it will hit Pexels at generation time).
    """
    shots = _plan_local(script, params)
    for shot in shots:
        if shot.source == "pending":
            shot.source = "pexels_preview"
    return shots


# ---------------------------------------------------------------------------
# Main orchestration entry point
# ---------------------------------------------------------------------------

def orchestrate(
    task_id: str,
    params,
    script: str,
    audio_duration: float,
) -> Tuple[List[str], List[ShotAssignment]]:
    """
    Returns (ordered_video_paths, shot_log).
    Caller passes result directly to combine_videos — no preprocess_video needed.
    """
    aspect: VideoAspect = params.video_aspect or VideoAspect.portrait
    clip_duration: float = params.video_clip_duration or 5
    pexels_prov = PexelsProvider()
    image_prov = ImageProvider()
    # 每隔 image_insert_every 个待填充镜头插一段"静态图+Ken Burns 运镜"片段,丰富
    # 节奏;0=关闭。只替换本会走在线视频的镜头(不动本地品牌素材)。
    image_insert_every: int = getattr(params, "image_insert_every", 5)

    shots = _plan_local(script, params)

    # 跨视频冷却去重:预加载最近 N 条视频用过的在线图库 URL,作为软排除,让系列化
    # 产出时不同视频优先挑没在最近用过的素材。只作用于在线图库源,不碰本地库
    # (本地是少量自有品牌 b-roll,跨视频复用是合理的)。详见 usage_history 模块。
    from app.services import usage_history
    cooldown_videos: int = getattr(params, "material_cooldown_videos", 8)
    try:
        video_seq = usage_history.reserve_video_seq()
        cooldown_urls: set = usage_history.get_cooldown_urls(cooldown_videos)
    except Exception as e:
        logger.warning(f"usage_history unavailable, cross-video cooldown disabled: {e}")
        video_seq, cooldown_urls = 0, set()
    if cooldown_urls:
        logger.info(
            f"[orchestrator] cross-video cooldown: excluding {len(cooldown_urls)} "
            f"URLs used in the last {cooldown_videos} video(s)"
        )

    # 4. Fill pending shots via stock providers — hard cross-segment dedup: similar
    # intents across segments can pull the same top search result, so track
    # used URLs across the whole video and skip them (provider walks past
    # already-used candidates instead of always taking the top hit). On top of
    # that, cooldown_urls softly steers away from recently-used material.
    used_stock_urls: set = set()
    newly_used_urls: list = []
    # 视频 b-roll 固定词库(可选):配了就让所有视频段从这个通用词库循环取词,不跟着
    # 逐段句子走。用于"句子题材稀缺(如黄金没视频),但通用市场/城市素材随便填"的场景——
    # 视频走市场/大盘/城市,题材镜头另由 image_query 强制插图。
    _vq_pool = [t.strip() for t in str(getattr(params, "video_query_pool", "") or "").split(",") if t.strip()]
    _vq_i = 0
    for shot in shots:
        if shot.source != "pending":
            continue
        if _vq_pool:
            query = _vq_pool[_vq_i % len(_vq_pool)]
            _vq_i += 1
        else:
            query = shot.visual_intent.split(",")[0].strip() or params.video_subject

        # 图片插槽:按段号每 image_insert_every 段安排一个静态图运镜片段(约每 N 段
        # 一张)。只有当该段本来要走在线视频(pending)时才真的换成图,落在本地素材
        # 段就跳过(保留本地)。图失败(没配 key/搜不到/渲染失败)时静默回退在线视频。
        result = None
        is_image_slot = (
            image_insert_every > 0
            and (shot.segment_index + 1) % image_insert_every == 0
        )
        if is_image_slot:
            # 图片插槽搜索词:若配了 image_query(固定题材,如黄金),用它,与逐段视频
            # 搜索词解耦——视频可以走通用市场/城市素材,图片仍强制是黄金。否则传完整
            # visual_intent(多个画面词),让 ImageProvider 逐词搜把靠后的本体词也用上。
            img_q = getattr(params, "image_query", "")
            image_intent = img_q or shot.visual_intent or query
            # 配了 image_query 时:按 image_query 做主题 + 严格判定,挡掉图库对"gold"
            # 误返回的非黄金图(书架烫金/城市等),保证这张强制图真是黄金。
            result = image_prov.fetch(
                image_intent, aspect, clip_duration,
                exclude_urls=used_stock_urls,
                cooldown_urls=cooldown_urls,
                topic=(img_q or params.video_subject),
                strict=(True if img_q else None),
            )
            if not result:
                logger.info(
                    f"  seg[{shot.segment_index}] image slot fell back to video"
                )

        if result is None:
            result = pexels_prov.fetch(
                query, aspect, clip_duration,
                exclude_urls=used_stock_urls,
                cooldown_urls=cooldown_urls,
                topic=params.video_subject,
            )

        # 视频补不上时(小池子被同题材用尽,如 9 句脚本最后一句)兜底用一张相关图片
        # Ken Burns,填上这句的空档——避免对齐错位/复用上一段造成"闪动、被连续调用"。
        if result is None and not is_image_slot:
            result = image_prov.fetch(
                shot.visual_intent or query, aspect, clip_duration,
                exclude_urls=used_stock_urls,
                cooldown_urls=cooldown_urls,
                topic=params.video_subject,
            )
            if result:
                logger.info(f"  seg[{shot.segment_index}] video exhausted → image fallback")

        if result:
            shot.material_path = result.path
            shot.source = result.source or "pexels"
            url = result.metadata.get("url") if result.metadata else None
            if url:
                used_stock_urls.add(url)
                newly_used_urls.append(url)
        else:
            logger.warning(f"  seg[{shot.segment_index}] stock fallback failed (no unused candidate)")

    # 5. Validate and order
    valid = [s for s in shots if s.material_path and os.path.isfile(s.material_path)]
    dropped = len(shots) - len(valid)
    if dropped:
        logger.warning(f"[orchestrator] dropped {dropped} shots with missing files")

    # 5a. 按句子对齐模式(默认):每句配一个画面、对齐到那句话的时间窗,所以一句一镜、
    # 不需要补抓额外素材来填时长,也不会有同一素材反复出现的问题。关闭 extra-fetch。
    # 关闭对齐(回到快切混填)时,才补抓独立素材降低重复(见 else 分支)。
    align_clips = getattr(params, "align_clips_to_script", True)
    extra_paths: list = []
    if not align_clips:
        # 混填模式:combine_videos 会把每个源切成多窗口填满音频——独立源不够时同一画面
        # (尤其长源片)会在视频不同段落反复出现。按 音频/单片时长 估算需要多少独立片段,
        # 不够就多抓(走相关性+硬去重+跨视频冷却),上限 _MAX_EXTRA_BROLL_CLIPS。
        onscreen_min = getattr(params, "min_clip_duration", 2.0) or 2.0
        onscreen_max = max(onscreen_min + 0.4, clip_duration)
        avg_onscreen = (onscreen_min + onscreen_max) / 2
        target_unique = math.ceil(audio_duration / max(avg_onscreen, 0.5))
        target_unique = min(target_unique, len(valid) + _MAX_EXTRA_BROLL_CLIPS)
        queries = [
            s.visual_intent.split(",")[0].strip()
            for s in valid if s.visual_intent and s.visual_intent.strip()
        ] or [params.video_subject]
        qi = attempts = 0
        max_attempts = target_unique * 3
        while len(valid) + len(extra_paths) < target_unique and attempts < max_attempts:
            attempts += 1
            q = queries[qi % len(queries)] or params.video_subject
            qi += 1
            r = pexels_prov.fetch(
                q, aspect, clip_duration,
                exclude_urls=used_stock_urls,
                cooldown_urls=cooldown_urls,
                topic=params.video_subject,
            )
            if not r:
                continue
            extra_paths.append(r.path)
            url = r.metadata.get("url") if r.metadata else None
            if url:
                used_stock_urls.add(url)
                newly_used_urls.append(url)
        if extra_paths:
            logger.info(
                f"[orchestrator] extra b-roll: +{len(extra_paths)} unique clips "
                f"(target {target_unique} for {audio_duration:.0f}s audio)"
            )

    # 5b. Hard no-repeat check — by this point every step (local matching,
    # min_local forcing, Pexels fetch) already excludes already-used paths.
    # If a duplicate still slips through, that means we genuinely ran out of
    # unique materials. Per product decision: never silently reuse a file —
    # fail loudly instead so the operator knows to add more local materials
    # or widen the Pexels search, rather than shipping a visually repetitive
    # video (a giveaway sign for "matrix account" style mass production).
    seen_paths: set = set()
    dup_segments = []
    for s in valid:
        if s.material_path in seen_paths:
            dup_segments.append(s.segment_index)
        seen_paths.add(s.material_path)
    if dup_segments:
        raise RuntimeError(
            f"orchestrator: could not find enough unique materials — "
            f"segment(s) {dup_segments} would reuse a file already used "
            f"elsewhere in this video. Hard no-repeat constraint refuses to "
            f"continue. Add more local materials covering this topic, or "
            f"the Pexels search terms are too narrow to return enough "
            f"distinct results."
        )

    # 5c. Record this video's stock URLs into cross-video usage history (only
    # reached if the hard no-repeat check above passed, i.e. the video is
    # actually going to be built). Failures here must not break generation.
    if video_seq and newly_used_urls:
        try:
            usage_history.record_stock_urls(newly_used_urls, video_seq, task_id)
        except Exception as e:
            logger.warning(f"usage_history: failed to record usage: {e}")

    # 6. Print summary log
    logger.info("=" * 60)
    logger.info("[orchestrator] SHOT LOG")
    for s in valid:
        logger.info(
            f"  seg[{s.segment_index}] [{s.source:14s}] score={s.score:.3f} | "
            f"{os.path.basename(s.material_path)}"
        )
    local_n = sum(1 for s in valid if "local" in s.source)
    logger.info(
        f"[orchestrator] total={len(valid)} local={local_n} "
        f"pexels={len(valid)-local_n} +extra_broll={len(extra_paths)}"
    )
    logger.info("=" * 60)

    # 每段主素材在前(保证语义对齐),补抓的独立 b-roll 追加在后,一起交给
    # combine_videos 填满音频时长,独立源更多 → 复用更少。
    video_paths = [s.material_path for s in valid] + extra_paths

    # 按句子对齐:为每段算出"应播出时长"——按该句字符数占比 × 音频总时长(语速近似均匀,
    # 字符占比是句子时间占比的好近似)。combine_videos 据此把每段画面铺到对应那句话的
    # 时间窗,实现"华盛顿那句出现华盛顿"。混填模式返回 None,走老的快切填充逻辑。
    clip_durations = None
    if align_clips and valid:
        lengths = [max(1, len(s.segment_text or "")) for s in valid]
        total_len = sum(lengths) or 1
        clip_durations = [audio_duration * (l / total_len) for l in lengths]
        logger.info(
            f"[orchestrator] aligned mode: {len(valid)} sentence-clips, "
            f"durations={[round(d, 1) for d in clip_durations]}"
        )

    return video_paths, valid, clip_durations
