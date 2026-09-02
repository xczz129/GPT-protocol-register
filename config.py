# -*- coding: utf-8 -*-
"""Process env + dotenv defaults.

Prefer config.yaml via config_loader.load_config().
apply_fingerprint_env() syncs fingerprint.* into these env vars at runtime.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path | None = None) -> None:
    path = path or (ROOT / ".env")
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# Fingerprint browser: local | camoufox | obscura
FINGERPRINT_BROWSER = _env("FINGERPRINT_BROWSER", "local").strip().lower()

# Optional Clash CF node switch (leave empty to disable)
CLASH_API = _env("CLASH_API", "")
CLASH_SECRET = _env("CLASH_SECRET", "")
CLASH_GROUP = _env("CLASH_GROUP", "GLOBAL")
CHATGPT_CF_NODES = [
    n.strip()
    for n in _env(
        "CHATGPT_CF_NODES",
        "level1-日本01-NF,level1-日本02-NF,level1-新加坡01-NF,level1-新加坡02-NF",
    ).split(",")
    if n.strip()
]

TOKEN_OUTPUT_DIR = _env("TOKEN_OUTPUT_DIR", str(ROOT / "tokens"))
COOKIE_OUTPUT_DIR = _env("COOKIE_OUTPUT_DIR", str(ROOT / "cookies"))
