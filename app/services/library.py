"""
SQLite-backed local material library.

M1: keyword overlap scoring.
M2: sha256 tagging cache + embedding column + cosine similarity search.
"""
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from loguru import logger

from app.utils import utils

_DB_FILE = "materials.db"

# Module-level Gemini client cache (lazy init for embedding at search time)
_gemini_client = None


@dataclass
class MaterialRecord:
    path: str
    sha256: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    aspect: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    topic_fit: List[str] = field(default_factory=list)
    mood: str = "neutral"
    quality: float = 5.0
    has_watermark: bool = False
    embedding: List[float] = field(default_factory=list)  # M2: gemini-embedding-2
    id: int = 0
    created_at: str = ""
    updated_at: str = ""


def _db_path() -> str:
    return os.path.join(utils.storage_dir(), _DB_FILE)


def _connect(db_path: str = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or _db_path())


def init_db(db_path: str = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS materials (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT UNIQUE NOT NULL,
            sha256      TEXT DEFAULT '',
            duration    REAL DEFAULT 0,
            width       INTEGER DEFAULT 0,
            height      INTEGER DEFAULT 0,
            aspect      TEXT DEFAULT '',
            description TEXT DEFAULT '',
            tags        TEXT DEFAULT '[]',
            topic_fit   TEXT DEFAULT '[]',
            mood        TEXT DEFAULT 'neutral',
            quality     REAL DEFAULT 5.0,
            has_watermark INTEGER DEFAULT 0,
            embedding   TEXT DEFAULT '',
            created_at  TEXT,
            updated_at  TEXT
        )
        """)
        # M2 migration: add embedding column to existing tables
        try:
            conn.execute("ALTER TABLE materials ADD COLUMN embedding TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    logger.debug(f"library db ready: {db_path or _db_path()}")


def upsert_material(record: MaterialRecord, db_path: str = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute("""
        INSERT INTO materials
            (path, sha256, duration, width, height, aspect, description,
             tags, topic_fit, mood, quality, has_watermark, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            sha256=excluded.sha256,
            duration=excluded.duration,
            width=excluded.width,
            height=excluded.height,
            aspect=excluded.aspect,
            description=excluded.description,
            tags=excluded.tags,
            topic_fit=excluded.topic_fit,
            mood=excluded.mood,
            quality=excluded.quality,
            has_watermark=excluded.has_watermark,
            updated_at=excluded.updated_at
        """, (
            record.path, record.sha256, record.duration,
            record.width, record.height, record.aspect,
            record.description,
            json.dumps(record.tags, ensure_ascii=False),
            json.dumps(record.topic_fit, ensure_ascii=False),
            record.mood, record.quality, int(record.has_watermark),
            now, now,
        ))
        conn.commit()
        row = conn.execute(
            "SELECT id FROM materials WHERE path = ?", (record.path,)
        ).fetchone()
        return row[0] if row else 0


def update_material_tags(
    path: str,
    sha256: str,
    description: str,
    tags: List[str],
    topic_fit: List[str],
    mood: str,
    quality: float,
    has_watermark: bool,
    embedding: Optional[List[float]] = None,
    db_path: str = None,
) -> None:
    """
    M2: update Gemini-generated tags + embedding for an existing record.
    Also upserts if the record doesn't exist yet (e.g. newly downloaded files).
    """
    now = datetime.now(timezone.utc).isoformat()
    emb_json = json.dumps(embedding) if embedding else ""
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE materials SET
                sha256=?, description=?, tags=?, topic_fit=?, mood=?,
                quality=?, has_watermark=?, embedding=?, updated_at=?
            WHERE path=?
            """,
            (
                sha256,
                description,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(topic_fit, ensure_ascii=False),
                mood,
                quality,
                int(has_watermark),
                emb_json,
                now,
                path,
            ),
        )
        conn.commit()


def _row_to_record(row) -> MaterialRecord:
    emb_raw = row[13] or ""
    return MaterialRecord(
        id=row[0], path=row[1], sha256=row[2],
        duration=row[3], width=row[4], height=row[5], aspect=row[6],
        description=row[7],
        tags=json.loads(row[8] or "[]"),
        topic_fit=json.loads(row[9] or "[]"),
        mood=row[10], quality=row[11],
        has_watermark=bool(row[12]),
        embedding=json.loads(emb_raw) if emb_raw else [],
        created_at=row[14] or "", updated_at=row[15] or "",
    )


def list_all(db_path: str = None) -> List[MaterialRecord]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, path, sha256, duration, width, height, aspect, "
            "description, tags, topic_fit, mood, quality, has_watermark, "
            "embedding, created_at, updated_at FROM materials ORDER BY quality DESC"
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def get_by_id(material_id: int, db_path: str = None) -> Optional[MaterialRecord]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, path, sha256, duration, width, height, aspect, "
            "description, tags, topic_fit, mood, quality, has_watermark, "
            "embedding, created_at, updated_at FROM materials WHERE id = ?",
            (material_id,),
        ).fetchone()
    return _row_to_record(row) if row else None


