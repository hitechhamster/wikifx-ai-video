"""
M3: mood-tagged BGM library + mood-driven selection.

Pipeline:
  tag_songs()             — Gemini audio understanding → mood/energy, sha256 cache
  analyze_script_mood()   — DeepSeek classifies overall script mood
  select_bgm(mood)        — pick a song matching mood (random among matches),
                             falls back to any song if no mood match
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from loguru import logger

from app.utils import utils

_DB_FILE = "songs.db"
_VALID_MOODS = ("professional", "tense", "uplifting", "neutral")

TAGGING_PROMPT = """Analyze this background music track for a forex/financial video production system.
Return ONLY a JSON object (no markdown fences, no extra text):
{
  "mood": "one of exactly: professional, tense, uplifting, neutral",
  "energy": <integer 1-5, 1=calm/ambient, 5=high energy/intense>,
  "description": "one short sentence describing the music style"
}"""

SCRIPT_MOOD_PROMPT = """You are classifying the overall emotional tone of a forex/financial video script
for background music selection. Read the script and respond with EXACTLY ONE WORD,
no punctuation, no explanation — one of:
professional
tense
uplifting
neutral

Guidance:
- "tense": risk warnings, market volatility, urgent/cautionary language
- "uplifting": profit opportunities, success stories, positive market sentiment
- "professional": calm technical/educational analysis, formal tone
- "neutral": none of the above clearly applies

Script:
{script}

Respond with one word only."""


@dataclass
class SongRecord:
    path: str
    sha256: str = ""
    duration: float = 0.0
    mood: str = ""
    energy: int = 0
    description: str = ""
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
        CREATE TABLE IF NOT EXISTS songs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT UNIQUE NOT NULL,
            sha256      TEXT DEFAULT '',
            duration    REAL DEFAULT 0,
            mood        TEXT DEFAULT '',
            energy      INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            created_at  TEXT,
            updated_at  TEXT
        )
        """)
        conn.commit()
    logger.debug(f"bgm library db ready: {db_path or _db_path()}")


def _row_to_record(row) -> SongRecord:
    return SongRecord(
        id=row[0], path=row[1], sha256=row[2], duration=row[3],
        mood=row[4], energy=row[5], description=row[6],
        created_at=row[7] or "", updated_at=row[8] or "",
    )


def list_all(db_path: str = None) -> List[SongRecord]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, path, sha256, duration, mood, energy, description, "
            "created_at, updated_at FROM songs ORDER BY id"
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def get_by_id(song_id: int, db_path: str = None) -> Optional[SongRecord]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, path, sha256, duration, mood, energy, description, "
            "created_at, updated_at FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()
    return _row_to_record(row) if row else None


def upsert_song(record: SongRecord, db_path: str = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute("""
        INSERT INTO songs (path, sha256, duration, mood, energy, description, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            sha256=excluded.sha256, duration=excluded.duration,
            mood=excluded.mood, energy=excluded.energy,
            description=excluded.description, updated_at=excluded.updated_at
        """, (
            record.path, record.sha256, record.duration,
            record.mood, record.energy, record.description, now, now,
        ))
        conn.commit()
        row = conn.execute("SELECT id FROM songs WHERE path=?", (record.path,)).fetchone()
        return row[0] if row else 0


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


def _get_duration(path: str) -> float:
    from moviepy import AudioFileClip
    try:
        clip = AudioFileClip(path)
        duration = clip.duration
        clip.close()
        return duration
    except Exception as e:
        logger.warning(f"could not read duration for {path}: {e}")
        return 0.0


def tag_one_song(path: str, client, model: str) -> Optional[dict]:
    """Upload one mp3 to Files API, get Gemini mood/energy tags."""
    from google.genai import types

    file_ref = None
    try:
        with open(path, "rb") as f:
            file_ref = client.files.upload(
                file=f,
                config=types.UploadFileConfig(
                    mime_type="audio/mpeg",
                    display_name=os.path.basename(path),
                ),
            )

        for _ in range(20):
            file_ref = client.files.get(name=file_ref.name)
            state = str(file_ref.state)
            if "ACTIVE" in state:
                break
            if "FAILED" in state:
                logger.error(f"bgm tagging: file processing failed: {path}")
                return None
            time.sleep(3)
        else:
            logger.error(f"bgm tagging: timeout waiting for ACTIVE: {path}")
            return None

        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_uri(file_uri=file_ref.uri, mime_type="audio/mpeg"),
                TAGGING_PROMPT,
            ],
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:])
            if raw.endswith("```"):
                raw = raw[:-3].strip()
            elif "```" in raw:
                raw = raw.rsplit("```", 1)[0].strip()

        data = json.loads(raw)
        if data.get("mood") not in _VALID_MOODS:
            logger.warning(f"bgm tagging: unexpected mood '{data.get('mood')}' for {path}, defaulting neutral")
            data["mood"] = "neutral"
        return data

    except json.JSONDecodeError as e:
        logger.warning(f"bgm tagging: JSON parse failed for {path}: {e} — raw: {raw[:200]}")
        return None
    except Exception as e:
        logger.error(f"bgm tagging: error for {path}: {e}")
        return None
    finally:
        if file_ref is not None:
            try:
                client.files.delete(name=file_ref.name)
            except Exception:
                pass


