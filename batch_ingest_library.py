"""
批量入库真实外汇素材库。

用法:
  python batch_ingest_library.py <素材目录>

流程(每个文件):
  1. sha256 去重 — 内容已在库中(即使文件名不同)直接跳过
  2. 质量过滤 + 复制进 storage/local_videos/(library.ingest_file: <480x480 拒绝,图片转视频)
  3. Gemini 打标(description/tags/topic_fit/mood/quality/has_watermark)+ embedding,失败自动重试

支持类型: mp4/mov/avi/flv/mkv/jpg/jpeg/png
增量:已入库的素材(按内容 sha256 判断,不是按文件名)自动跳过,可反复对同一目录重跑。
"""
import os
import sys
import shutil

sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger
from app.services import library as lib
from app.services import tagging
from app.utils.utils import storage_dir

ALLOWED_EXT = (".mp4", ".mov", ".avi", ".flv", ".mkv", ".jpg", ".jpeg", ".png")


def _existing_sha256s(db_path: str = None) -> set:
    return {r.sha256 for r in lib.list_all(db_path) if r.sha256}


def _unique_dest_path(local_dir: str, filename: str) -> str:
    dest_path = os.path.join(local_dir, filename)
    if not os.path.exists(dest_path):
        return dest_path
    stem, ext = os.path.splitext(filename)
    n = 1
    while True:
        candidate = os.path.join(local_dir, f"{stem}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def batch_ingest(source_dir: str, db_path: str = None, max_retries: int = 2) -> dict:
    if not os.path.isdir(source_dir):
        logger.error(f"source directory does not exist: {source_dir}")
        return {}

    lib.init_db(db_path)
    local_dir = storage_dir("local_videos", create=True)

    candidates = sorted(
        os.path.join(source_dir, f)
        for f in os.listdir(source_dir)
        if f.lower().endswith(ALLOWED_EXT)
    )
    if not candidates:
        logger.warning(f"no candidate files (mp4/mov/avi/flv/mkv/jpg/jpeg/png) found in {source_dir}")
        return {"found": 0, "copied": 0, "skipped_dup": 0, "rejected": 0}

    logger.info(f"found {len(candidates)} candidate files in {source_dir}")
    known_hashes = _existing_sha256s(db_path)

    copied_paths = []
    skipped_dup = 0
    rejected = 0

    for i, src in enumerate(candidates, 1):
        name = os.path.basename(src)
        logger.info(f"[{i}/{len(candidates)}] {name}")

        try:
            sha = tagging.compute_sha256(src)
        except Exception as e:
            logger.error(f"  cannot read/hash file, skipping: {e}")
            rejected += 1
            continue

        if sha in known_hashes:
            logger.info("  already in library (sha256 match) — skip")
            skipped_dup += 1
            continue

        dest_path = _unique_dest_path(local_dir, name)
        try:
            shutil.copy2(src, dest_path)
        except Exception as e:
            logger.error(f"  copy failed: {e}")
            rejected += 1
            continue

        record = lib.ingest_file(
            path=dest_path,
            tags=[],
            description="[pending tagging] batch import",
            topic_fit=[],
            mood="neutral",
            quality=5.0,
            db_path=db_path,
        )
        if record is None:
            logger.warning("  rejected by ingest_file (resolution <480x480 or unreadable) — removing copy")
            try:
                os.remove(dest_path)
            except OSError:
                pass
            rejected += 1
            continue

        known_hashes.add(sha)
        copied_paths.append(dest_path)
        logger.success(f"  ingested id={record.id} → {os.path.basename(dest_path)}")

    logger.info(
        f"\ningest stage done: {len(copied_paths)} new, "
        f"{skipped_dup} duplicate, {rejected} rejected"
    )

    tag_stats = {"tagged": 0, "skipped": 0, "failed": 0}
    if copied_paths:
        logger.info("\nrunning Gemini tagging on newly ingested files ...")
        tag_stats = tagging.run_tagging(local_dir=local_dir, db_path=db_path, max_retries=max_retries)
    else:
        logger.info("nothing new to tag")

    summary = {
        "found": len(candidates),
        "copied": len(copied_paths),
        "skipped_dup": skipped_dup,
        "rejected": rejected,
        **tag_stats,
    }
    logger.success(f"\nbatch ingest summary: {summary}")
    return summary


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python batch_ingest_library.py <source_dir>")
        sys.exit(1)
    batch_ingest(sys.argv[1])