def get_by_path(path: str, db_path: str = None) -> Optional[MaterialRecord]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, path, sha256, duration, width, height, aspect, "
            "description, tags, topic_fit, mood, quality, has_watermark, "
            "embedding, created_at, updated_at FROM materials WHERE path = ?",
            (path,),
        ).fetchone()
    return _row_to_record(row) if row else None


# ---------------------------------------------------------------------------
# M2: cosine similarity helpers
# ---------------------------------------------------------------------------

def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return round(dot / (norm_a * norm_b), 4)


def _get_embed_client():
    """Lazy-init module-level Gemini client for search-time embedding."""
    global _gemini_client
    if _gemini_client is None:
        import toml
        import google.genai as genai
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "..", "config.toml")
        cfg = toml.load(cfg_path)["app"]
        _gemini_client = genai.Client(api_key=cfg["gemini_api_key"])
    return _gemini_client


def _embed_intent(intent: str) -> Optional[List[float]]:
    """Embed the query intent with RETRIEVAL_QUERY task type for cosine search."""
    import toml
    from google.genai import types as gtypes
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "..", "config.toml")
    embedding_model = toml.load(cfg_path)["app"].get("gemini_embedding_model", "gemini-embedding-001")
    try:
        client = _get_embed_client()
        result = client.models.embed_content(
            model=embedding_model,
            contents=intent,
            config=gtypes.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
        )
        return list(result.embeddings[0].values)
    except Exception as e:
        logger.warning(f"intent embedding failed: {e}")
        return None


# ---------------------------------------------------------------------------
# M1 keyword scoring (kept as fallback when embeddings not yet available)
# ---------------------------------------------------------------------------

def _score_record(record: MaterialRecord, query_terms: List[str]) -> float:
    if not query_terms:
        return 0.0
    searchable = " ".join(
        record.tags + record.topic_fit + [record.description]
    ).lower()
    matches = sum(1 for t in query_terms if t in searchable)
    base = matches / len(query_terms)
    if any(k in record.topic_fit for k in ("forex", "trading", "finance")):
        base = min(1.0, base + 0.1)
    return round(base * record.quality / 10.0, 4)


