"""
跨视频素材使用历史 —— 实现"冷却 N 条内不重复"的去重策略。

为什么需要:orchestrator 的 used_pexels_urls/used_local_paths 只在单条视频内
生效,系列化产出时不同视频会反复抓到同一批在线图库素材。这里把"用过的在线素材
标识(下载直链 URL)"持久化到 SQLite,新视频开始前预加载最近 N 条视频用过的 URL,
作为软排除注入选材流程,让系统优先挑没在最近用过的素材。

冷却而非永久拉黑:外汇题材在免费图库里的真实拍摄池子本来就小,永久拉黑会很快
耗尽,逼出"素材不足"硬失败。冷却窗口让素材在 N 条视频之后重新进入候选,既避免
相邻视频撞车,又不会把池子烧干。永久拉黑的需求请改用更大的 cooldown 值近似。

只记录在线图库 URL(kind='stock'),不记录本地库路径:本地库是 WikiFX 自有的少量
品牌 b-roll,跨视频复用是合理甚至期望的,对它做冷却只会误触发素材不足。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Set

from loguru import logger

from app.utils import utils

_DB_FILE = "usage_history.sqlite3"


def _db_path() -> str:
    return os.path.join(utils.storage_dir(), _DB_FILE)


def _connect(db_path: str = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or _db_path())


def init_db(db_path: str = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS material_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier  TEXT NOT NULL,
            kind        TEXT DEFAULT 'stock',
            video_seq   INTEGER NOT NULL,
            task_id     TEXT DEFAULT '',
            used_at     TEXT DEFAULT (datetime('now'))
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_seq ON material_usage(video_seq)"
        )
        conn.commit()


def reserve_video_seq(db_path: str = None) -> int:
    """
    为当前这条视频取一个递增的序号,本条视频选中的所有素材都用同一个 seq 记录。

    并发说明:max+1 在多任务并发时理论上可能撞号,后果只是两条视频被冷却逻辑
    当成同一条(冷却窗口少算一格),对去重是无害的近似,不值得为此加全局锁。
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(video_seq), 0) FROM material_usage"
        ).fetchone()
        return int(row[0]) + 1


def get_cooldown_urls(cooldown_videos: int, db_path: str = None) -> Set[str]:
    """
    返回最近 cooldown_videos 条视频里用过的在线图库 URL 集合(用于软排除)。

    cooldown_videos<=0 表示关闭冷却,返回空集。
    """
    if cooldown_videos <= 0:
        return set()

    init_db(db_path)
    with _connect(db_path) as conn:
        recent_seqs = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT video_seq FROM material_usage "
                "ORDER BY video_seq DESC LIMIT ?",
                (cooldown_videos,),
            ).fetchall()
        ]
        if not recent_seqs:
            return set()
        placeholders = ",".join("?" * len(recent_seqs))
        rows = conn.execute(
            f"SELECT identifier FROM material_usage "
            f"WHERE kind='stock' AND video_seq IN ({placeholders})",
            recent_seqs,
        ).fetchall()
    return {r[0] for r in rows}


def record_stock_urls(
    urls, video_seq: int, task_id: str = "", db_path: str = None
) -> None:
    """把本条视频实际用到的在线图库 URL 记入历史。"""
    urls = [u for u in (urls or []) if u]
    if not urls:
        return
    init_db(db_path)
    try:
        with _connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO material_usage (identifier, kind, video_seq, task_id) "
                "VALUES (?, 'stock', ?, ?)",
                [(u, video_seq, task_id) for u in urls],
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"usage_history: failed to record stock urls: {e}")
