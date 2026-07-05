from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Optional


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json_list(value) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(list(value), ensure_ascii=False)


def _decode_list(value) -> list[str]:
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def _row(row: sqlite3.Row | None) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for key in ("keywords",):
        if key in out:
            out[key] = _decode_list(out[key])
    return out


def _tokens(text: str) -> set[str]:
    import re

    text = (text or "").lower()
    words = set(re.findall(r"[a-zA-Z0-9_]{2,}", text))
    cjk = set(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    chars = {text[i:i + 2] for i in range(max(0, len(text) - 1))
             if any("\u4e00" <= ch <= "\u9fff" for ch in text[i:i + 2])}
    return words | cjk | chars


class ConversationMemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_topics (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    keywords TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_message_at REAL,
                    message_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,
                    topic_id TEXT,
                    timestamp REAL NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL DEFAULT '',
                    user_summary TEXT NOT NULL DEFAULT '',
                    assistant_summary TEXT NOT NULL DEFAULT '',
                    keywords TEXT NOT NULL DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.0,
                    FOREIGN KEY(topic_id) REFERENCES conversation_topics(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory_signals (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    source_turn_id TEXT,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    needs_confirmation INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_topics_last ON conversation_topics(status, last_message_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_topic_time ON conversation_turns(topic_id, timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_time ON conversation_turns(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON user_memory_signals(status, type, updated_at DESC)")
            conn.commit()

    def list_recent_topics(self, limit: int = 8) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversation_topics
                WHERE status = 'active'
                ORDER BY COALESCE(last_message_at, updated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows if r is not None]

    def get_topic(self, topic_id: str) -> Optional[dict]:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM conversation_topics WHERE id = ?", (topic_id,)).fetchone()
        return _row(row)

    def create_topic(self, title: str, summary: str = "", keywords: Optional[list[str]] = None) -> dict:
        now = time.time()
        topic_id = _id("topic")
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO conversation_topics
                    (id, title, summary, keywords, status, created_at, updated_at, last_message_at, message_count)
                VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, 0)
                """,
                (topic_id, title or "Untitled topic", summary or "", _json_list(keywords), now, now),
            )
            conn.commit()
        return self.get_topic(topic_id) or {"id": topic_id, "title": title, "summary": summary, "keywords": keywords or []}

    def update_topic(self, topic_id: str, **fields) -> Optional[dict]:
        allowed = {"title", "summary", "keywords", "status"}
        sets: list[str] = []
        values: list = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(_json_list(value) if key == "keywords" else value)
        if not sets:
            return self.get_topic(topic_id)
        sets.append("updated_at = ?")
        values.append(time.time())
        values.append(topic_id)
        with closing(self.connect()) as conn:
            conn.execute(f"UPDATE conversation_topics SET {', '.join(sets)} WHERE id = ?", values)
            conn.commit()
        return self.get_topic(topic_id)

    def append_turn(
        self,
        topic_id: str,
        user_text: str,
        assistant_text: str = "",
        *,
        user_summary: str = "",
        assistant_summary: str = "",
        keywords: Optional[list[str]] = None,
        importance: float = 0.0,
        timestamp: Optional[float] = None,
    ) -> dict:
        now = timestamp or time.time()
        turn_id = _id("turn")
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns
                    (id, topic_id, timestamp, user_text, assistant_text, user_summary,
                     assistant_summary, keywords, importance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id, topic_id, now, user_text, assistant_text or "", user_summary or "",
                    assistant_summary or "", _json_list(keywords), float(importance or 0.0),
                ),
            )
            conn.execute(
                """
                UPDATE conversation_topics
                SET message_count = message_count + 1,
                    last_message_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, topic_id),
            )
            conn.commit()
        return self.get_turn(turn_id) or {"id": turn_id, "topic_id": topic_id}

    def get_turn(self, turn_id: str) -> Optional[dict]:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM conversation_turns WHERE id = ?", (turn_id,)).fetchone()
        return _row(row)

    def recent_turns(self, topic_id: str, limit: int = 6) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversation_turns
                WHERE topic_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (topic_id, limit),
            ).fetchall()
        return list(reversed([_row(r) for r in rows if r is not None]))

    def search_relevant_turns(
        self,
        query: str,
        limit: int = 4,
        *,
        exclude_topic_id: Optional[str] = None,
    ) -> list[dict]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT ct.*, t.title AS topic_title
                FROM conversation_turns ct
                LEFT JOIN conversation_topics t ON t.id = ct.topic_id
                ORDER BY ct.timestamp DESC
                LIMIT 200
                """
            ).fetchall()
        now = time.time()
        scored: list[tuple[float, dict]] = []
        for row in rows:
            item = _row(row)
            if item is None or (exclude_topic_id and item.get("topic_id") == exclude_topic_id):
                continue
            text = " ".join(str(item.get(k) or "") for k in (
                "user_text", "assistant_text", "user_summary", "assistant_summary", "topic_title"
            ))
            overlap = len(query_tokens & _tokens(text))
            if overlap <= 0:
                continue
            age_days = max(0.0, (now - float(item.get("timestamp") or now)) / 86400.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = overlap * 3.0 + recency + float(item.get("importance") or 0.0)
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def list_user_memory_signals(self, status: Optional[str] = None, limit: int = 20) -> list[dict]:
        params: list = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status)
        params.append(limit)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM user_memory_signals
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row(r) for r in rows if r is not None]

    def add_user_memory_signal(
        self,
        kind: str,
        content: str,
        *,
        confidence: float = 0.0,
        source_turn_id: Optional[str] = None,
        needs_confirmation: bool = False,
        status: str = "candidate",
    ) -> dict:
        now = time.time()
        signal_id = _id("signal")
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO user_memory_signals
                    (id, type, content, confidence, source_turn_id, status,
                     needs_confirmation, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id, kind, content, float(confidence or 0.0), source_turn_id, status,
                    1 if needs_confirmation else 0, now, now,
                ),
            )
            conn.commit()
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM user_memory_signals WHERE id = ?", (signal_id,)).fetchone()
        return _row(row) or {"id": signal_id, "type": kind, "content": content}
