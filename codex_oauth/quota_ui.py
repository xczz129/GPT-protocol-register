"""Local web dashboard for Codex quota reports."""

from __future__ import annotations

import argparse
import errno
import gzip
import hmac
import json
import os
import socket
import threading
import time
import webbrowser
from dataclasses import replace
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse

from .quota import QuotaClient, QuotaError, QuotaService, load_config, save_import_settings, save_quota_settings
from .import_jobs import AccountStore, ImportErrorBase, ImportJobManager
from .sms import SmsError, SmsSettings, check_sms_balance, load_sms_settings, save_sms_settings
from .token_files import (
    TokenFileError,
    build_archive,
    build_full_quota_cpa_archive,
    build_full_quota_cpa_json,
    build_full_quota_json,
    build_full_quota_sub2api_json,
    build_account_archive,
    build_account_cpa_json,
    build_account_sub2api_json,
    cpa_record_bytes,
    cpa_filename,
    list_token_files,
    resolve_token_file,
    resolve_token_file_by_email,
)
from .web_auth import WEB_SESSION_COOKIE, WebAuth


WEB_ROOT = Path(__file__).resolve().parent / "web"


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodexQuota/1.0"

    # Keep the request path deliberately boring and predictable.  A browser
    # can open several dashboard resources at once; a larger accept queue and
    # daemon worker threads prevent a slow quota refresh from making the
    # listener look unavailable to a reverse proxy.
    def _safe_route_error(self, exc: Exception) -> None:
        """Turn an unexpected route exception into a proper HTTP response.

        ``BaseHTTPRequestHandler`` otherwise lets the exception escape after
        the proxy has already connected.  Caddy (and many hosted gateways)
        then reports that as a misleading 502.  Keep implementation details in
        the server log while returning a short, actionable JSON message.
        """
        if getattr(self, "_response_started", False):
            return
        self.log_error("dashboard route error: %s", exc)
        try:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "服务暂时繁忙，请稍后重试")
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    @property
    def dashboard(self) -> "QuotaHTTPServer":
        return self.server  # type: ignore[return-value]

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._response_started = True
        response_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # API responses and HTML may contain live state, credentials metadata,
        # or a session-dependent page.  Static assets opt into caching from
        # ``_serve_static`` via an overriding header.
        self.send_header("Cache-Control", response_headers.get("cache-control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            if str(key).lower() == "cache-control":
                continue
            self.send_header(str(key), str(value))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A browser/proxy may cancel a slow export while the worker has
            # already finished building it.  The client has gone away; do not
            # turn that normal disconnect into a noisy server traceback.
            return

    def _send_error_json(self, status: int, message: str) -> None:
        self._send(status, _json_bytes({"error": message}))

    def _action_key_ok(self) -> bool:
        supplied = self.headers.get("X-Quota-Action-Key", "")
        try:
            expected = self.dashboard.service.client.action_key()
        except QuotaError:
            return False
        return bool(supplied) and hmac.compare_digest(supplied.strip(), expected)

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > 2 * 1024 * 1024:
            return None
        try:
            raw = self.rfile.read(length) if length else b"{}"
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _imports(self) -> ImportJobManager:
        return self.dashboard.imports

    def _session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(WEB_SESSION_COOKIE)
        return str(morsel.value) if morsel else ""

    def _authenticated(self) -> bool:
        return self.dashboard.auth.authenticated(self._session_token())

    def _require_login(self) -> bool:
        if self._authenticated():
            return True
        self._send(HTTPStatus.UNAUTHORIZED, _json_bytes({
            "error": "请先登录",
            "auth_required": True,
        }))
        return False

    def _login(self) -> None:
        body = self._read_json_body()
        if body is None:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, "登录数据格式不正确")
        username = body.get("username")
        password = body.get("password")
        token = self.dashboard.auth.login(username, password)
        if not token:
            return self._send(HTTPStatus.UNAUTHORIZED, _json_bytes({
                "error": "账号或密码错误",
                "authenticated": False,
            }))
        cookie = (
            f"{WEB_SESSION_COOKIE}={token}; Path=/; Max-Age=28800; "
            "HttpOnly; SameSite=Lax"
        )
        self._send(HTTPStatus.OK, _json_bytes({"ok": True, "authenticated": True}), headers={"Set-Cookie": cookie})

    def _logout(self) -> None:
        self.dashboard.auth.logout(self._session_token())
        cookie = f"{WEB_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        self._send(HTTPStatus.OK, _json_bytes({"ok": True, "authenticated": False}), headers={"Set-Cookie": cookie})

    def _token_directory(self) -> Path:
        return self._imports().output_dir

    def _send_download(self, body: bytes, *, filename: str, content_type: str) -> None:
        safe_name = filename.replace("\r", "").replace("\n", "").replace('"', "_")
        disposition = f"attachment; filename=\"{safe_name}\"; filename*=UTF-8''{quote(safe_name)}"
        self._send(
            HTTPStatus.OK,
            body,
            content_type,
            headers={"Content-Disposition": disposition},
        )

    def _require_action_key(self) -> bool:
        if self._action_key_ok():
            return True
        self._send(HTTPStatus.UNAUTHORIZED, _json_bytes({
            "error": "需要操作密钥",
            "action_key_required": True,
            "action_key_file": self.dashboard.service.client.config.action_key_path.name
            if self.dashboard.service.client.config.action_key_path else "quota-action-key",
        }))
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # keep a route bug from becoming a gateway 502
            self._safe_route_error(exc)

    def do_HEAD(self) -> None:  # noqa: N802 - lightweight proxy/health-check support
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/healthz", "/readyz"}:
                status = self.dashboard.service.status()
                ready = bool(status.get("has_report") or status.get("refreshing"))
                payload = _json_bytes({
                    "ok": True,
                    "ready": ready,
                    "refreshing": bool(status.get("refreshing")),
                    "has_report": bool(status.get("has_report")),
                })
                self._send_headers_only(HTTPStatus.OK if path == "/healthz" or ready else HTTPStatus.SERVICE_UNAVAILABLE, len(payload))
                return
            if path in {"/", "/login"}:
                filename = "index.html" if self._authenticated() else "login.html"
                content_type = "text/html; charset=utf-8"
            elif path == "/favicon.svg":
                filename, content_type = "favicon.svg", "image/svg+xml"
            elif path in {"/assets/app.css", "/assets/app.js", "/assets/login.css", "/assets/login.js"}:
                filename = path.rsplit("/", 1)[-1]
                content_type = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}[Path(filename).suffix]
            else:
                self._send_headers_only(HTTPStatus.NOT_FOUND, len(_json_bytes({"error": "not found"})))
                return
            body = (WEB_ROOT / filename).read_bytes()
            self._send_headers_only(HTTPStatus.OK, len(body), content_type)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception as exc:
            self._safe_route_error(exc)

    def _send_headers_only(self, status: int, length: int, content_type: str = "application/json; charset=utf-8") -> None:
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in {"/", "/login"}:
            return self._serve_static("index.html" if self._authenticated() else "login.html", "text/html; charset=utf-8")
        if path == "/favicon.svg":
            return self._serve_static("favicon.svg", "image/svg+xml")
        if path == "/assets/app.css":
            return self._serve_static("app.css", "text/css; charset=utf-8")
        if path == "/assets/app.js":
            return self._serve_static("app.js", "application/javascript; charset=utf-8")
        if path == "/assets/login.css":
            return self._serve_static("login.css", "text/css; charset=utf-8")
        if path == "/assets/login.js":
            return self._serve_static("login.js", "application/javascript; charset=utf-8")
        if path == "/api/auth/status":
            return self._send(HTTPStatus.OK, _json_bytes({"authenticated": self._authenticated()}))
        if path.startswith("/api/") and not self._require_login():
            return
        if path in {"/healthz", "/readyz"}:
            status = self.dashboard.service.status()
            ready = bool(status.get("has_report") or status.get("refreshing"))
            payload = {
                "ok": True,
                "ready": ready,
                "refreshing": bool(status.get("refreshing")),
                "has_report": bool(status.get("has_report")),
            }
            return self._send(
                HTTPStatus.OK if path == "/healthz" or ready else HTTPStatus.SERVICE_UNAVAILABLE,
                _json_bytes(payload),
            )
        if path == "/api/status":
            service_status = self.dashboard.service.status()
            try:
                sms = load_sms_settings(self._imports().sms_settings_path).public_mapping()
            except SmsError:
                sms = {"enabled": False, "configured": False, "state_label": "接码配置读取失败"}
            return self._send(HTTPStatus.OK, _json_bytes({
                **service_status,
                "import": self._imports().runtime_status(),
                "sms": sms,
                "token_directory": str(self._token_directory()),
            }))
        if path == "/api/config":
            cfg = self.dashboard.service.client.config
            return self._send(HTTPStatus.OK, _json_bytes({
                "mode": cfg.mode,
                "auth_dir": cfg.auth_dir,
                "auth_dirs": list(cfg.auth_dirs),
                "concurrency": cfg.concurrency,
                "cache_seconds": cfg.cache_seconds,
                "reset_cards": cfg.mode == "local",
                "action_key_required": True,
                "action_key_file": cfg.action_key_path.name if cfg.action_key_path else "quota-action-key",
                "import_output_dir": str(self._imports().output_dir),
                "import_mirror_dir": str(self._imports().mirror_dir) if self._imports().mirror_dir else "",
                "import_store": str(self._imports().store.path),
                "import_concurrency": self._imports().concurrency,
                "import_concurrency_limit": None,
                "skip_phone_required": self._imports().skip_phone_required,
            }))
        if path == "/api/settings/quota":
            cfg = self.dashboard.service.client.config
            return self._send(HTTPStatus.OK, _json_bytes({
                "concurrency": cfg.concurrency,
                "cache_seconds": cfg.cache_seconds,
                "concurrency_limit": 32,
                "cache_limit": 3600,
            }))
        if path == "/api/quotas":
            force = query.get("refresh", ["0"])[0].lower() in {"1", "true", "yes"}
            report = self.dashboard.service.refresh_async() if force else self.dashboard.service.report(force=False)
            return self._send(HTTPStatus.OK, _json_bytes(report))
        if path == "/api/import/accounts":
            return self._send(HTTPStatus.OK, _json_bytes({"accounts": self._imports().store.public_all()}))
        if path == "/api/import/jobs":
            batch_id = query.get("batch", [""])[0]
            return self._send(HTTPStatus.OK, _json_bytes({"jobs": self._imports().list_jobs(batch_id)}))
        if path == "/api/tokens":
            return self._send(HTTPStatus.OK, _json_bytes({
                "directory": str(self._token_directory()),
                "tokens": list_token_files(self._token_directory()),
            }))
        if path == "/api/tokens/download":
            name = query.get("name", [""])[0]
            try:
                token_path, _ = resolve_token_file(self._token_directory(), name)
                self._send_download(
                    token_path.read_bytes(),
                    filename=token_path.name,
                    content_type="application/json; charset=utf-8",
                )
            except (TokenFileError, OSError) as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            return
        if path == "/api/tokens/account/download":
            email = query.get("email", [""])[0]
            try:
                cfg = self.dashboard.service.client.config
                directories = tuple(Path(item) for item in cfg.auth_dirs)
                token_path, token = resolve_token_file_by_email(
                    self._token_directory(), email, directories=directories,
                )
                # Account-row downloads are CPA downloads too.  Normalize the
                # source once more here so legacy/Sub2API-shaped files never
                # leak their alternate schema through this endpoint.
                body = cpa_record_bytes(token, email_hint=email)
                filename = cpa_filename(token, email_hint=email, fallback=token_path.stem)
                self._send_download(
                    body,
                    filename=filename,
                    content_type="application/json; charset=utf-8",
                )
            except (TokenFileError, OSError) as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            return
        if path == "/api/tokens/archive":
            try:
                archive, count = build_archive(self._token_directory())
                self._send_download(archive, filename="codex-tokens.zip", content_type="application/zip")
                return
            except TokenFileError as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        if path in {
            "/api/tokens/full-quota",
            "/api/tokens/full-quota.json",
            "/api/tokens/full-quota/sub2api.json",
            "/api/tokens/full-quota/cpa.json",
            "/api/tokens/full-quota/cpa.zip",
        }:
            try:
                # Exporting should use the report currently shown by the
                # dashboard.  ``report(False)`` still refreshes when the cache
                # is empty/expired, while avoiding a slow all-account network
                # sweep for every download click (old clients may still send
                # refresh=1).  The dedicated “刷新配额” action remains the
                # explicit forced-refresh path.
                report = self.dashboard.service.snapshot()
                if report is None:
                    report = self.dashboard.service.refresh_async()
                cfg = self.dashboard.service.client.config
                directories = tuple(Path(item) for item in cfg.auth_dirs)
                if path == "/api/tokens/full-quota/cpa.zip":
                    body, _ = build_full_quota_cpa_archive(
                        self._token_directory(), report, directories=directories,
                    )
                    self._send_download(body, filename="codex-full-quota-cpa.zip", content_type="application/zip")
                elif path == "/api/tokens/full-quota/cpa.json":
                    body, _ = build_full_quota_cpa_json(
                        self._token_directory(), report, directories=directories,
                    )
                    self._send_download(body, filename="codex-full-quota-cpa.json", content_type="application/json; charset=utf-8")
                else:
                    # The legacy endpoint now emits the strict Sub2API shape so
                    # an old bookmark/button cannot produce an account without
                    # the required top-level ``name`` field.
                    body, _ = build_full_quota_sub2api_json(
                        self._token_directory(), report, directories=directories,
                    )
                    self._send_download(body, filename="codex-full-quota-sub2api.json", content_type="application/json; charset=utf-8")
                return
            except (TokenFileError, QuotaError, OSError) as exc:
                return self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        if path.startswith("/api/import/jobs/"):
            job_id = unquote(path[len("/api/import/jobs/"):])
            if job_id:
                job = self._imports().job_view(job_id)
                if job is None:
                    return self._send_error_json(HTTPStatus.NOT_FOUND, "任务不存在")
                return self._send(HTTPStatus.OK, _json_bytes(job))
        if path in {"/api/settings/smsbower", "/api/settings/sms"}:
            try:
                settings = load_sms_settings(self._imports().sms_settings_path)
            except SmsError as exc:
                return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes(settings.public_mapping()))
        if path == "/api/settings/import":
            return self._send(HTTPStatus.OK, _json_bytes(self._imports().runtime_status()))
        self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            self._do_POST()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # keep a route bug from becoming a gateway 502
            self._safe_route_error(exc)

    def _do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            return self._login()
        if parsed.path == "/api/auth/logout":
            if not self._require_login():
                return
            return self._logout()
        if parsed.path.startswith("/api/") and not self._require_login():
            return
        if parsed.path == "/api/import/preview":
            body = self._read_json_body()
            if body is None or not isinstance(body.get("text"), str):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少账号文本")
            return self._send(HTTPStatus.OK, _json_bytes(self._imports().preview(body["text"])))
        if parsed.path == "/api/tokens/archive":
            body = self._read_json_body() or {}
            names = body.get("names")
            if names is not None and not isinstance(names, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "Token 文件列表格式不正确")
            try:
                archive, _ = build_archive(self._token_directory(), names)
                return self._send_download(archive, filename="codex-tokens.zip", content_type="application/zip")
            except TokenFileError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/tokens/accounts/archive":
            body = self._read_json_body() or {}
            emails = body.get("emails")
            if not isinstance(emails, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号邮箱列表格式不正确")
            try:
                cfg = self.dashboard.service.client.config
                directories = tuple(Path(item) for item in cfg.auth_dirs)
                archive, count = build_account_archive(
                    self._token_directory(),
                    [str(item) for item in emails],
                    directories=directories,
                )
                return self._send_download(
                    archive,
                    filename="codex-selected-accounts.zip",
                    content_type="application/zip",
                )
            except (TokenFileError, OSError) as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/tokens/accounts/cpa.json":
            body = self._read_json_body() or {}
            emails = body.get("emails")
            if not isinstance(emails, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号邮箱列表格式不正确")
            try:
                cfg = self.dashboard.service.client.config
                directories = tuple(Path(item) for item in cfg.auth_dirs)
                merged, _ = build_account_cpa_json(
                    self._token_directory(),
                    [str(item) for item in emails],
                    directories=directories,
                )
                return self._send_download(
                    merged,
                    filename="codex-selected-accounts-cpa.json",
                    content_type="application/json; charset=utf-8",
                )
            except (TokenFileError, OSError) as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/tokens/accounts/sub2api.json":
            body = self._read_json_body() or {}
            emails = body.get("emails")
            if not isinstance(emails, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号邮箱列表格式不正确")
            try:
                cfg = self.dashboard.service.client.config
                directories = tuple(Path(item) for item in cfg.auth_dirs)
                merged, _ = build_account_sub2api_json(
                    self._token_directory(),
                    [str(item) for item in emails],
                    directories=directories,
                )
                return self._send_download(
                    merged,
                    filename="codex-selected-accounts-sub2api.json",
                    content_type="application/json; charset=utf-8",
                )
            except (TokenFileError, OSError) as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/tokens/accounts/delete":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            emails = body.get("emails")
            if not isinstance(emails, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号邮箱列表格式不正确")
            try:
                cfg = self.dashboard.service.client.config
                result = self._imports().delete_assets_for_emails(
                    [str(item) for item in emails],
                    token_directories=tuple(Path(item) for item in cfg.auth_dirs),
                    trash_directory=Path(__file__).resolve().parents[2] / "data" / ".codex-token-trash",
                )
                # A local-mode report is built from the token directories.  A
                # moved file must invalidate the short-lived index before the
                # next background refresh, otherwise the just-deleted account
                # can be served from memory for up to a minute.
                deleted_emails = result.get("deleted_emails") or []
                remove_snapshot = getattr(self.dashboard.service, "remove_accounts_from_snapshot", None)
                if callable(remove_snapshot) and deleted_emails:
                    updated_report = remove_snapshot(deleted_emails)
                    if updated_report is not None:
                        result["report"] = updated_report
                invalidate = getattr(self.dashboard.service.client, "_invalidate_local_cache", None)
                if callable(invalidate):
                    invalidate()
                return self._send(HTTPStatus.OK, _json_bytes(result))
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except (TokenFileError, OSError) as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/import/accounts":
            if not self._require_action_key():
                return
            body = self._read_json_body()
            if body is None or not isinstance(body.get("text"), str):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少账号文本")
            try:
                result = self._imports().store.add_text(body["text"])
            except Exception as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes(result))
        if parsed.path == "/api/import/accounts/export":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            account_ids = body.get("account_ids")
            if account_ids is not None and not isinstance(account_ids, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号 ID 列表格式不正确")
            try:
                exported, count = self._imports().store.export_text(
                    [str(item) for item in account_ids] if account_ids is not None else None,
                )
                filename = "accounts-selected.txt" if account_ids is not None else "accounts-export.txt"
                return self._send_download(
                    exported,
                    filename=filename,
                    content_type="text/plain; charset=utf-8",
                )
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except OSError as exc:
                return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"导出账号失败：{exc}")
        if parsed.path == "/api/import/accounts/delete":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            account_ids = body.get("account_ids")
            if not isinstance(account_ids, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号 ID 列表格式不正确")
            try:
                result = self._imports().delete_accounts([str(item) for item in account_ids])
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except OSError as exc:
                return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"删除账号失败：{exc}")
            return self._send(HTTPStatus.OK, _json_bytes(result))
        if parsed.path == "/api/import/accounts/export-by-emails":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            emails = body.get("emails")
            if not isinstance(emails, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号邮箱列表格式不正确")
            try:
                exported, _ = self._imports().export_for_emails([str(item) for item in emails])
                return self._send_download(
                    exported,
                    filename="accounts-selected.txt",
                    content_type="text/plain; charset=utf-8",
                )
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/import/accounts/delete-by-emails":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            emails = body.get("emails")
            if not isinstance(emails, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号邮箱列表格式不正确")
            try:
                result = self._imports().delete_for_emails([str(item) for item in emails])
                return self._send(HTTPStatus.OK, _json_bytes(result))
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/import/jobs":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            ids = body.get("account_ids") or []
            if not isinstance(ids, list):
                ids = []
            try:
                result = self._imports().start([str(item) for item in ids], all_accounts=bool(body.get("all")))
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"启动任务失败：{exc}")
            return self._send(HTTPStatus.OK, _json_bytes(result))
        if parsed.path == "/api/import/jobs/reauthorize":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            emails = body.get("emails")
            if not isinstance(emails, list):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "账号邮箱列表格式不正确")
            try:
                result = self._imports().start_for_emails([str(item) for item in emails])
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"重新授权任务启动失败：{exc}")
            return self._send(HTTPStatus.OK, _json_bytes(result))
        if parsed.path.startswith("/api/import/jobs/") and parsed.path.endswith("/input"):
            if not self._require_action_key():
                return
            job_id = unquote(parsed.path[len("/api/import/jobs/"):-len("/input")])
            body = self._read_json_body() or {}
            try:
                result = self._imports().submit_input(job_id, str(body.get("kind") or ""), str(body.get("value") or ""))
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes(result))
        if parsed.path.startswith("/api/import/jobs/") and parsed.path.endswith("/cancel"):
            if not self._require_action_key():
                return
            job_id = unquote(parsed.path[len("/api/import/jobs/"):-len("/cancel")])
            if not self._imports().cancel(job_id):
                return self._send_error_json(HTTPStatus.NOT_FOUND, "任务不存在")
            return self._send(HTTPStatus.OK, _json_bytes(self._imports().job_view(job_id) or {}))
        if parsed.path.startswith("/api/import/jobs/") and parsed.path.endswith("/skip-phone"):
            if not self._require_action_key():
                return
            job_id = unquote(parsed.path[len("/api/import/jobs/"):-len("/skip-phone")])
            try:
                if not self._imports().skip_phone(job_id):
                    return self._send_error_json(HTTPStatus.NOT_FOUND, "任务不存在")
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes(self._imports().job_view(job_id) or {}))
        if parsed.path.startswith("/api/import/jobs/") and parsed.path.endswith("/retry"):
            if not self._require_action_key():
                return
            job_id = unquote(parsed.path[len("/api/import/jobs/"):-len("/retry")])
            try:
                result = self._imports().retry(job_id)
            except ImportErrorBase as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes(result))
        if parsed.path in {"/api/settings/sms/test-balance", "/api/settings/smsbower/test-balance"}:
            if not self._require_action_key():
                return
            try:
                settings = load_sms_settings(self._imports().sms_settings_path)
                details = check_sms_balance(settings)
            except SmsError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes({
                "ok": True,
                "provider": settings.provider,
                "provider_label": settings.provider_label,
                "checked_at": int(time.time()),
                "message": "余额查询成功",
                **details,
            }))
        if parsed.path in {"/api/settings/smsbower", "/api/settings/sms"}:
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            try:
                settings = SmsSettings.from_mapping(body)
                if settings.provider != "smsbower":
                    raise SmsError("此接码渠道已移除，仅支持 SMSBower")
                # Preserve only an existing SMSBower key when omitted.
                existing = load_sms_settings(self._imports().sms_settings_path)
                settings = replace(
                    settings,
                    api_key=settings.api_key or existing.api_key,
                )
                if settings.enabled and not settings.credentials_configured:
                    raise ImportErrorBase("启用 SMSBower 前请填写 API key")
                save_sms_settings(self._imports().sms_settings_path, settings)
            except (SmsError, ImportErrorBase) as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes(settings.public_mapping()))
        if parsed.path == "/api/settings/import":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            if "concurrency" not in body and "skip_phone_required" not in body:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少导入设置")
            try:
                saved = save_import_settings(
                    self.dashboard.service.client.config,
                    concurrency=body.get("concurrency") if "concurrency" in body else None,
                    skip_phone_required=body.get("skip_phone_required") if "skip_phone_required" in body else None,
                )
                value = self._imports().set_concurrency(saved["concurrency"])
                skip_value = self._imports().set_skip_phone_required(saved["skip_phone_required"])
            except (QuotaError, ValueError) as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes({"ok": True, **self._imports().runtime_status(), "concurrency": value, "skip_phone_required": skip_value}))
        if parsed.path == "/api/settings/quota":
            if not self._require_action_key():
                return
            body = self._read_json_body() or {}
            try:
                saved = save_quota_settings(
                    self.dashboard.service.client.config,
                    concurrency=body.get("concurrency"),
                    cache_seconds=body.get("cache_seconds"),
                )
            except (QuotaError, ValueError) as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send(HTTPStatus.OK, _json_bytes({"ok": True, **saved}))
        if parsed.path == "/api/refresh":
            report = self.dashboard.service.refresh_async()
            return self._send(HTTPStatus.OK, _json_bytes(report))
        prefix = "/api/accounts/"
        suffixes = ("/reset-credit", "/reset")
        if parsed.path.startswith(prefix) and any(parsed.path.endswith(suffix) for suffix in suffixes):
            if not self._action_key_ok():
                return self._send(HTTPStatus.UNAUTHORIZED, _json_bytes({
                    "error": "需要配额操作密钥。请读取 data/quota-action-key 后重试。",
                    "action_key_required": True,
                }))
            body = self._read_json_body()
            if not body or body.get("confirm") is not True:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "请确认使用一张重置卡")
            suffix = next(suffix for suffix in suffixes if parsed.path.endswith(suffix))
            selector = unquote(parsed.path[len(prefix):-len(suffix)])
            if not selector:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少账号标识")
            try:
                # Accept the stable local auth filename stem, email, or
                # account UUID so integrations do not have to know the UI's
                # preferred selector.
                result = self.dashboard.service.consume_reset_credit(
                    auth_index=selector, email=selector, account_id=selector,
                )
            except QuotaError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:  # keep implementation details out of API output
                return self._send_error_json(HTTPStatus.BAD_GATEWAY, f"重置卡请求失败：{exc}")
            response: dict[str, Any] = {"ok": True, **result}
            try:
                # Return a fresh report in the same response so the browser
                # never renders an empty dashboard while refreshing after a
                # successful redemption.
                response["report"] = self.dashboard.service.snapshot() or self.dashboard.service.refresh_async()
            except Exception as exc:
                response["report_error"] = str(exc)[:300]
            return self._send(HTTPStatus.OK, _json_bytes(response))
        self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_PUT(self) -> None:  # noqa: N802 - mirror the documented settings API
        self.do_POST()

    def _serve_static(self, filename: str, content_type: str) -> None:
        target = (WEB_ROOT / filename).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            return self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
        try:
            body = target.read_bytes()
        except OSError:
            return self._send_error_json(HTTPStatus.NOT_FOUND, "asset not found")
        # Versioned CSS/JS (and the tiny favicon) are immutable for the life of
        # a page.  Caching them avoids a burst of duplicate requests on every
        # dashboard reload while HTML remains session-safe and uncached.
        cache_header = "no-store"
        if filename.endswith((".css", ".js", ".svg")):
            cache_header = "public, max-age=86400, stale-while-revalidate=300"
        headers = {"Cache-Control": cache_header}
        if filename.endswith((".css", ".js")) and "gzip" in self.headers.get("Accept-Encoding", "").lower():
            compressed = gzip.compress(body, compresslevel=6, mtime=0)
            if len(compressed) < len(body):
                body = compressed
                headers["Content-Encoding"] = "gzip"
                headers["Vary"] = "Accept-Encoding"
        self._send(HTTPStatus.OK, body, content_type, headers=headers)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the terminal readable.  Errors are still represented in the UI.
        if self.command == "GET" and self.path.startswith("/api/quotas"):
            return
        super().log_message(fmt, *args)


class QuotaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        address: tuple[str, int],
        service: QuotaService,
        imports: ImportJobManager,
        auth: WebAuth | None = None,
        *,
        inherited_fd: int | None = None,
    ):
        if inherited_fd is None:
            super().__init__(address, _DashboardHandler)
        else:
            # systemd socket activation keeps port 1000 open while this worker
            # is restarted, eliminating the short stop/start window that a
            # reverse proxy would otherwise report as 502.
            super().__init__(address, _DashboardHandler, bind_and_activate=False)
            previous = self.socket
            try:
                self.socket = socket.fromfd(inherited_fd, socket.AF_INET, socket.SOCK_STREAM)
                self.server_address = self.socket.getsockname()
            finally:
                previous.close()
                try:
                    os.close(inherited_fd)
                except OSError:
                    pass
            self.server_activate()
        self.service = service
        self.imports = imports
        self.auth = auth or WebAuth()


def _systemd_listener_fd() -> int | None:
    """Return the first LISTEN_FDS descriptor when launched by systemd."""
    try:
        pid = int(os.environ.get("LISTEN_PID", "0"))
        count = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return None
    if pid != os.getpid() or count < 1:
        return None
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地 Codex 配额仪表盘")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=1000, help="监听端口，默认 1000")
    parser.add_argument("--config", type=Path, default=None, help="配额配置 JSON 路径")
    parser.add_argument("--once", action="store_true", help="只刷新一次并输出 JSON，不启动网页")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except QuotaError as exc:
        raise SystemExit(str(exc)) from exc
    service = QuotaService(QuotaClient(config))
    auth = WebAuth()
    imports = ImportJobManager(
        data_dir=Path(__file__).resolve().parents[2] / "data",
        output_dir=Path(__file__).resolve().parents[2] / "data",
        sms_settings_path=Path(__file__).resolve().parents[2] / "data" / "smsbower.json",
        concurrency=config.import_concurrency,
        skip_phone_required=config.skip_phone_required,
    )
    if args.once:
        print(json.dumps(service.report(force=True), ensure_ascii=False, indent=2))
        return

    # Create the mutating-action key before the browser opens.  The key itself
    # is never printed or sent over HTTP; the operator can read the 600-mode
    # file when they intentionally enable a reset-card action.
    try:
        service.client.action_key()
    except QuotaError as exc:
        raise SystemExit(str(exc)) from exc

    inherited_fd = _systemd_listener_fd()
    if inherited_fd is not None:
        try:
            server = QuotaHTTPServer(
                (args.host, args.port), service, imports, auth,
                inherited_fd=inherited_fd,
            )
        except OSError as exc:
            raise SystemExit(f"接管 systemd 监听端口失败：{exc}") from exc
    else:
        server = None
        bind_error: OSError | None = None
        # Never silently move the dashboard to another port: a front proxy or
        # a bookmarked URL would keep calling 1000 and surface a confusing
        # 502.  A short same-port retry covers a manual stop/start hand-off.
        for attempt in range(20):
            try:
                server = QuotaHTTPServer((args.host, args.port), service, imports, auth)
                break
            except OSError as exc:
                bind_error = exc
                if exc.errno != errno.EADDRINUSE or attempt == 19:
                    break
                time.sleep(0.25)
        if server is None:
            raise SystemExit(f"端口 {args.port} 无法监听：{bind_error}")
    bound_port = int(server.server_address[1])
    bind_url = f"http://{args.host}:{bound_port}/"
    if args.host == "0.0.0.0":
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            local_ip = "服务器IP"
        browser_url = f"http://127.0.0.1:{bound_port}/"
        print(f"Codex 配额仪表盘已启动，监听：{bind_url}")
        print(f"远程访问：http://{local_ip}:{bound_port}/（或替换为服务器公网 IP）")
    else:
        browser_url = bind_url
        print(f"Codex 配额仪表盘已启动：{bind_url}")
    if config.mode == "local":
        print("数据模式：local；直接读取本地凭证文件，不依赖 CPA 管理端口。")
        print(f"重置卡操作密钥文件：{config.action_key_path}")
    else:
        print(f"数据模式：{config.mode}（兼容模式）。")
    print("按 Ctrl+C 停止。")

    # Warm the quota cache after the listener is ready.  The first browser/API
    # request must remain independent of the upstream sweep; ``refresh_async``
    # only queues the work and returns a snapshot immediately.
    def warm_quota_cache() -> None:
        try:
            service.refresh_async()
        except Exception:
            # The dashboard can still serve the persisted/empty snapshot and
            # expose the error through /api/status and the UI notice.
            return

    warm_timer = threading.Timer(0.25, warm_quota_cache)
    warm_timer.daemon = True
    warm_timer.start()
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(browser_url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止配额仪表盘…")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