def tag_songs(song_dir: str = None, db_path: str = None) -> dict:
    """
    Tag all mp3 files in song_dir with Gemini audio understanding.
    Skips files whose sha256 already matches DB (incremental cache).
    """
    if song_dir is None:
        song_dir = utils.song_dir()

    cfg = _load_config()
    model = cfg.get("gemini_model_name", "gemini-3.5-flash")

    init_db(db_path)
    existing = {r.path: r for r in list_all(db_path)}

    mp3s = sorted(glob.glob(os.path.join(song_dir, "*.mp3")))
    if not mp3s:
        logger.warning(f"bgm tagging: no mp3 files found in {song_dir}")
        return {"tagged": 0, "skipped": 0, "failed": 0}

    client = _get_client()
    stats = {"tagged": 0, "skipped": 0, "failed": 0}

    for path in mp3s:
        sha = compute_sha256(path)
        rec = existing.get(path)

        if rec and rec.sha256 == sha and rec.sha256:
            logger.info(f"sha256 cache hit — skipping: {os.path.basename(path)}")
            stats["skipped"] += 1
            continue

        logger.info(f"tagging bgm: {os.path.basename(path)} ...")
        data = tag_one_song(path, client, model)
        if data is None:
            stats["failed"] += 1
            continue

        record = SongRecord(
            path=path,
            sha256=sha,
            duration=_get_duration(path),
            mood=data["mood"],
            energy=int(data.get("energy", 3)),
            description=data.get("description", ""),
        )
        mid = upsert_song(record, db_path)
        logger.success(
            f"  tagged [{mid}] {os.path.basename(path)}: "
            f"mood={record.mood} energy={record.energy} — {record.description}"
        )
        stats["tagged"] += 1

    logger.info(
        f"bgm tagging complete: tagged={stats['tagged']} "
        f"skipped={stats['skipped']} failed={stats['failed']}"
    )
    return stats


def analyze_script_mood(script: str) -> str:
    """DeepSeek classifies overall script mood. Falls back to 'neutral' on failure."""
    from app.services.llm import _generate_response

    prompt = SCRIPT_MOOD_PROMPT.format(script=script)
    try:
        raw = _generate_response(prompt).strip().lower()
        for mood in _VALID_MOODS:
            if mood in raw:
                return mood
        logger.warning(f"script mood classification unparseable: '{raw}', defaulting neutral")
        return "neutral"
    except Exception as e:
        logger.error(f"script mood analysis failed: {e}")
        return "neutral"


