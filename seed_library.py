"""
Seed the material library with a few tagged local videos for M1 testing.
Run once: python seed_library.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from app.services.library import init_db, ingest_file, list_all
from app.utils.utils import storage_dir

ENTRIES = [
    {
        "filename": "vid-013608f8f92bc390bf5e5d85118d7f05.mp4",
        "description": "forex trading desk with multiple monitors showing charts",
        "tags": ["trading desk", "monitors", "charts", "office", "professional"],
        "topic_fit": ["forex", "trading", "professional"],
        "mood": "professional",
        "quality": 7.0,
    },
    {
        "filename": "vid-0353524b1a99f7b06f6016802e6e07c1.mp4",
        "description": "stock market chart and candlestick graph on screen",
        "tags": ["chart", "candlestick", "stock market", "graph", "trading", "k-line"],
        "topic_fit": ["forex", "trading", "chart", "finance"],
        "mood": "tense",
        "quality": 7.5,
    },
    {
        "filename": "vid-042ed54ad309570719decf57df85393a.mp4",
        "description": "currency exchange counter with dollar bills",
        "tags": ["currency", "exchange", "dollar", "money", "finance", "cash"],
        "topic_fit": ["forex", "currency", "finance"],
        "mood": "neutral",
        "quality": 6.5,
    },
    {
        "filename": "vid-0e260db15fdc39d449dc0ee2d27b150c.mp4",
        "description": "financial data analysis on computer screen",
        "tags": ["financial data", "analysis", "computer", "numbers", "screen", "forex"],
        "topic_fit": ["forex", "analysis", "finance", "trading"],
        "mood": "professional",
        "quality": 7.0,
    },
]

def main():
    local_dir = os.path.join(storage_dir(), "local_videos")
    init_db()

    print(f"Seeding from: {local_dir}")
    seeded = 0
    for entry in ENTRIES:
        path = os.path.join(local_dir, entry["filename"])
        if not os.path.isfile(path):
            print(f"  SKIP (not found): {entry['filename']}")
            continue

        record = ingest_file(
            path=path,
            tags=entry["tags"],
            description=entry["description"],
            topic_fit=entry["topic_fit"],
            mood=entry["mood"],
            quality=entry["quality"],
        )
        if record:
            print(f"  OK [{record.id}] {entry['filename']} ({record.width}x{record.height}, {record.duration:.1f}s)")
            seeded += 1
        else:
            print(f"  FAIL: {entry['filename']}")

    print(f"\nSeeded {seeded}/{len(ENTRIES)} materials")
    print("\nLibrary contents:")
    for r in list_all():
        print(f"  [{r.id}] q={r.quality} mood={r.mood} | {r.description[:50]}")


if __name__ == "__main__":
    main()
