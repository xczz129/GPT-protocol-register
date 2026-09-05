"""Enumerate and downloadable-package local Codex auth JSON files."""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import shutil
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from .quota import extract_email, extract_plan, full_quota_groups


TOKEN_FILE_MAX_BYTES = 8 * 1024 * 1024
TOKEN_ARCHIVE_MAX_FILES = 500
TOKEN_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
TOKEN_TRASH_DIR_NAME = ".codex-token-trash"
FULL_QUOTA_SUB2API_CONCURRENCY = 10
FULL_QUOTA_SUB2API_PRIORITY = 1
NON_TOKEN_FILES = {
    "import-accounts.json",
    "import-jobs.json",
    "quota-config.json",
    "quota-report.json",
    "codex-full-quota-current.json",
    "codex-full-quota-sub2api.json",
}


class TokenFileError(RuntimeError):
    """A token file selection or packaging error suitable for the UI."""


def _root(directory: Path) -> Path:
    return directory.expanduser().resolve()


def _candidate(root: Path, name: str) -> Path | None:
    value = str(name or "").strip()
    path_name = Path(value)
    # Only a single basename is accepted.  This blocks traversal and keeps
    # download names stable even when a client sends encoded separators.
    if not value or path_name.name != value or path_name.suffix.lower() != ".json":
        return None
    if value in NON_TOKEN_FILES:
        return None
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _record(path: Path) -> Mapping[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > TOKEN_FILE_MAX_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    access_token = value.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    return value


def _metadata(root: Path, path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    stat = path.stat()
    email = str(value.get("email") or "").strip() or _claim(value, "email") or extract_email(value)
    if not email or email == "unknown":
        email = path.stem
    return {
        "filename": path.name,
        "email": email,
        "size": int(stat.st_size),
        "updated_at": int(stat.st_mtime),
        "expires_at": int(value.get("expires_at") or 0),
        "plan": str(value.get("plan_type") or value.get("plan") or "unknown"),
        "has_refresh_token": bool(str(value.get("refresh_token") or "").strip()),
        "download_url": f"/api/tokens/download?name={quote(path.name, safe='')}",
    }


def list_token_files(directory: Path) -> list[dict[str, Any]]:
    """Return metadata for valid auth JSON files in the primary directory."""
    root = _root(directory)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*.json"), key=lambda item: item.name.lower())
    except OSError:
        return []
    for path in paths:
        candidate = _candidate(root, path.name)
        if candidate is None or candidate != path.resolve():
            continue
        value = _record(candidate)
        if value is None:
            continue
        try:
            rows.append(_metadata(root, candidate, value))
        except OSError:
            continue
    rows.sort(key=lambda item: (str(item.get("email") or "").lower(), str(item.get("filename") or "").lower()))
    return rows


def resolve_token_file(directory: Path, name: str) -> tuple[Path, Mapping[str, Any]]:
    root = _root(directory)
    path = _candidate(root, name)
    if path is None:
        raise TokenFileError("Token 文件名不合法")
    value = _record(path)
    if value is None:
        raise TokenFileError("Token 文件不存在或格式无效")
    return path, value


def _record_email(value: Mapping[str, Any], path: Path) -> str:
    """Return the email represented by an auth record.

    Auth files produced by the different OAuth clients do not all put the
    address in the same field.  Keep the lookup rules in one place so the
    dashboard and download endpoints resolve the same account.
    """
    email = str(value.get("email") or "").strip() or _claim(value, "email") or extract_email(value)
    if not email or email.casefold() == "unknown":
        email = path.stem
    return email.strip()


def _token_roots(directory: Path, directories: Sequence[Path] | None = None) -> list[Path]:
    """Return unique token roots with the primary directory first."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for item in (directory, *(directories or ())):
        root = _root(Path(item))
        if root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def _direct_email_path(root: Path, email: str) -> Path | None:
    """Return an email-named path only when the value is a single filename."""
    if not email or Path(email).name != email or email in {".", ".."}:
        return None
    path = (root / f"{email}.json").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _iter_token_records(roots: Sequence[Path]):
    """Yield valid auth files once, preserving root priority."""
    seen_paths: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            paths = sorted(root.glob("*.json"), key=lambda item: item.name.lower())
        except OSError:
            continue
        for path in paths:
            candidate = _candidate(root, path.name)
            if candidate is None or candidate != path.resolve() or candidate in seen_paths:
                continue
            seen_paths.add(candidate)
            value = _record(candidate)
            if value is not None:
                yield candidate, value


def resolve_token_file_by_email(
    directory: Path,
    email: str,
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[Path, Mapping[str, Any]]:
    """Resolve one auth JSON by its email without touching the quota network.

    The common case is an email-named file, which is checked directly first.
    A bounded metadata scan handles legacy files whose names are opaque or
    whose address is only present in a JWT claim.  The primary directory wins
    when duplicate addresses exist in configured roots.
    """
    wanted = str(email or "").strip()
    if (
        not wanted
        or len(wanted) > 320
        or "\x00" in wanted
        or Path(wanted).name != wanted
        or wanted in {".", ".."}
    ):
        raise TokenFileError("账号邮箱不合法")
    wanted_key = wanted.casefold()
    roots = _token_roots(directory, directories)

    # OAuth imports normally use ``<email>.json``.  This avoids parsing every
    # account for the per-row download button.
    for root in roots:
        direct = _direct_email_path(root, wanted)
        if direct is not None and direct.is_file():
            value = _record(direct)
            if value is not None and _record_email(value, direct).casefold() == wanted_key:
                return direct, value
        # Filesystems are case-sensitive while email comparison is not.  A
        # case-folded filename check remains cheap and covers hand-renamed
        # files without a full scan.
        try:
            matches = [item for item in root.glob("*.json") if item.name.casefold() == f"{wanted}.json".casefold()]
        except OSError:
            matches = []
        for candidate in matches:
            safe_candidate = _candidate(root, candidate.name)
            if safe_candidate is None:
                continue
            value = _record(safe_candidate)
            if value is not None and _record_email(value, safe_candidate).casefold() == wanted_key:
                return safe_candidate, value

    for path, value in _iter_token_records(roots):
        if _record_email(value, path).casefold() == wanted_key:
            return path, value
    raise TokenFileError(f"没有找到账号对应的 Token：{wanted}")


def move_token_files_to_trash(
    directory: Path,
    emails: Sequence[str],
    *,
    directories: Sequence[Path] | None = None,
    trash_directory: Path | None = None,
) -> dict[str, Any]:
    """Move every local Token file for ``emails`` into a private trash dir.

    The quota inventory can contain both the project copy and a mirrored CPA
    copy, and older imports may use an opaque filename.  Deleting only
    ``<email>.json`` therefore leaves stale rows behind.  This helper scans
    the same bounded token index used by download routes, matches by the
    account email embedded in each record, and moves all matches atomically
    enough for a local dashboard operation.  The files remain recoverable in
    a timestamped trash batch instead of being unlinked permanently.
    """
    requested: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        email = str(raw or "").strip()
        key = email.casefold()
        if (
            not email
            or len(email) > 320
            or "\x00" in email
            or Path(email).name != email
            or email in {".", ".."}
            or key in seen
        ):
            continue
        seen.add(key)
        requested.append(email)
    if not requested:
        raise TokenFileError("请至少选择一个账号")
    if len(requested) > TOKEN_ARCHIVE_MAX_FILES:
        raise TokenFileError(f"一次最多处理 {TOKEN_ARCHIVE_MAX_FILES} 个账号")

    wanted = {email.casefold(): email for email in requested}
    roots = _token_roots(directory, directories)
    matches: list[tuple[Path, str]] = []
    for path, value in _iter_token_records(roots):
        key = _record_email(value, path).casefold()
        if key in wanted:
            matches.append((path, wanted[key]))

    # A quota row can outlive its file (for example after an earlier manual
    # cleanup).  Treat that as an idempotent delete and avoid creating an
    # empty trash hierarchy for a no-op request.
    if not matches:
        return {
            "deleted": [],
            "errors": [],
            "deleted_count": 0,
            "requested_count": len(requested),
            "trash_directory": str(_root(trash_directory or (Path(directory) / TOKEN_TRASH_DIR_NAME))),
        }

    trash_root = _root(trash_directory or (Path(directory) / TOKEN_TRASH_DIR_NAME))
    # Never allow a caller-provided trash path to resolve inside a source file
    # path in a way that could make the scan move its own destination.
    trash_root.mkdir(parents=True, exist_ok=True)
    try:
        trash_root.chmod(0o700)
    except OSError:
        pass
    batch_root = trash_root / time.strftime("%Y%m%d-%H%M%S")
    # Multiple browser requests can land in the same second.
    batch_root = batch_root / uuid.uuid4().hex[:10]
    batch_root.mkdir(parents=True, exist_ok=True)
    try:
        batch_root.chmod(0o700)
    except OSError:
        pass

    moved: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    used_names: set[str] = set()
    for path, email in matches:
        name = path.name
        if name in used_names:
            stem = path.stem
            suffix = path.suffix or ".json"
            ordinal = 2
            while f"{stem}-{ordinal}{suffix}" in used_names:
                ordinal += 1
            name = f"{stem}-{ordinal}{suffix}"
        used_names.add(name)
        destination = batch_root / name
        try:
            # The token index only yields regular files under configured roots;
            # re-check containment before moving in case a file changed while
            # the scan was running.
            source = path.resolve()
            if not source.is_file():
                raise OSError("文件已不存在")
            shutil.move(str(source), str(destination))
            moved.append({
                "email": email,
                "filename": path.name,
                "source": str(source),
                "trash_path": str(destination),
            })
        except (OSError, shutil.Error) as exc:
            errors.append({"email": email, "filename": path.name, "error": str(exc)[:240]})

    # Do not leave a forest of empty batch folders after a concurrent request
    # found no files or every file disappeared between scan and move.
    if not moved and not errors:
        try:
            batch_root.rmdir()
            if not any(trash_root.iterdir()):
                trash_root.rmdir()
        except OSError:
            pass
    return {
        "deleted": moved,
        "errors": errors,
        "deleted_count": len(moved),
        "requested_count": len(requested),
        "trash_directory": str(trash_root),
    }


def _archive_entries_for_emails(
    directory: Path,
    emails: Sequence[str],
    *,
    directories: Sequence[Path] | None = None,
) -> list[tuple[str, bytes, int]]:
    """Read requested accounts and prepare normalized CPA ZIP entries."""
    requested: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        email = str(raw or "").strip()
        key = email.casefold()
        if (
            not email
            or len(email) > 320
            or "\x00" in email
            or Path(email).name != email
            or email in {".", ".."}
            or key in seen
        ):
            continue
        seen.add(key)
        requested.append(email)
    if not requested:
        raise TokenFileError("请至少选择一个账号")
    if len(requested) > TOKEN_ARCHIVE_MAX_FILES:
        raise TokenFileError(f"一次最多下载 {TOKEN_ARCHIVE_MAX_FILES} 个文件")

    roots = _token_roots(directory, directories)
    # Build one lookup for fallback/opaque filenames.  Direct filename hits
    # still short-circuit before this map is needed for the usual case.
    resolved: dict[str, tuple[Path, Mapping[str, Any]]] = {}
    pending: list[str] = []
    for email in requested:
        wanted_key = email.casefold()
        found: tuple[Path, Mapping[str, Any]] | None = None
        for root in roots:
            direct = _direct_email_path(root, email)
            if direct is not None and direct.is_file():
                value = _record(direct)
                if value is not None and _record_email(value, direct).casefold() == wanted_key:
                    found = (direct, value)
                    break
            try:
                matches = [item for item in root.glob("*.json") if item.name.casefold() == f"{email}.json".casefold()]
            except OSError:
                matches = []
            for candidate in matches:
                safe_candidate = _candidate(root, candidate.name)
                if safe_candidate is None:
                    continue
                value = _record(safe_candidate)
                if value is not None and _record_email(value, safe_candidate).casefold() == wanted_key:
                    found = (safe_candidate, value)
                    break
            if found:
                break
        if found:
            resolved[wanted_key] = found
        else:
            pending.append(email)

    if pending:
        wanted = {email.casefold() for email in pending}
        for path, value in _iter_token_records(roots):
            key = _record_email(value, path).casefold()
            if key in wanted and key not in resolved:
                resolved[key] = (path, value)

    missing = [email for email in requested if email.casefold() not in resolved]
    if missing:
        raise TokenFileError("没有找到账号对应的 Token：" + ", ".join(missing[:3]))

    entries: list[tuple[str, bytes, int]] = []
    used_names: set[str] = set()
    total = 0
    # Use one timestamp for the whole export.  This matters for minimal/legacy
    # records that only carry ``expires_in``: the JSON and ZIP representations
    # must describe the exact same CPA object rather than differing by a few
    # milliseconds depending on which loop happened to run first.
    export_now = datetime.now(timezone.utc)
    for email in requested:
        path, record = resolved[email.casefold()]
        try:
            source_size = path.stat().st_size
        except OSError as exc:
            raise TokenFileError(f"读取 Token 文件失败：{path.name}") from exc
        if source_size > TOKEN_FILE_MAX_BYTES:
            raise TokenFileError(f"Token 文件超过大小限制：{path.name}")
        raw = cpa_record_bytes(record, email_hint=email, now=export_now)
        size = len(raw)
        total += size
        if total > TOKEN_ARCHIVE_MAX_BYTES:
            raise TokenFileError("选中的文件总大小超过下载上限")
        normalized = cpa_record(record, email_hint=email, now=export_now)
        arcname = _cpa_archive_name(str(normalized.get("email") or email), path.stem)
        if arcname in used_names:
            stem = Path(arcname).stem
            suffix = Path(arcname).suffix or ".json"
            ordinal = 2
            while f"{stem}-{ordinal}{suffix}" in used_names:
                ordinal += 1
            arcname = f"{stem}-{ordinal}{suffix}"
        used_names.add(arcname)
        entries.append((arcname, raw, size))
    return entries


def _cpa_entries_for_records(
    records: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[tuple[str, bytes, int]]:
    """Build the same normalized CPA entries used by the full-quota ZIP.

    Keeping this conversion in one helper prevents the aggregate JSON and ZIP
    endpoints from drifting.  In particular, records that derive ``expired``
    from ``expires_in`` get one shared reference timestamp.
    """
    export_now = now or datetime.now(timezone.utc)
    entries: list[tuple[str, bytes, int]] = []
    used_names: set[str] = set()
    total = 0
    for item in records:
        path = item.get("path")
        if not isinstance(path, Path):
            raise TokenFileError(f"Token 文件路径无效：{item.get('filename', '')}")
        try:
            source_size = path.stat().st_size
        except OSError as exc:
            raise TokenFileError(f"读取 Token 文件失败：{item.get('filename', '')}") from exc
        if source_size > TOKEN_FILE_MAX_BYTES:
            raise TokenFileError(f"Token 文件超过大小限制：{path.name}")
        token = item.get("token")
        if not isinstance(token, Mapping):
            raise TokenFileError(f"Token 记录格式无效：{path.name}")
        email_hint = str(item.get("email") or "").strip()
        raw = cpa_record_bytes(token, email_hint=email_hint, now=export_now)
        size = len(raw)
        total += size
        if total > TOKEN_ARCHIVE_MAX_BYTES:
            raise TokenFileError("选中的文件总大小超过下载上限")
        normalized = cpa_record(token, email_hint=email_hint, now=export_now)
        arcname = _cpa_archive_name(str(normalized.get("email") or email_hint), path.stem)
        if arcname in used_names:
            stem = Path(arcname).stem
            suffix = Path(arcname).suffix or ".json"
            ordinal = 2
            while f"{stem}-{ordinal}{suffix}" in used_names:
                ordinal += 1
            arcname = f"{stem}-{ordinal}{suffix}"
        used_names.add(arcname)
        entries.append((arcname, raw, size))
    return entries


def build_account_archive(
    directory: Path,
    emails: Sequence[str],
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[bytes, int]:
    """Build a fast ZIP containing selected accounts in strict CPA format."""
    entries = _archive_entries_for_emails(directory, emails, directories=directories)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, raw, _ in entries:
            archive.writestr(arcname, raw)
    return output.getvalue(), len(entries)


def _merged_cpa_payload(records: Sequence[Mapping[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Build a top-level array of strict CPA credential objects.

    CPA's native storage unit is one flat JSON object per account.  A bulk
    document therefore must not add an ``accounts``/``format`` wrapper that
    can be mistaken for a credential.  Each array element is produced by the
    exact same converter as the known-good per-account and ZIP downloads.
    """
    current = now or datetime.now(timezone.utc)
    accounts: list[dict[str, Any]] = []
    for item in records:
        record = item.get("token") if isinstance(item.get("token"), Mapping) else item
        hint = str(item.get("email") or "") if isinstance(item, Mapping) else ""
        if not isinstance(record, Mapping):
            continue
        accounts.append(cpa_record(record, email_hint=hint, now=current))
    if not accounts:
        raise TokenFileError("没有可合并的 CPA 账号")
    return accounts


def build_account_cpa_json(
    directory: Path,
    emails: Sequence[str],
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[bytes, int]:
    """Build one JSON array from selected strict CPA account objects."""
    # Reuse the same bounded resolver and validation as the selected ZIP.  The
    # ZIP entries already contain normalized CPA objects, so decoding them
    # keeps both download formats byte-for-byte schema compatible.
    entries = _archive_entries_for_emails(directory, emails, directories=directories)
    # Do not normalize a second time and do not infer an email from the ZIP
    # member name.  The decoded objects below are exactly the objects that the
    # selected-account CPA ZIP contains and that CPA already accepts.
    payload = [json.loads(raw) for _, raw, _ in entries]
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return body, len(payload)


def _account_records_for_emails(
    directory: Path,
    emails: Sequence[str],
    *,
    directories: Sequence[Path] | None = None,
) -> list[dict[str, Any]]:
    """Resolve selected email addresses while preserving their raw token fields."""
    requested: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        email = str(raw or "").strip()
        key = email.casefold()
        if (
            not email
            or len(email) > 320
            or "\x00" in email
            or Path(email).name != email
            or email in {".", ".."}
            or key in seen
        ):
            continue
        seen.add(key)
        requested.append(email)
    if not requested:
        raise TokenFileError("请至少选择一个账号")
    if len(requested) > TOKEN_ARCHIVE_MAX_FILES:
        raise TokenFileError(f"一次最多下载 {TOKEN_ARCHIVE_MAX_FILES} 个账号")
    records: list[dict[str, Any]] = []
    for email in requested:
        path, token = resolve_token_file_by_email(directory, email, directories=directories)
        records.append({
            "filename": path.name,
            "email": _record_email(token, path),
            "path": path,
            "token": dict(token),
        })
    return records


def build_account_sub2api_json(
    directory: Path,
    emails: Sequence[str],
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[bytes, int]:
    """Build one merged Sub2API document from selected account token files."""
    records = _account_records_for_emails(directory, emails, directories=directories)
    now = datetime.now(timezone.utc)
    accounts = [_sub2api_record(item, index, now) for index, item in enumerate(records)]
    payload = {
        "exported_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "proxies": [],
        "accounts": accounts,
    }
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return body, len(accounts)


def build_archive(directory: Path, names: Sequence[str] | None = None) -> tuple[bytes, int]:
    """Build an in-memory ZIP containing selected or all valid token files."""
    available = {row["filename"]: row for row in list_token_files(directory)}
    if names is None:
        selected_names = list(available)
    else:
        selected_names = []
        seen: set[str] = set()
        for raw in names:
            name = str(raw or "").strip()
            if name in seen:
                continue
            seen.add(name)
            if name not in available:
                raise TokenFileError(f"Token 文件不存在：{name}")
            selected_names.append(name)
    if not selected_names:
        raise TokenFileError("没有可下载的 Token 文件")
    if len(selected_names) > TOKEN_ARCHIVE_MAX_FILES:
        raise TokenFileError(f"一次最多下载 {TOKEN_ARCHIVE_MAX_FILES} 个文件")
    total = sum(int(available[name]["size"]) for name in selected_names)
    if total > TOKEN_ARCHIVE_MAX_BYTES:
        raise TokenFileError("选中的文件总大小超过下载上限")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in selected_names:
            path, _ = resolve_token_file(directory, name)
            try:
                archive.write(path, arcname=name)
            except OSError as exc:
                raise TokenFileError(f"读取 Token 文件失败：{name}") from exc
    return output.getvalue(), len(selected_names)


def _decode_jwt_payload(value: Any) -> dict[str, Any] | None:
    """Decode a JWT payload for metadata only; signature verification is upstream."""
    if not isinstance(value, str) or value.count(".") < 1:
        return None
    try:
        encoded = value.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def _claim_sources(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the record and useful OpenAI JWT claim sections."""
    sources: list[Mapping[str, Any]] = [record]
    seen: set[int] = {id(record)}
    for key in ("id_token", "access_token"):
        payload = _decode_jwt_payload(record.get(key))
        if not payload or id(payload) in seen:
            continue
        seen.add(id(payload))
        sources.append(payload)
        for section_name in ("https://api.openai.com/auth", "https://api.openai.com/profile"):
            section = payload.get(section_name)
            if isinstance(section, Mapping) and id(section) not in seen:
                seen.add(id(section))
                sources.append(section)
    return sources


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _claim(record: Mapping[str, Any], *keys: str) -> str:
    for source in _claim_sources(record):
        for key in keys:
            value = source.get(key)
            text = _first_non_empty(value)
            if text:
                return text
    return ""


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO, Unix-second, or Unix-millisecond timestamps as UTC."""
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number > 1e11:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        return _parse_timestamp(numeric)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_timestamp(value: Any) -> str:
    parsed = _parse_timestamp(value)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") if parsed else ""


# CPA consumes one flat credential object per account.  Keep this schema in a
# single converter so the per-account button, selected-account ZIP, and the
# 100%-quota ZIP cannot drift apart as the source token files evolve.
CPA_FIELDS = (
    "type",
    "account_id",
    "chatgpt_account_id",
    "email",
    "name",
    "plan_type",
    "chatgpt_plan_type",
    "id_token",
    "access_token",
    "refresh_token",
    "last_refresh",
    "expired",
)


def _cpa_email(record: Mapping[str, Any], email_hint: str = "") -> str:
    for candidate in (
        record.get("email"),
        _claim(record, "email", "preferred_username", "username"),
        extract_email(record),
        email_hint,
    ):
        email = _first_non_empty(candidate)
        if email and email.casefold() != "unknown":
            return email
    return "unknown"


def _cpa_account_id(record: Mapping[str, Any]) -> str:
    # Both fields are intentionally sourced from one value.  CPA and its
    # importers treat them as aliases, and a number of older token files only
    # contain one of the two names.
    return _first_non_empty(
        record.get("account_id"),
        record.get("chatgpt_account_id"),
        _claim(record, "chatgpt_account_id", "account_id", "accountId"),
    )


def _cpa_plan(record: Mapping[str, Any]) -> str:
    plan = ""
    for candidate in (
        record.get("plan_type"),
        record.get("chatgpt_plan_type"),
        _claim(record, "chatgpt_plan_type", "plan_type", "planType"),
        extract_plan(record),
    ):
        value = _first_non_empty(candidate)
        if value and value.casefold() not in {"unknown", "none", "null"}:
            plan = value
            break
    return plan.lower() if plan else "unknown"


def _cpa_expired(record: Mapping[str, Any], now: datetime) -> str:
    # Prefer the value explicitly written by the OAuth exporter, then the
    # numeric expiry and finally the JWT exp claim.  ``expires_in`` is a last
    # resort for minimal hand-created records.
    for value in (
        record.get("expired"),
        record.get("expires_at"),
        _claim(record, "exp"),
    ):
        result = _iso_timestamp(value)
        if result:
            return result
    raw_expires_in = record.get("expires_in")
    try:
        seconds = int(float(raw_expires_in))
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        return _iso_timestamp(now + timedelta(seconds=seconds))
    return ""


def cpa_record(
    record: Mapping[str, Any],
    *,
    email_hint: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the strict flat CPA credential shape used by every CPA export.

    Source files may be Sub2API-shaped, legacy OAuth-shaped, or contain JWT
    claims instead of top-level metadata.  The output deliberately contains
    exactly :data:`CPA_FIELDS`, in that order, with ``name`` equal to the
    resolved email as expected by CPA.
    """
    if not isinstance(record, Mapping):
        raise TokenFileError("Token 记录格式无效")
    current = now or datetime.now(timezone.utc)
    email = _cpa_email(record, email_hint)
    account_id = _cpa_account_id(record)
    plan = _cpa_plan(record)
    last_refresh = (
        _iso_timestamp(record.get("last_refresh"))
        or _iso_timestamp(record.get("created_at"))
    )
    return {
        "type": "codex",
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "email": email,
        "name": email,
        "plan_type": plan,
        "chatgpt_plan_type": plan,
        "id_token": _first_non_empty(record.get("id_token")),
        "access_token": _first_non_empty(record.get("access_token")),
        "refresh_token": _first_non_empty(record.get("refresh_token")),
        "last_refresh": last_refresh,
        "expired": _cpa_expired(record, current),
    }


def cpa_record_bytes(
    record: Mapping[str, Any],
    *,
    email_hint: str = "",
    now: datetime | None = None,
) -> bytes:
    """Serialize :func:`cpa_record` as a stable, importable JSON file."""
    payload = cpa_record(record, email_hint=email_hint, now=now)
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _cpa_archive_name(email: str, fallback: str = "account") -> str:
    """Return an email-based ZIP member name without allowing path entries."""
    value = str(email or "").strip() or str(fallback or "account").strip() or "account"
    value = value.replace("/", "_").replace("\\", "_").replace("\r", "_").replace("\n", "_")
    if value in {".", ".."}:
        value = str(fallback or "account").strip() or "account"
    return f"{value}.json"


def cpa_filename(record: Mapping[str, Any], *, email_hint: str = "", fallback: str = "account") -> str:
    """Return the safe email-based filename for one normalized CPA record."""
    payload = cpa_record(record, email_hint=email_hint)
    return _cpa_archive_name(str(payload.get("email") or email_hint), fallback)


def _expiry_info(record: Mapping[str, Any], now: datetime) -> tuple[str, int]:
    """Return Sub2API's ISO expiry and non-negative seconds remaining."""
    payloads = _claim_sources(record)
    expiry: datetime | None = None
    for value in (record.get("expires_at"), record.get("expired")):
        expiry = _parse_timestamp(value)
        if expiry:
            break
    if expiry is None:
        for source in payloads:
            expiry = _parse_timestamp(source.get("exp"))
            if expiry:
                break
    raw_expires_in = record.get("expires_in")
    try:
        expires_in = max(0, int(float(raw_expires_in)))
    except (TypeError, ValueError):
        expires_in = 0
    if expiry is None and expires_in:
        expiry = now + timedelta(seconds=expires_in)
    if expiry is None:
        return "", expires_in
    return _iso_timestamp(expiry), max(0, int((expiry - now).total_seconds()))


def _email_key(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", email.strip().lower()).strip("_")


def _sub2api_record(item: Mapping[str, Any], index: int, now: datetime) -> dict[str, Any]:
    token = item.get("token") if isinstance(item.get("token"), Mapping) else {}
    path = item.get("path")
    fallback_name = path.stem if isinstance(path, Path) else str(item.get("filename") or "")
    email = _first_non_empty(item.get("email"), token.get("email"), _claim(token, "email"))
    if not email or email.casefold() == "unknown":
        email = fallback_name or f"account-{index + 1}"
    name = email or fallback_name or f"account-{index + 1}"
    account_id = _first_non_empty(
        token.get("account_id"), token.get("chatgpt_account_id"), _claim(token, "chatgpt_account_id", "account_id", "accountId"),
    )
    user_id = _first_non_empty(
        token.get("chatgpt_user_id"), token.get("user_id"), _claim(token, "chatgpt_user_id", "user_id"),
    )
    plan_type = _first_non_empty(token.get("plan_type"), extract_plan(token)) or "unknown"
    expires_at, expires_in = _expiry_info(token, now)
    credentials: dict[str, Any] = {}
    credential_values = (
        ("access_token", token.get("access_token")),
        ("id_token", token.get("id_token")),
        ("refresh_token", token.get("refresh_token")),
        ("chatgpt_account_id", account_id),
        ("chatgpt_user_id", user_id),
        ("email", email),
        ("expires_at", expires_at),
        ("expires_in", expires_in),
        ("plan_type", plan_type),
    )
    for key, value in credential_values:
        if value not in (None, ""):
            credentials[key] = value
    extra: dict[str, Any] = {
        "email": email,
        "email_key": _email_key(email),
        "name": name,
        "source": "chatgpt_web_session",
    }
    last_refresh = _iso_timestamp(token.get("last_refresh")) or _iso_timestamp(token.get("created_at"))
    if last_refresh:
        extra["last_refresh"] = last_refresh
    return {
        "name": name,
        "platform": "openai",
        "type": "oauth",
        "concurrency": FULL_QUOTA_SUB2API_CONCURRENCY,
        "priority": FULL_QUOTA_SUB2API_PRIORITY,
        "credentials": credentials,
        "extra": extra,
    }


def _quota_token_records(directories: Sequence[Path], report: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Collect raw token records for the report's current 100% groups."""
    groups = report.get("full_quota_accounts")
    if not isinstance(groups, Mapping):
        groups = full_quota_groups(report.get("accounts") or [])
    wanted: dict[str, str] = {}
    normalised_groups: dict[str, list[str]] = {"with_reset_card": [], "without_reset_card": []}
    for group in normalised_groups:
        values = groups.get(group) if isinstance(groups, Mapping) else []
        if not isinstance(values, list):
            values = []
        for value in values:
            email = str(value or "").strip()
            key = email.casefold()
            if not key or key in wanted:
                continue
            wanted[key] = group
            normalised_groups[group].append(email)

    found: dict[str, dict[str, Any]] = {}
    for directory in directories:
        root = _root(Path(directory))
        if not root.is_dir():
            continue
        try:
            paths = sorted(root.glob("*.json"), key=lambda item: item.name.lower())
        except OSError:
            continue
        for path in paths:
            if path.name in NON_TOKEN_FILES:
                continue
            record = _record(path)
            if record is None:
                continue
            email = str(record.get("email") or "").strip() or _claim(record, "email") or extract_email(record)
            if not email or email.casefold() == "unknown":
                email = path.stem
            key = email.casefold()
            if key not in wanted or key in found:
                continue
            found[key] = {
                "filename": path.name,
                "email": email,
                "group": wanted[key],
                "path": path,
                "token": dict(record),
            }
    records = [
        found[key]
        for group in ("with_reset_card", "without_reset_card")
        for email in normalised_groups[group]
        if (key := email.casefold()) in found
    ]
    return records, normalised_groups


def build_full_quota_json(
    directory: Path,
    report: Mapping[str, Any],
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[bytes, int]:
    """Build one JSON export containing the current 100%-quota token records."""
    roots = list(directories or (directory,))
    if directory not in roots:
        roots.insert(0, directory)
    records, _ = _quota_token_records(roots, report)
    if not records:
        raise TokenFileError("当前没有找到可导出的 100% 额度 Token")
    groups = {
        group: [item["email"] for item in records if item.get("group") == group]
        for group in ("with_reset_card", "without_reset_card")
    }
    payload = {
        "format": "codex-full-quota-export-v1",
        "generated_at": int(report.get("generated_at") or 0),
        "source": str(report.get("source") or "unknown"),
        "criteria": "所有已知额度窗口 remaining_percent 均为 100%",
        "counts": {key: len(values) for key, values in groups.items()},
        "groups": groups,
        # Keep the original auth-file shape in ``accounts`` so the aggregate
        # JSON can be split into normal per-email files without a transform.
        "accounts": [item["token"] for item in records],
        "files": [
            {"filename": item["filename"], "email": item["email"], "group": item["group"]}
            for item in records
        ],
    }
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return body, len(records)


def _quota_roots(directory: Path, directories: Sequence[Path] | None) -> list[Path]:
    roots = list(directories or (directory,))
    primary = _root(directory)
    if not any(_root(Path(item)) == primary for item in roots):
        roots.insert(0, directory)
    return roots


def build_full_quota_sub2api_json(
    directory: Path,
    report: Mapping[str, Any],
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[bytes, int]:
    """Build a Sub2API-compatible aggregate with a guaranteed account name."""
    records, _ = _quota_token_records(_quota_roots(directory, directories), report)
    if not records:
        raise TokenFileError("当前没有找到可导出的 100% 额度 Token")
    # ``exported_at`` and ``expires_in`` describe the download moment, not the
    # potentially stale timestamp at which the quota report was cached.
    now = datetime.now(timezone.utc)
    accounts = [_sub2api_record(item, index, now) for index, item in enumerate(records)]
    payload = {
        "exported_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "proxies": [],
        "accounts": accounts,
    }
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return body, len(accounts)


def build_full_quota_cpa_json(
    directory: Path,
    report: Mapping[str, Any],
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[bytes, int]:
    """Build one strict-CPA JSON array for current 100% accounts."""
    records, _ = _quota_token_records(_quota_roots(directory, directories), report)
    if not records:
        raise TokenFileError("当前没有找到可导出的 100% 额度 Token")
    # Decode the exact bytes that the ZIP endpoint emits.  Each array element
    # is therefore byte/schema-identical to the corresponding ZIP member.
    entries = _cpa_entries_for_records(records)
    payload = [json.loads(raw) for _, raw, _ in entries]
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return body, len(payload)


def build_full_quota_cpa_archive(
    directory: Path,
    report: Mapping[str, Any],
    *,
    directories: Sequence[Path] | None = None,
) -> tuple[bytes, int]:
    """Build a ZIP containing each selected account in strict CPA format."""
    records, _ = _quota_token_records(_quota_roots(directory, directories), report)
    if not records:
        raise TokenFileError("当前没有找到可导出的 100% 额度 Token")
    if len(records) > TOKEN_ARCHIVE_MAX_FILES:
        raise TokenFileError(f"一次最多导出 {TOKEN_ARCHIVE_MAX_FILES} 个文件")

    entries = _cpa_entries_for_records(records)

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, raw, _ in entries:
            archive.writestr(arcname, raw)
    return output.getvalue(), len(entries)