def search_by_intent(
    intent: str,
    top_k: int = 5,
    min_duration: float = 3.0,
    exclude_paths: set = None,
    db_path: str = None,
) -> List[Tuple[MaterialRecord, float]]:
    """
    Return (record, score) pairs sorted by score descending.

    M2 path: if any record has an embedding, embed the intent and use cosine similarity.
    M1 fallback: keyword overlap scoring (used if no embeddings in DB yet).

    exclude_paths: HARD exclusion — a material already used elsewhere in the
    same video is never reused, even if it means returning fewer (or zero)
    candidates. Used to be a soft "prefer to skip, fall back to reuse if
    nothing else" — changed to a hard no-repeat constraint; callers that
    come up empty must fall through to Pexels, not silently reuse a file.
    """
    records = list_all(db_path)
    valid = [r for r in records if r.duration >= min_duration and os.path.isfile(r.path)]

    if exclude_paths:
        valid = [r for r in valid if r.path not in exclude_paths]

    # M2: cosine path if at least one record has an embedding
    embedded = [r for r in valid if r.embedding]
    if embedded:
        intent_vec = _embed_intent(intent)
        if intent_vec:
            scored = [(r, _cosine(intent_vec, r.embedding)) for r in embedded]
            # For any unembedded records, use keyword fallback score
            query_terms = [t for t in re.split(r"[\s,，/\-]+", intent.lower()) if len(t) > 1]
            for r in valid:
                if not r.embedding:
                    scored.append((r, _score_record(r, query_terms)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    # M1 keyword fallback
    query_terms = [t for t in re.split(r"[\s,，/\-]+", intent.lower()) if len(t) > 1]
    scored = [(r, _score_record(r, query_terms)) for r in valid]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def find_best_match_for_intent(
    intent: str,
    candidates: List[MaterialRecord],
) -> Tuple[Optional[MaterialRecord], float]:
    """
    Find the best matching record from candidates for the given intent.
    Uses cosine similarity if embeddings available, else quality rank.
    Returns (record, score).
    """
    if not candidates:
        return None, 0.0

    embedded = [r for r in candidates if r.embedding]
    if embedded:
        intent_vec = _embed_intent(intent)
        if intent_vec:
            scored = [(r, _cosine(intent_vec, r.embedding)) for r in embedded]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[0]

    # Fallback: best quality
    best = max(candidates, key=lambda r: r.quality)
    return best, best.quality / 10.0


def get_top_quality(
    n: int = 5,
    min_duration: float = 3.0,
    db_path: str = None,
) -> List[MaterialRecord]:
    records = list_all(db_path)
    valid = [r for r in records if r.duration >= min_duration and os.path.isfile(r.path)]
    return sorted(valid, key=lambda r: r.quality, reverse=True)[:n]


def ingest_file(
    path: str,
    tags: List[str] = None,
    description: str = "",
    topic_fit: List[str] = None,
    mood: str = "neutral",
    quality: float = 5.0,
    clip_duration: int = 4,
    db_path: str = None,
) -> Optional[MaterialRecord]:
    """
    Validate, convert (image→video with Ken Burns), and register in library.

    Replicates preprocess_video's quality filter and image handling without
    the storage/local_videos path restriction. Paths are admin-controlled at
    ingest time, not from API caller input.
    """
    from app.models import const
    from app.utils.utils import parse_extension
    from app.services.video import (
        _open_image_clip_with_fallback,
        _open_video_clip_quietly,
        close_clip,
    )

    if not os.path.isfile(path):
        logger.warning(f"ingest_file: not found: {path}")
        return None

    ext = parse_extension(path)
    resolved_path = path

    try:
        if ext in const.FILE_TYPE_IMAGES:
            clip, resolved_path = _open_image_clip_with_fallback(path)
        else:
            clip = _open_video_clip_quietly(path)
    except Exception as exc:
        logger.warning(f"ingest_file: cannot open {path}: {exc}")
        return None

    width, height = clip.size

    if width < 480 or height < 480:
        close_clip(clip)
        logger.warning(
            f"ingest_file: low resolution {width}x{height}, skipping: {path}"
        )
        return None

    # Image → mp4 with Ken Burns zoom (same logic as preprocess_video)
    if ext in const.FILE_TYPE_IMAGES:
        from moviepy import ImageClip, CompositeVideoClip
        close_clip(clip)
        img_clip = (
            ImageClip(resolved_path)
            .with_duration(clip_duration)
            .with_position("center")
        )
        zoom_clip = img_clip.resized(
            lambda t: 1 + (clip_duration * 0.03) * (t / clip_duration)
        )
        final_clip = CompositeVideoClip([zoom_clip])
        video_file = f"{resolved_path}.mp4"
        final_clip.write_videofile(video_file, fps=30, logger=None)
        close_clip(img_clip)
        close_clip(final_clip)
        resolved_path = video_file
        clip = _open_video_clip_quietly(resolved_path)
        width, height = clip.size

    duration = clip.duration
    close_clip(clip)

    aspect = "9:16" if height > width else ("1:1" if height == width else "16:9")

    init_db(db_path)
    record = MaterialRecord(
        path=resolved_path,
        duration=duration,
        width=width,
        height=height,
        aspect=aspect,
        description=description,
        tags=tags or [],
        topic_fit=topic_fit or [],
        mood=mood,
        quality=quality,
    )
    mid = upsert_material(record, db_path)
    record.id = mid
    logger.success(
        f"ingested [{mid}] {os.path.basename(resolved_path)} "
        f"({width}x{height}, {duration:.1f}s)"
    )
    return record
