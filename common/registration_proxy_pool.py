# -*- coding: utf-8 -*-
"""Country-scoped proxy pools used by account registration."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from config import ROOT
from proxy_util import normalize_proxy_url


PATH = ROOT / "data" / "registration_proxy_pools.json"
_REGION_RE = re.compile(r"(?:^|[-_])region-([a-z]{2})(?:[-_]|$)", re.IGNORECASE)


def _clean_lines(text: Any) -> list[str]:
    values: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        normalized = normalize_proxy_url(line)
        if normalized:
            values.append(line)
    return values


def _unique_proxies(lines: Iterable[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = normalize_proxy_url(line)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return values


def _declared_proxy_country(proxy: str) -> str:
    """Read provider credentials such as ``region-JP-sid-...`` when present."""
    match = _REGION_RE.search(str(proxy or ""))
    return match.group(1).upper() if match else ""


def _proxies_matching_country(lines: Iterable[str], country: str) -> tuple[list[str], list[str]]:
    expected = str(country or "").upper().strip()
    matching: list[str] = []
    mismatched: list[str] = []
    for proxy in _unique_proxies(lines):
        declared = _declared_proxy_country(proxy)
        if declared and declared != expected:
            mismatched.append(proxy)
        else:
            matching.append(proxy)
    return matching, mismatched


def _clean_country(value: Any) -> str:
    country = str(value or "").upper().strip()
    if len(country) != 2 or not country.isalpha():
        raise ValueError("国家代码必须是两个英文字母")
    return country


def _rows(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("国家代理池条目格式错误")
        country = _clean_country(item.get("country"))
        if country in seen:
            raise ValueError(f"国家代码重复：{country}")
        seen.add(country)
        lines = _clean_lines(item.get("text"))
        proxies = _unique_proxies(lines)
        schedulable, mismatched = _proxies_matching_country(lines, country)
        declared_countries = sorted(
            {
                declared
                for proxy in proxies
                if (declared := _declared_proxy_country(proxy))
            }
        )
        rows.append(
            {
                "country": country,
                "label": str(item.get("label") or country).strip()[:40] or country,
                "text": "\n".join(lines),
                "line_count": len(lines),
                "count": len(schedulable),
                "duplicate_count": len(lines) - len(proxies),
                "country_mismatch_count": len(mismatched),
                "declared_countries": declared_countries,
            }
        )
    return rows


def read_registration_proxy_pools() -> dict[str, Any]:
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8")) if PATH.is_file() else {}
    except Exception:
        raw = {}
    try:
        rows = _rows(raw.get("countries", []) if isinstance(raw, dict) else [])
    except ValueError:
        rows = []
    return {
        "items": rows,
        "countries": len(rows),
        "total": sum(int(item["count"]) for item in rows),
        "total_lines": sum(int(item["line_count"]) for item in rows),
        "total_duplicates": sum(int(item["duplicate_count"]) for item in rows),
        "path": str(PATH),
    }


def write_registration_proxy_pools(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = _rows(items or [])
    payload = {
        "countries": [
            {"country": item["country"], "label": item["label"], "text": item["text"]}
            for item in rows
        ]
    }
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(
        prefix=".registration-proxy-pools.",
        suffix=".json",
        dir=str(PATH.parent),
        text=True,
    )
    temp_path = Path(temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, PATH)
    finally:
        temp_path.unlink(missing_ok=True)
    return read_registration_proxy_pools()


def proxies_for_country(country: str) -> list[str]:
    selected = str(country or "").upper().strip()
    if not selected:
        return []
    for item in read_registration_proxy_pools()["items"]:
        if item["country"] != selected:
            continue
        matching, _mismatched = _proxies_matching_country(
            _clean_lines(item.get("text")), selected
        )
        return matching
    return []
