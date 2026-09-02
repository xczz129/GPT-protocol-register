# -*- coding: utf-8 -*-
"""Cloudflare/Clash node switching helper for the registration page flow."""

from __future__ import annotations

import os

from config import CHATGPT_CF_NODES, CLASH_API, CLASH_GROUP, CLASH_SECRET
from register.log import _log
from register.util import _cf_node_idx


def _switch_cf_node() -> str | None:
    api = (CLASH_API or os.environ.get("CLASH_API") or "").strip()
    if not api:
        return None
    nodes = CHATGPT_CF_NODES or []
    if not nodes:
        return None
    try:
        import requests

        secret = CLASH_SECRET or os.environ.get("CLASH_SECRET") or ""
        group = CLASH_GROUP or os.environ.get("CLASH_GROUP") or "GLOBAL"
        node = nodes[_cf_node_idx[0] % len(nodes)]
        _cf_node_idx[0] += 1
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        requests.put(
            f"{api.rstrip('/')}/proxies/{group}",
            json={"name": node},
            headers=headers,
            timeout=8,
        )
        try:
            requests.delete(f"{api.rstrip('/')}/connections", headers=headers, timeout=5)
        except Exception:
            pass
        return node
    except Exception as e:
        _log(f"[CF] 切换节点失败: {str(e)[:80]}")
        return None