def select_bgm(
    mood: str,
    target_energy: Optional[int] = None,
    exclude_paths: set = None,
    db_path: str = None,
) -> Optional[str]:
    """
    Pick a song matching mood (random among matches, never first-by-default —
    avoids every video in a batch landing on the same track).

    target_energy: optional secondary filter — narrows to the closest-energy
                    candidates before randomizing (e.g. prefer higher energy
                    tense tracks for more urgent scripts).
    exclude_paths: songs to avoid reusing within the same batch run; falls
                   back to the full match set if exclusion would leave nothing.
    Falls back to any tagged song, then to None (caller falls back to legacy random).
    """
    songs = list_all(db_path)
    tagged = [s for s in songs if s.mood]
    if not tagged:
        logger.warning("select_bgm: no tagged songs in library")
        return None

    # 默认偏好:脚本情绪识别仍然生效(tense/uplifting/professional 走各自的曲库)；
    # 但分类落到 "neutral"(无明确情绪)时，按产品默认偏好改投紧张快节奏曲目，
    # 而不是去挑库里同样标 neutral 的平淡曲子。偏好用 target_energy 拉高到
    # tense 池里能量最高的那一档，而不是写死某一首。
    effective_mood = mood
    if mood == "neutral":
        tense_pool = [s for s in tagged if s.mood == "tense"]
        if tense_pool:
            effective_mood = "tense"
            if target_energy is None:
                target_energy = max(s.energy for s in tense_pool)
            logger.info("select_bgm: mood='neutral' (无明确情绪) → 默认偏好 tense + 高energy")

    matches = [s for s in tagged if s.mood == effective_mood]
    pool_label = f"mood match ({effective_mood})" if effective_mood != mood else "mood match"
    if not matches:
        logger.warning(f"select_bgm: no songs tagged mood='{effective_mood}', falling back to any tagged song")
        matches = tagged
        pool_label = "fallback (any mood)"

    if exclude_paths:
        non_repeat = [s for s in matches if s.path not in exclude_paths]
        if non_repeat:
            matches = non_repeat
            pool_label += ", deduped"

    if target_energy is not None and len(matches) > 1:
        closest = min(abs(s.energy - target_energy) for s in matches)
        narrowed = [s for s in matches if abs(s.energy - target_energy) == closest]
        if narrowed:
            matches = narrowed
            pool_label += f", energy~={target_energy}"

    chosen = random.choice(matches)
    logger.info(
        f"select_bgm: mood='{mood}' [{pool_label}] → {len(matches)} candidates → "
        f"chosen {os.path.basename(chosen.path)} (mood={chosen.mood}, energy={chosen.energy})"
    )
    return chosen.path


def select_tense_or_high_energy_bgm(
    min_energy: int = 4,
    max_energy: int = 5,
    exclude_paths: set = None,
    db_path: str = None,
) -> Optional[str]:
    """
    "快速紧张"风格专用选曲池:产品要的是"听感快速紧张"，不是严格匹配
    mood=tense 这个标签。tense 曲库本来就薄(实测大部分 Lyria 生成的曲目会被
    Gemini 独立判定成 professional 而不是 tense，即使 prompt 写的是紧张主题)，
    死磕标签会导致候选池太小、批量产出同质化。这里把池子放宽成
    mood=tense OR energy>=min_energy —— 高能量的 professional/uplifting 曲目
    同样有"快"的听感，可以一起用。

    max_energy:上限可配。用户反馈过 energy=5 那批太吵，可以把这个值调到比如
    3~4，把最吵的那批排除在自动选曲池之外，而不是整体一刀切只看 min_energy。
    """
    songs = list_all(db_path)
    tagged = [s for s in songs if s.mood]
    if not tagged:
        logger.warning("select_tense_or_high_energy_bgm: no tagged songs in library")
        return None

    pool = [
        s for s in tagged
        if (s.mood == "tense" or s.energy >= min_energy) and s.energy <= max_energy
    ]
    pool_label = f"tense∪energy>={min_energy}, energy<={max_energy}"
    if not pool:
        logger.warning(
            f"select_tense_or_high_energy_bgm: no songs match mood=tense or "
            f"energy>={min_energy}, falling back to any tagged song"
        )
        pool = tagged
        pool_label = "fallback (any mood)"

    if exclude_paths:
        non_repeat = [s for s in pool if s.path not in exclude_paths]
        if non_repeat:
            pool = non_repeat
            pool_label += ", deduped"

    chosen = random.choice(pool)
    logger.info(
        f"select_tense_or_high_energy_bgm: [{pool_label}] → {len(pool)} candidates → "
        f"chosen {os.path.basename(chosen.path)} (mood={chosen.mood}, energy={chosen.energy})"
    )
    return chosen.path
