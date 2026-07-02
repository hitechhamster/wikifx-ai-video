"""
M2 验证:
1. 打标样例:打印 materials 表中几条真实 Gemini 标注
2. 区分度:对外汇文案做 semantic search,外汇素材分明显高于无关对照
3. 缓存增量:第二次打标所有文件都是 sha256 cache hit,无重新调用
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger
from app.services import library as lib
from app.services import tagging


def section(title: str):
    logger.info("=" * 60)
    logger.info(f"[M2] {title}")
    logger.info("=" * 60)


# ─────────────────────────────────────────────────────────────
# STEP 1: 运行打标(第一次,或增量)
# ─────────────────────────────────────────────────────────────
def run_tagging_pass(label: str) -> dict:
    section(f"打标 ({label})")
    stats = tagging.run_tagging()
    logger.info(
        f"  tagged={stats['tagged']} skipped={stats['skipped']} failed={stats['failed']}"
    )
    return stats


# ─────────────────────────────────────────────────────────────
# STEP 2: 打印 materials 表样例
# ─────────────────────────────────────────────────────────────
def print_library_samples():
    section("materials 表标注样例")
    records = lib.list_all()
    for r in records:
        has_emb = len(r.embedding) > 0
        logger.info(
            f"[{r.id}] {os.path.basename(r.path)}\n"
            f"    description : {r.description}\n"
            f"    tags        : {r.tags}\n"
            f"    topic_fit   : {r.topic_fit}\n"
            f"    mood={r.mood} quality={r.quality} watermark={r.has_watermark} embedding={'YES' if has_emb else 'NO'}"
        )


# ─────────────────────────────────────────────────────────────
# STEP 3: 区分度验证
# ─────────────────────────────────────────────────────────────
def check_discrimination():
    section("语义区分度验证")
    intents = [
        "forex currency trading candlestick chart",
        "US dollar exchange rate analysis",
        "stock market financial data screen",
    ]
    records = lib.list_all()
    embedded = [r for r in records if r.embedding]
    if not embedded:
        logger.error("  no embeddings in DB — tagging must run first")
        return False

    all_pass = True
    for intent in intents:
        results = lib.search_by_intent(intent, top_k=len(embedded))
        logger.info(f"\n  intent: '{intent}'")
        forex_scores = []
        other_scores = []
        for r, score in results:
            is_forex = any(k in r.topic_fit for k in ("forex", "trading", "chart", "currency", "finance"))
            tag = "FOREX" if is_forex else "OTHER"
            logger.info(
                f"    [{tag:5s}] score={score:.4f} | {os.path.basename(r.path)} | {r.description[:50]}"
            )
            if is_forex:
                forex_scores.append(score)
            else:
                other_scores.append(score)

        if forex_scores and other_scores:
            avg_forex = sum(forex_scores) / len(forex_scores)
            avg_other = sum(other_scores) / len(other_scores)
            gap = avg_forex - avg_other
            ok = gap > 0.05
            status = "✓ PASS" if ok else "✗ FAIL"
            logger.info(
                f"  avg_forex={avg_forex:.4f} avg_other={avg_other:.4f} "
                f"gap={gap:+.4f} → {status}"
            )
            if not ok:
                all_pass = False
        else:
            logger.warning("  not enough categories to measure discrimination")

    return all_pass


# ─────────────────────────────────────────────────────────────
# STEP 4: 产出一条视频(orchestrator 用 M2 embedding)
# ─────────────────────────────────────────────────────────────
def run_video():
    section("产出视频(M2 semantic search)")
    from app.services import task
    from app.services import state as sm
    from app.models.schema import VideoParams

    SCRIPT = (
        "美元兑人民币汇率近期持续走强，市场情绪明显好转。"
        "从技术面看，关键支撑位守稳，多头格局延续。"
        "交易员建议密切关注美联储政策动向，适时调整仓位。"
    )
    task_id = "m2_semantic_test"
    params = VideoParams(
        video_subject="外汇美元走势分析",
        video_script=SCRIPT,
        voice_name="zh-CN-XiaoxiaoNeural-Female",
        voice_rate=1.0,
        video_source="pexels",
        video_clip_duration=5,
        subtitle_enabled=True,
        bgm_type="random",
        bgm_volume=0.2,
        paragraph_number=1,
        use_orchestrator=True,
        local_threshold=0.55,  # M2 calibrated: embedding-001 forex_min=0.54 > other_max=0.54
        min_local_segments=2,
    )
    result = task.start(task_id, params)
    if result and result.get("videos"):
        out = result["videos"][0]
        size = os.path.getsize(out) if os.path.isfile(out) else 0
        logger.success(f"  output: {out}  ({size:,} bytes)")
        return True
    else:
        s = sm.state.get_task(task_id)
        logger.error(f"  FAIL: {s}")
        return False


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def main():
    # Pass 1: tag all (first run = real API calls)
    stats1 = run_tagging_pass("pass 1 — should tag all files")
    print_library_samples()

    # Discrimination check
    disc_ok = check_discrimination()

    # Pass 2: re-run tagging to verify sha256 cache
    section("缓存增量验证 (pass 2)")
    stats2 = run_tagging_pass("pass 2 — all should be cache hits")
    cache_ok = stats2["tagged"] == 0 and stats2["skipped"] > 0
    if cache_ok:
        logger.success(f"  ✓ cache hit: {stats2['skipped']} files skipped, 0 re-tagged")
    else:
        logger.error(f"  ✗ cache miss: tagged={stats2['tagged']} skipped={stats2['skipped']}")

    # Video output
    video_ok = run_video()

    # Summary
    section("M2 验收汇总")
    logger.info(f"  打标数量    : {stats1['tagged']} tagged, {stats1['skipped']} skipped")
    logger.info(f"  语义区分度  : {'✓ PASS' if disc_ok else '✗ FAIL'}")
    logger.info(f"  sha256 缓存 : {'✓ PASS' if cache_ok else '✗ FAIL'}")
    logger.info(f"  视频产出    : {'✓ PASS' if video_ok else '✗ FAIL'}")

    all_pass = disc_ok and cache_ok and video_ok
    if all_pass:
        logger.success("M2 全部通过")
        sys.exit(0)
    else:
        logger.error("M2 有失败项")
        sys.exit(1)


if __name__ == "__main__":
    main()
