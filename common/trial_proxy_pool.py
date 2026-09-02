# -*- coding: utf-8 -*-
"""Dedicated country proxy pool for Plus/free-trial eligibility checks."""
from __future__ import annotations

import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any

from config import ROOT
from proxy_util import normalize_proxy_url

PATH = ROOT / "data" / "trial_eligibility_proxy_pool.json"


def _clean_lines(text: Any) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        normalized = normalize_proxy_url(line)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(line)
    return values


def read_trial_proxy_config() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8")) if PATH.is_file() else {}
    except Exception:
        raw = {}
    rows: list[dict[str, Any]] = []
    for item in raw.get("countries", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        country = str(item.get("country") or "").upper().strip()
        if len(country) != 2 or not country.isalpha():
            continue
        lines = _clean_lines(item.get("text"))
        rows.append({"country": country, "label": str(item.get("label") or country).strip()[:40], "text": "\n".join(lines), "count": len(lines)})
    return {"selected_country": str(raw.get("selected_country") or "").upper().strip(), "countries": rows, "path": str(PATH)}


def write_trial_proxy_config(selected_country: str, countries: list[dict[str, Any]]) -> dict[str, Any]:
    selected = str(selected_country or "").upper().strip()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in countries or []:
        country = str((item or {}).get("country") or "").upper().strip()
        if len(country) != 2 or not country.isalpha() or country in seen:
            continue
        seen.add(country)
        lines = _clean_lines((item or {}).get("text"))
        rows.append({"country": country, "label": str((item or {}).get("label") or country).strip()[:40], "text": "\n".join(lines)})
    if selected and selected not in seen:
        raise ValueError("资格检测默认国家未在代理池中")
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".trial-proxy.", suffix=".json", dir=str(PATH.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"selected_country": selected, "countries": rows}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp, 0o600); os.replace(temp, PATH)
    finally:
        Path(temp).unlink(missing_ok=True)
    return read_trial_proxy_config()


def selected_trial_proxies(
    *,
    limit: int = 0,
    exclude: set[str] | None = None,
) -> tuple[str, list[str]]:
    config = read_trial_proxy_config()
    selected = config.get("selected_country") or ""
    blocked = {normalize_proxy_url(value) for value in (exclude or set()) if value}
    for item in config["countries"]:
        if item["country"] == selected:
            lines = _clean_lines(item.get("text"))
            values = [normalize_proxy_url(line) for line in lines]
            values = [value for value in values if value and value not in blocked]
            # A country pool is intentionally per-check randomized.  This is
            # separate from the registration proxy allocator.
            random.shuffle(values)
            if limit > 0:
                values = values[: max(1, int(limit))]
            return selected, values
    return "", []


def selected_trial_proxy() -> tuple[str, str]:
    country, proxies = selected_trial_proxies(limit=1)
    return country, proxies[0] if proxies else ""
