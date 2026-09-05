# -*- coding: utf-8 -*-
"""Save Playwright cookies for chatgpt accounts."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from threading import RLock

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from config import COOKIE_OUTPUT_DIR

_ACCOUNTS_TXT_LOCK = RLock()


def _rewrite_account_line(path: str, email: str, password: str, key_val: str) -> None:
    """Upsert ``email|password|session`` in accounts.txt.

    Concurrent registration tasks used to append blindly, producing duplicate
    rows and occasional interleaving.  This keeps the latest session per email
    and writes atomically.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    wanted = str(email or "").strip().lower()
    line = f"{email}|{password or ''}|{key_val}"
    lock_path = target.with_suffix(target.suffix + ".lock")
    with _ACCOUNTS_TXT_LOCK, lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            rows: list[str] = []
            if target.is_file():
                rows = [
                    raw.strip()
                    for raw in target.read_text(encoding="utf-8", errors="replace").splitlines()
                    if raw.strip()
                ]
            kept: list[str] = []
            replaced = False
            for row in rows:
                row_email = row.split("|", 1)[0].strip().lower()
                if row_email == wanted:
                    if not replaced:
                        kept.append(line)
                        replaced = True
                    continue
                kept.append(row)
            if not replaced:
                kept.append(line)

            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=str(target.parent),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(kept) + ("\n" if kept else ""))
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(tmp_name).replace(target)
            finally:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except Exception:
                    pass
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


async def save_platform_cookies(
    context,
    platform,
    profile_id,
    email=None,
    password=None,
    key_cookie_names=(),
):
    cookies = await context.cookies()
    # Defensive: some CDP engines may return non-list; never iterate async gens
    if cookies is None:
        cookies = []
    elif not isinstance(cookies, list):
        try:
            cookies = list(cookies)
        except TypeError:
            cookies = []
    pdir = os.path.join(COOKIE_OUTPUT_DIR, platform)
    os.makedirs(pdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    key_val = None
    found_names = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        if c.get("name") in key_cookie_names:
            found_names.append(c["name"])
            if key_val is None:
                key_val = c["value"]

    # NextAuth splits large encrypted session cookies into .0/.1/... chunks.
    # Persist the reconstructed value instead of silently keeping only .0.
    for key_name in key_cookie_names:
        base = str(key_name or "")
        if base.endswith(".0"):
            base = base[:-2]
        chunks: list[tuple[int, str]] = []
        for cookie in cookies:
            name = str(cookie.get("name") or "") if isinstance(cookie, dict) else ""
            if not name.startswith(base + "."):
                continue
            suffix = name[len(base) + 1 :]
            if suffix.isdigit():
                chunks.append((int(suffix), str(cookie.get("value") or "")))
        if chunks:
            chunks.sort(key=lambda item: item[0])
            expected = list(range(len(chunks)))
            if [index for index, _value in chunks] == expected:
                key_val = "".join(value for _index, value in chunks)
                break

    print(f"  [{platform}] {len(cookies)} 个 cookie，关键 cookie: {found_names}")

    # 会话 cookie 属敏感凭据：原子写入 + 0600 权限，避免半写文件和同机其它用户可读
    full_path = os.path.join(pdir, f"full_{profile_id}_{ts}.json")
    fd, tmp_name = tempfile.mkstemp(
        dir=pdir, prefix=".full_cookies_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, full_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    print(f"  [{platform}] 完整 cookie 已保存: {full_path}")

    if key_val and email:
        acc_path = os.path.join(pdir, "accounts.txt")
        _rewrite_account_line(acc_path, email, password or "", key_val)
        print(f"  [{platform}] 账号已保存: {acc_path}")

    return key_val, full_path
