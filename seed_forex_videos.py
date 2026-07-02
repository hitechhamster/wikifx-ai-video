"""
Download 5 real forex videos from Pexels → storage/local_videos/ → ingest into library.

Keeps existing unrelated videos as contrast set for M2 semantic search validation.
"""
import os
import sys
import hashlib
import requests
sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger
from app.services.material import search_videos_pexels
from app.models.schema import VideoAspect
from app.services import library as lib
from app.utils.utils import storage_dir

FOREX_SEARCHES = [
    ("forex currency exchange trading", 1),
    ("candlestick chart stock market", 1),
    ("dollar bills currency money", 1),
    ("financial trading terminal screen", 1),
    ("stock market graph analysis", 1),
]


def _download(url: str, dest: str) -> bool:
    try:
        r = requests.get(url, stream=True, timeout=90)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"download failed {url}: {e}")
        return False


def main():
    local_dir = os.path.join(storage_dir(), "local_videos")
    os.makedirs(local_dir, exist_ok=True)
    lib.init_db()

    downloaded = 0
    seen_urls: set = set()

    for term, count in FOREX_SEARCHES:
        logger.info(f"Pexels search: '{term}'")
        try:
            videos = search_videos_pexels(
                search_term=term,
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            )
        except Exception as e:
            logger.error(f"  search failed: {e}")
            continue

        if not videos:
            logger.warning(f"  no results for '{term}'")
            continue

        for v in videos[:count]:
            url = getattr(v, "url", None)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # filename: md5 of url
            fname = "forex-" + hashlib.md5(url.encode()).hexdigest()[:16] + ".mp4"
            dest = os.path.join(local_dir, fname)

            if os.path.exists(dest):
                logger.info(f"  already exists: {fname}")
            else:
                logger.info(f"  downloading → {fname}")
                if not _download(url, dest):
                    continue

            rec = lib.ingest_file(
                path=dest,
                tags=[],
                description=f"[pending tagging] pexels: {term}",
                topic_fit=[],
                mood="neutral",
                quality=5.0,
            )
            if rec:
                downloaded += 1
                logger.success(f"  ingested id={rec.id}: {fname}")

    # Summary of library
    all_recs = lib.list_all()
    logger.info(f"\nLibrary now has {len(all_recs)} records ({downloaded} newly downloaded):")
    for r in all_recs:
        logger.info(f"  [{r.id}] {os.path.basename(r.path)} — {r.description[:60]}")


if __name__ == "__main__":
    main()
