# -*- coding: utf-8 -*-
"""SQLite-backed task history with one-time JSON migration."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import ROOT

DEFAULT_LEGACY_PATH = ROOT / "data" / "task_history.json"
DEFAULT_DB_PATH = ROOT / "data" / "task_history.sqlite3"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _db_path(legacy_path: Path) -> Path:
    path = Path(legacy_path)
    try:
        if path.resolve() == DEFAULT_LEGACY_PATH.resolve():
            return DEFAULT_DB_PATH
    except OSError:
        pass
    return path.with_suffix(".sqlite3")


def _empty_runtime() -> dict[str, Any]:
    return {"version": 1, "updated_at": "", "cards": [], "last_job": {}}


def _normalize_runtime(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_runtime()
    cards = value.get("cards") if isinstance(value.get("cards"), list) else []
    last_job = value.get("last_job") if isinstance(value.get("last_job"), dict) else {}
    return {
        "version": 1,
        "updated_at": str(value.get("updated_at") or ""),
        "cards": [dict(item) for item in cards if isinstance(item, dict)],
        "last_job": dict(last_job),
    }


def _read_legacy(path: Path) -> dict[str, Any]:
    try:
        return _normalize_runtime(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return _empty_runtime()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=15.0)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_cards (
            card_id TEXT PRIMARY KEY,
            updated_at_epoch REAL NOT NULL DEFAULT 0,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_cards_updated
            ON task_cards(updated_at_epoch DESC, card_id ASC);
        CREATE TABLE IF NOT EXISTS task_meta (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return connection


def _card_id(card: dict[str, Any]) -> str:
    batch_id = str(card.get("batch_id") or "legacy").strip() or "legacy"
    index = str(card.get("index") or "0").strip() or "0"
    return str(card.get("card_id") or f"{batch_id}:{index}").strip()


def _sort_cards(cards: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(item) for item in cards if isinstance(item, dict)),
        key=lambda item: float(item.get("updated_at_epoch") or item.get("started_at_epoch") or 0),
        reverse=True,
    )


def _read_db(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    sql = "SELECT payload FROM task_cards ORDER BY updated_at_epoch DESC, card_id"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (max(0, int(limit)),)
    rows = connection.execute(sql, params).fetchall()
    cards: list[dict[str, Any]] = []
    for row in rows:
        try:
            item = json.loads(str(row[0]))
        except Exception:
            continue
        if isinstance(item, dict):
            cards.append(item)
    metadata = {
        str(name): str(value)
        for name, value in connection.execute("SELECT name, value FROM task_meta")
    }
    try:
        last_job = json.loads(metadata.get("last_job") or "{}")
    except Exception:
        last_job = {}
    return _normalize_runtime(
        {
            "updated_at": metadata.get("updated_at", ""),
            "cards": cards,
            "last_job": last_job if isinstance(last_job, dict) else {},
        }
    )


def _write_db(
    path: Path,
    runtime: dict[str, Any],
    *,
    replace_cards: bool = False,
) -> dict[str, Any]:
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if replace_cards:
            connection.execute("DELETE FROM task_cards")
        rows: list[tuple[str, float, str]] = []
        for raw in runtime["cards"]:
            card = dict(raw)
            card["card_id"] = _card_id(card)
            try:
                updated_epoch = float(card.get("updated_at_epoch") or card.get("started_at_epoch") or 0)
            except (TypeError, ValueError):
                updated_epoch = 0.0
            rows.append(
                (card["card_id"], updated_epoch, json.dumps(card, ensure_ascii=False))
            )
        if rows:
            connection.executemany(
                """
                INSERT INTO task_cards(card_id, updated_at_epoch, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET
                    updated_at_epoch=excluded.updated_at_epoch,
                    payload=excluded.payload
                """,
                rows,
            )
        connection.execute(
            "INSERT INTO task_meta(name, value) VALUES ('updated_at', ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (str(runtime.get("updated_at") or _now()),),
        )
        connection.execute(
            "INSERT INTO task_meta(name, value) VALUES ('last_job', ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (json.dumps(runtime.get("last_job") or {}, ensure_ascii=False),),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return _normalize_runtime(runtime)


def load_runtime(
    legacy_path: Path = DEFAULT_LEGACY_PATH,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    db_path = _db_path(Path(legacy_path))
    with _LOCK:
        needs_migration = not db_path.is_file() and Path(legacy_path).is_file()
        if needs_migration:
            legacy = _read_legacy(Path(legacy_path))
            return _write_db(db_path, legacy, replace_cards=True)
        connection = _connect(db_path)
        try:
            return _read_db(connection, limit=limit)
        finally:
            connection.close()


def save_runtime(
    cards: Iterable[dict[str, Any]],
    last_job: dict[str, Any],
    legacy_path: Path = DEFAULT_LEGACY_PATH,
) -> dict[str, Any]:
    """UPSERT only supplied cards; existing history stays untouched in SQLite."""
    with _LOCK:
        db_path = _db_path(Path(legacy_path))
        if not db_path.is_file() and Path(legacy_path).is_file():
            load_runtime(legacy_path)
        prepared = [dict(raw) for raw in cards if isinstance(raw, dict)]
        runtime = {
            "version": 1,
            "updated_at": _now(),
            "cards": _sort_cards(prepared),
            "last_job": dict(last_job or {}),
        }
        return _write_db(db_path, runtime)


def delete_runtime(
    keys: Iterable[str] = (),
    *,
    delete_all: bool = False,
    legacy_path: Path = DEFAULT_LEGACY_PATH,
) -> int:
    with _LOCK:
        db_path = _db_path(Path(legacy_path))
        if not db_path.is_file() and Path(legacy_path).is_file():
            load_runtime(legacy_path)
        wanted = {str(key).strip() for key in keys if str(key).strip()}
        connection = _connect(db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            if delete_all:
                connection.execute("DELETE FROM task_cards")
            elif wanted:
                connection.executemany(
                    "DELETE FROM task_cards WHERE card_id = ?",
                    ((key,) for key in wanted),
                )
            removed = connection.total_changes - before
            connection.execute(
                "INSERT INTO task_meta(name, value) VALUES ('updated_at', ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (_now(),),
            )
            if delete_all:
                connection.execute(
                    "INSERT INTO task_meta(name, value) VALUES ('last_job', '{}') "
                    "ON CONFLICT(name) DO UPDATE SET value='{}'"
                )
            connection.commit()
            return removed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def export_runtime(legacy_path: Path = DEFAULT_LEGACY_PATH) -> bytes:
    payload = json.dumps(load_runtime(legacy_path), ensure_ascii=False, indent=2) + "\n"
    return payload.encode("utf-8")


def import_runtime(payload: bytes, legacy_path: Path = DEFAULT_LEGACY_PATH) -> dict[str, Any]:
    try:
        runtime = _normalize_runtime(json.loads(payload.decode("utf-8")))
    except Exception as exc:
        raise ValueError("invalid task history payload") from exc
    with _LOCK:
        return _write_db(_db_path(Path(legacy_path)), runtime, replace_cards=True)
