# -*- coding: utf-8 -*-
"""本地明文聊天历史。

只保存 OCR 得到的文字、角色、发言人、捕获时间和结构化判断，不保存截图或密钥。
数据库放在用户数据目录，不放在发布目录，分享 exe 时不会把个人聊天带出去。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_LIMIT = 40
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_MB = 50


def default_path() -> str:
    """返回本机用户数据目录；不依赖 exe 所在目录。"""
    root = os.environ.get("LOCALAPPDATA", "").strip()
    if not root:
        root = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(root, "JevChat", "history.sqlite3")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


class HistoryStore:
    """短事务 SQLite 存储。

    当前程序只由主进程调用，但内部仍用锁和 busy_timeout，避免设置重置、采集回调
    同时触发时把一个临时数据库错误冒泡到 UI。
    """

    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path or default_path())
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure(self, conn: sqlite3.Connection) -> None:
        if self._initialized:
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_key TEXT NOT NULL,
                role TEXT NOT NULL,
                speaker_name TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
                ON messages(conversation_key, captured_at, id);
            CREATE TABLE IF NOT EXISTS judgments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_key TEXT NOT NULL,
                message_tail_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                answers_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_judgments_conversation_time
                ON judgments(conversation_key, created_at, id);
            """
        )
        self._initialized = True

    @staticmethod
    def _dedupe_key(conversation_key: str, role: str, name: str, text: str,
                    captured_at: datetime) -> str:
        # OCR 连续几帧可能重复识别同一句话；按分钟去重，跨分钟的相同消息仍会保留。
        bucket = captured_at.strftime("%Y-%m-%dT%H:%M")
        value = "\x1f".join((conversation_key, role, name, text, bucket))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def load_messages(self, conversation_key: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            try:
                conn = self._connect()
                try:
                    self._ensure(conn)
                    rows = conn.execute(
                        "SELECT role, speaker_name, text, captured_at "
                        "FROM messages WHERE conversation_key = ? "
                        "ORDER BY captured_at DESC, id DESC LIMIT ?",
                        (conversation_key, limit),
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.DatabaseError:
                self._recover_broken()
                return []
        rows.reverse()
        return [{"who": row["role"], "name": row["speaker_name"],
                 "text": row["text"], "timestamp": _display_time(row["captured_at"])}
                for row in rows]

    def save_messages(self, conversation_key: str, messages: list[tuple],
                      retention_days: int = DEFAULT_RETENTION_DAYS,
                      max_mb: int = DEFAULT_MAX_MB) -> bool:
        now = _now()
        inserted = False
        with self._lock:
            try:
                conn = self._connect()
                try:
                    self._ensure(conn)
                    with conn:
                        for item in messages:
                            if len(item) < 3:
                                continue
                            role, name, text = str(item[0]), str(item[1] or ""), str(item[2]).strip()
                            if role not in {"her", "me"} or not text:
                                continue
                            captured_at = _now()
                            key = self._dedupe_key(conversation_key, role, name, text, captured_at)
                            cur = conn.execute(
                                "INSERT OR IGNORE INTO messages "
                                "(conversation_key, role, speaker_name, text, captured_at, dedupe_key) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (conversation_key, role, name, text, captured_at.isoformat(), key),
                            )
                            inserted = inserted or cur.rowcount > 0
                    self.cleanup(retention_days, max_mb, conn=conn)
                finally:
                    conn.close()
            except (OSError, sqlite3.DatabaseError):
                self._recover_broken()
                return False
        return inserted

    def save_judgment(self, conversation_key: str, result: dict) -> bool:
        answers = (result or {}).get("answers") or {}
        if not answers:
            return False
        payload = json.dumps(answers, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                conn = self._connect()
                try:
                    self._ensure(conn)
                    with conn:
                        conn.execute(
                            "INSERT INTO judgments "
                            "(conversation_key, message_tail_hash, created_at, answers_json) "
                            "VALUES (?, ?, ?, ?)",
                            (conversation_key, "", _now().isoformat(), payload),
                        )
                finally:
                    conn.close()
            except (OSError, sqlite3.DatabaseError):
                self._recover_broken()
                return False
        return True

    def cleanup(self, retention_days: int = DEFAULT_RETENTION_DAYS,
                max_mb: int = DEFAULT_MAX_MB, conn: sqlite3.Connection | None = None) -> None:
        """按日期和估算内容大小清理；只有明显超过上限时才做 VACUUM。"""
        own = conn is None
        with self._lock:
            try:
                conn = conn or self._connect()
                self._ensure(conn)
                days = max(1, min(3650, int(retention_days)))
                cap = max(1, min(2048, int(max_mb))) * 1024 * 1024
                cutoff = (_now() - timedelta(days=days)).isoformat()
                with conn:
                    conn.execute("DELETE FROM messages WHERE captured_at < ?", (cutoff,))
                    conn.execute("DELETE FROM judgments WHERE created_at < ?", (cutoff,))
                    # 文字内容很小，估算值比频繁 VACUUM 更稳定；超限时从最早消息开始删。
                    while True:
                        size = conn.execute(
                            "SELECT COALESCE(SUM(LENGTH(text) + LENGTH(speaker_name) + 80), 0) "
                            "FROM messages"
                        ).fetchone()[0]
                        if size <= cap:
                            break
                        conn.execute(
                            "DELETE FROM messages WHERE id IN "
                            "(SELECT id FROM messages ORDER BY captured_at, id LIMIT 100)"
                        )
                if own:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    if os.path.exists(self.path) and os.path.getsize(self.path) > cap * 2:
                        conn.execute("VACUUM")
            finally:
                if own and conn is not None:
                    conn.close()

    def clear_all(self) -> None:
        """清空全部消息和判断；这是用户点击重置设置时的明确操作。"""
        with self._lock:
            try:
                conn = self._connect()
                try:
                    self._ensure(conn)
                    with conn:
                        conn.execute("DELETE FROM messages")
                        conn.execute("DELETE FROM judgments")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                finally:
                    conn.close()
            except (OSError, sqlite3.DatabaseError):
                self._recover_broken()

    def _recover_broken(self) -> None:
        """数据库损坏时保留坏文件，下一次调用会创建新库，不阻断助手。"""
        self._initialized = False
        if not os.path.exists(self.path):
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        broken = f"{self.path}.broken-{stamp}"
        try:
            os.replace(self.path, broken)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            sidecar = self.path + suffix
            if os.path.exists(sidecar):
                try:
                    os.replace(sidecar, broken + suffix)
                except OSError:
                    pass


_STORE = HistoryStore()


def load_messages(conversation_key: str, limit: int) -> list[dict]:
    return _STORE.load_messages(conversation_key, limit)


def save_messages(conversation_key: str, messages: list[tuple], retention_days: int,
                  max_mb: int) -> bool:
    return _STORE.save_messages(conversation_key, messages, retention_days, max_mb)


def save_judgment(conversation_key: str, result: dict) -> bool:
    return _STORE.save_judgment(conversation_key, result)


def cleanup(retention_days: int, max_mb: int) -> None:
    _STORE.cleanup(retention_days, max_mb)


def clear_all() -> None:
    _STORE.clear_all()
