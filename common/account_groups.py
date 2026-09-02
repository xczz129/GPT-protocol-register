# -*- coding: utf-8 -*-
"""Persistent account group metadata."""

from __future__ import annotations

import json
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from config import ROOT

GROUPS_FILE = ROOT / "data" / "account_groups.json"
_LOCK = RLock()
_NAME_SPACE_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_group_name(value: Any) -> str:
    name = _NAME_SPACE_RE.sub(" ", str(value or "").strip())
    if not name:
        raise ValueError("分组名称不能为空")
    if len(name) > 48:
        raise ValueError("分组名称不能超过 48 个字符")
    return name


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        gid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if gid and name:
            out.append({
                "id": gid,
                "name": name,
                "created_at": str(item.get("created_at") or _now()),
                "updated_at": str(item.get("updated_at") or item.get("created_at") or _now()),
            })
    return out


def _write(path: Path, groups: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(groups, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
        Path(tmp_name).replace(path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def list_groups(path: Path = GROUPS_FILE, *, ensure_default: bool = True) -> list[dict[str, Any]]:
    with _LOCK:
        groups = _read(path)
        if not groups and ensure_default:
            now = _now()
            groups = [{
                "id": f"grp_{uuid.uuid4().hex[:12]}",
                "name": "默认分组",
                "created_at": now,
                "updated_at": now,
            }]
            _write(path, groups)
        return [dict(item) for item in groups]


def get_group(group_id: str, path: Path = GROUPS_FILE) -> dict[str, Any] | None:
    target = str(group_id or "").strip()
    return next((item for item in list_groups(path) if item["id"] == target), None)


def create_group(name: str, path: Path = GROUPS_FILE) -> dict[str, Any]:
    clean = normalize_group_name(name)
    with _LOCK:
        groups = _read(path)
        if any(item["name"].casefold() == clean.casefold() for item in groups):
            raise ValueError("分组名称已存在")
        now = _now()
        group = {
            "id": f"grp_{uuid.uuid4().hex[:12]}",
            "name": clean,
            "created_at": now,
            "updated_at": now,
        }
        groups.append(group)
        _write(path, groups)
        return dict(group)


def rename_group(group_id: str, name: str, path: Path = GROUPS_FILE) -> dict[str, Any]:
    clean = normalize_group_name(name)
    target = str(group_id or "").strip()
    with _LOCK:
        groups = list_groups(path)
        if any(
            item["id"] != target and item["name"].casefold() == clean.casefold()
            for item in groups
        ):
            raise ValueError("分组名称已存在")
        group = next((item for item in groups if item["id"] == target), None)
        if group is None:
            raise KeyError("分组不存在")
        group["name"] = clean
        group["updated_at"] = _now()
        _write(path, groups)
        return dict(group)


def delete_group(group_id: str, path: Path = GROUPS_FILE) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = str(group_id or "").strip()
    with _LOCK:
        groups = list_groups(path)
        if len(groups) <= 1:
            raise ValueError("至少保留一个分组")
        removed = next((item for item in groups if item["id"] == target), None)
        if removed is None:
            raise KeyError("分组不存在")
        remaining = [item for item in groups if item["id"] != target]
        _write(path, remaining)
        return dict(removed), [dict(item) for item in remaining]
