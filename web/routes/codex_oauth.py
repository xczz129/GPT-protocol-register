# -*- coding: utf-8 -*-
# Codex OAuth RT issuance: batch protocol login with SMS phone verification.
#
# Bridges the standalone codex-oauth-project into the panel so selected
# accounts can mint a real refresh_token (offline_access scope) through the
# Codex PKCE client, with SMSBower / Firefox.fun SMS providers for the
# add-phone step when OpenAI requires it.

from __future__ import annotations

import asyncio
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_CODEX_SRC = Path("/root/codex-oauth-project/src")
if _CODEX_SRC.is_dir() and str(_CODEX_SRC) not in sys.path:
    sys.path.insert(0, str(_CODEX_SRC))

from codex_oauth.oauth import (  # noqa: E402
    ProtocolClient,
    exchange_code,
    totp_from_secret,
)
from codex_oauth.sms import (  # noqa: E402
    FirefoxSmsClient,
    SmsBowerClient,
    SmsSettings,
    check_sms_balance,
)

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _state() -> Any:
    import web_app

    return web_app


# ---------------------------------------------------------------------------
# Batch state (single active run; panel usage is operator-driven)

_LOCK = threading.Lock()
_STOP = threading.Event()
_STATE: dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "success": 0,
    "failed": 0,
    "logs": [],
    "results": [],
    "started_at": "",
    "finished_at": "",
}
_MAX_LOGS = 800


def _log(message: str, level: str = "info") -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with _LOCK:
        _STATE["logs"].append({"t": stamp, "level": level, "m": str(message)[:300]})
        overflow = len(_STATE["logs"]) - _MAX_LOGS
        if overflow > 0:
            del _STATE["logs"][:overflow]


def _phone_requirement(data: Any) -> tuple:
    # Detect the add-phone requirement from an MFA response.
    if not isinstance(data, dict):
        return None, ""
    step = ""
    auth_step = data.get("auth_step")
    if isinstance(auth_step, dict):
        step = str(auth_step.get("step") or auth_step.get("id") or "")
    step = step or str(data.get("step") or data.get("next") or "")
    lowered = str(data.get("error") or "").lower()
    if "add-phone" in step or "phone_verification" in step:
        return True, step
    if "add-phone" in lowered:
        return True, str(data.get("error"))
    if step and "phone" in step.lower():
        return True, step
    if "workspace" in step or "consent" in step:
        return False, step
    return None, step


def _sms_client(settings: SmsSettings):
    if not settings.enabled or not settings.credentials_configured:
        return None
    if settings.provider == "firefox":
        return FirefoxSmsClient(settings, timeout=30.0)
    return SmsBowerClient(settings, timeout=30.0)


def _auto_phone(client, sms, log) -> None:
    attempts_max = int(sms.settings.max_phone_attempts or 5)
    last_error = ""
    for attempt in range(1, attempts_max + 1):
        if _STOP.is_set():
            raise RuntimeError("任务已停止")
        log(f"接码获取号码(第 {attempt} 次)")
        activation = sms.acquire()
        try:
            log(f"已分配 {activation.phone}(国家 {activation.country})")
            client.send_phone(activation.phone)
            log("手机号已提交,等待短信验证码")
            code = sms.wait_code(activation, cancel_event=_STOP)
            log("收到验证码,提交验证")
            client.submit_phone_otp(code)
            sms.finish(activation)
            log("手机验证通过")
            return
        except Exception as exc:
            last_error = str(exc)
            try:
                sms.cancel(activation)
            except Exception:
                pass
            log(f"号码 {activation.phone} 失败: {last_error}", "warning")
    raise RuntimeError(f"接码 {attempts_max} 次均失败: {last_error[:160]}")


def _process_one(rec: dict, sms_cfg: dict) -> dict:
    email = str(rec.get("email") or "").strip()
    password = str(rec.get("password") or "")
    totp = str(rec.get("totp_secret") or "")

    def log(message: str, level: str = "info") -> None:
        _log(f"{email} | {message}", level)

    if not password or not totp:
        raise RuntimeError("账号缺少密码或 2FA 密钥,无法补发")

    settings = SmsSettings.from_mapping(sms_cfg)
    sms = _sms_client(settings)

    client = ProtocolClient(email, timeout=30)
    try:
        log("初始化会话与 PKCE")
        client.bootstrap()
        client.submit_email()
        log("邮箱已提交")
        client.verify_password(password)
        log("密码验证通过")
        mfa_id = client.mfa_id()
        client.issue_mfa_challenge(mfa_id)
        mfa_response = client.verify_mfa(mfa_id, totp_from_secret(totp))
        log("2FA 验证通过")
        try:
            mfa_data = mfa_response.json()
        except Exception:
            mfa_data = {}
        if not isinstance(mfa_data, dict):
            mfa_data = {}
        phone_required, reason = _phone_requirement(mfa_data)
        if phone_required is True:
            log(f"服务端要求手机验证({reason or 'add-phone'})")
            if sms is None:
                raise RuntimeError("需要手机验证,但接码未启用或未配置")
            _auto_phone(client, sms, log)
        else:
            log("无需手机验证,直接进入 workspace")
        try:
            callback = client.select_workspace()
        except Exception as exc:
            if sms is not None and "phone" in str(exc).lower():
                log("workspace 返回手机号步骤,切换到接码流程")
                _auto_phone(client, sms, log)
                callback = client.select_workspace()
            else:
                raise
        query = parse_qs(urlparse(callback).query)
        code = query.get("code", [""])[0]
        if not code:
            raise RuntimeError("回调缺少授权 code")
        log("交换 OAuth Token")
        token = exchange_code(code, client.verifier)
        refresh = str(token.get("refresh_token") or "")
        if not refresh:
            raise RuntimeError("响应缺少 refresh_token(scope 未含 offline_access?)")
        updates = {
            **rec,
            "refresh_token": refresh,
            "id_token": str(token.get("id_token") or ""),
            "access_token": str(token.get("access_token") or ""),
            "oauth_client_id": CODEX_CLIENT_ID,
            "last_token_refresh_at": _state()._now_iso(),
            "last_token_refresh_error": "",
        }
        _state().upsert_account_record(updates, _state().ACCOUNTS_FILE)
        log("RT 已写回账号库", "ok")
        return {
            "email": email,
            "status": "success",
            "refresh_token": refresh,
            "expires_in": token.get("expires_in"),
            "error": "",
        }
    finally:
        try:
            client.session.close()
        except Exception:
            pass


def _run_batch(records: list, sms_cfg: dict) -> None:
    try:
        for rec in records:
            if _STOP.is_set():
                _log("任务已停止(剩余账号跳过)", "warning")
                break
            try:
                result = _process_one(rec, sms_cfg)
                with _LOCK:
                    _STATE["success"] += 1
                    _STATE["results"].append(result)
            except Exception as exc:
                with _LOCK:
                    _STATE["failed"] += 1
                    _STATE["results"].append(
                        {
                            "email": rec.get("email"),
                            "status": "failed",
                            "refresh_token": "",
                            "expires_in": None,
                            "error": str(exc)[:260],
                        }
                    )
                _log(f"{rec.get('email')} | 失败: {exc}", "error")
            finally:
                with _LOCK:
                    _STATE["done"] += 1
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SMS provider configuration stored under config.yaml ``sms_code``


class SmsConfigUpdate(BaseModel):
    enabled: bool = False
    provider: str = Field(default="smsbower", max_length=20)
    api_key: str = Field(default="", max_length=200)
    service: str = Field(default="dr", max_length=30)
    country_order: str = Field(default="6, 4, 151", max_length=120)
    poll_interval_ms: int = Field(default=5000, ge=1000, le=60000)
    timeout_seconds: int = Field(default=180, ge=30, le=1800)
    max_price: str = Field(default="", max_length=20)
    max_phone_attempts: int = Field(default=5, ge=1, le=20)
    firefox_api_name: str = Field(default="", max_length=120)
    firefox_api_password: str = Field(default="", max_length=120)
    firefox_api_token: str = Field(default="", max_length=200)
    firefox_docking_code: str = Field(default="", max_length=120)
    firefox_did: str = Field(default="", max_length=60)
    firefox_item_id: str = Field(default="1096", max_length=20)
    firefox_country_order: str = Field(default="usa, chl, eng", max_length=160)


def _sms_config_section(cfg: dict) -> dict:
    raw = cfg.get("sms_code") if isinstance(cfg.get("sms_code"), dict) else {}
    provider = str(raw.get("provider") or "smsbower").strip().lower()
    if provider in {"firefox.fun", "firefox_sms", "huohuli", "火狐狸"}:
        provider = "firefox"
    if provider not in {"smsbower", "firefox"}:
        provider = "smsbower"
    countries = raw.get("country_order", "6, 4, 151")
    if isinstance(countries, (list, tuple)):
        countries = ", ".join(str(c) for c in countries)
    ff_countries = raw.get("firefox_country_order", "usa, chl, eng")
    if isinstance(ff_countries, (list, tuple)):
        ff_countries = ", ".join(str(c) for c in ff_countries)

    def _int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(raw.get("enabled")),
        "provider": provider,
        "api_key": str(raw.get("api_key") or ""),
        "service": str(raw.get("service") or "dr"),
        "country_order": str(countries),
        "poll_interval_ms": _int(raw.get("poll_interval_ms"), 5000),
        "timeout_seconds": _int(raw.get("timeout_seconds"), 180),
        "max_price": str(raw.get("max_price") or ""),
        "max_phone_attempts": _int(raw.get("max_phone_attempts"), 5),
        "firefox_api_name": str(raw.get("firefox_api_name") or ""),
        "firefox_api_password": str(raw.get("firefox_api_password") or ""),
        "firefox_api_token": str(raw.get("firefox_api_token") or ""),
        "firefox_docking_code": str(raw.get("firefox_docking_code") or ""),
        "firefox_did": str(raw.get("firefox_did") or ""),
        "firefox_item_id": str(raw.get("firefox_item_id") or "1096"),
        "firefox_country_order": str(ff_countries),
    }


@router.get("/api/sms-config")
async def sms_config_get():
    def _do():
        cfg = _state()._read_config()
        section = _sms_config_section(cfg)
        settings = SmsSettings.from_mapping(section)
        public = dict(section)
        public["api_key"] = ""
        public["firefox_api_password"] = ""
        public["firefox_api_token"] = ""
        public["has_api_key"] = bool(settings.api_key)
        public["api_key_masked"] = _state()._mask_secret(settings.api_key) if settings.api_key else ""
        public["has_firefox_token"] = bool(settings.firefox_api_token)
        public["firefox_api_token_masked"] = (
            _state()._mask_secret(settings.firefox_api_token) if settings.firefox_api_token else ""
        )
        public["provider_label"] = settings.provider_label
        public["configured"] = settings.credentials_configured
        return public

    return await asyncio.to_thread(_do)


@router.put("/api/sms-config")
async def sms_config_update(body: SmsConfigUpdate):
    def _do():
        with _state()._edit_config() as cfg:
            current = cfg.setdefault("sms_code", {})
            api_key = str(body.api_key or "").strip()
            firefox_password = str(body.firefox_api_password or "").strip()
            firefox_token = str(body.firefox_api_token or "").strip()
            if api_key:
                current["api_key"] = api_key
            if firefox_password:
                current["firefox_api_password"] = firefox_password
            if firefox_token:
                current["firefox_api_token"] = firefox_token
            current.update(
                {
                    "enabled": bool(body.enabled),
                    "provider": str(body.provider or "smsbower"),
                    "service": str(body.service or "dr"),
                    "country_order": str(body.country_order or "6, 4, 151"),
                    "poll_interval_ms": body.poll_interval_ms,
                    "timeout_seconds": body.timeout_seconds,
                    "max_price": str(body.max_price or ""),
                    "max_phone_attempts": body.max_phone_attempts,
                    "firefox_api_name": str(body.firefox_api_name or ""),
                    "firefox_docking_code": str(body.firefox_docking_code or ""),
                    "firefox_did": str(body.firefox_did or ""),
                    "firefox_item_id": str(body.firefox_item_id or "1096"),
                    "firefox_country_order": str(body.firefox_country_order or "usa, chl, eng"),
                }
            )
        return {"ok": True}

    return await asyncio.to_thread(_do)


@router.post("/api/sms-config/test")
async def sms_config_test():
    def _do():
        cfg = _state()._read_config()
        section = _sms_config_section(cfg)
        settings = SmsSettings.from_mapping(section)
        try:
            balance = check_sms_balance(settings)
            return {
                "ok": True,
                "message": f"{settings.provider_label} 余额: {balance.get('balance', '?')}",
                "balance": balance,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)[:200]}

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Batch run endpoints



def _parse_credential_lines(text: str) -> list:
    creds: list = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----", 2)
        if len(parts) == 3 and all(part.strip() for part in parts):
            creds.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return creds


def _login_for_token(email: str, password: str, totp: str, sms, log):
    # Shared protocol login chain for imports: bootstrap -> email ->
    # password -> TOTP -> (phone?) -> workspace -> PKCE token exchange.
    client = ProtocolClient(email, timeout=30)
    try:
        log("初始化会话与 PKCE")
        client.bootstrap()
        client.submit_email()
        log("邮箱已提交")
        client.verify_password(password)
        log("密码验证通过")
        mfa_id = client.mfa_id()
        client.issue_mfa_challenge(mfa_id)
        mfa_response = client.verify_mfa(mfa_id, totp_from_secret(totp))
        log("2FA 验证通过")
        try:
            mfa_data = mfa_response.json()
        except Exception:
            mfa_data = {}
        if not isinstance(mfa_data, dict):
            mfa_data = {}
        phone_required, reason = _phone_requirement(mfa_data)
        if phone_required is True:
            log(f"服务端要求手机验证({reason or 'add-phone'})")
            if sms is None:
                raise RuntimeError("需要手机验证,但接码未启用或未配置")
            _auto_phone(client, sms, log)
        else:
            log("无需手机验证,直接进入 workspace")
        try:
            callback = client.select_workspace()
        except Exception as exc:
            if sms is not None and "phone" in str(exc).lower():
                log("workspace 返回手机号步骤,切换到接码流程")
                _auto_phone(client, sms, log)
                callback = client.select_workspace()
            else:
                raise
        query = parse_qs(urlparse(callback).query)
        code = query.get("code", [""])[0]
        if not code:
            raise RuntimeError("回调缺少授权 code")
        log("交换 OAuth Token")
        token = exchange_code(code, client.verifier)
        return client, token
    except Exception:
        try:
            client.session.close()
        except Exception:
            pass
        raise


def _import_one(email: str, password: str, totp: str, sms_cfg: dict) -> dict:
    def log(message: str, level: str = "info") -> None:
        _log(f"{email} | {message}", level)

    settings = SmsSettings.from_mapping(sms_cfg)
    sms = _sms_client(settings)
    client, token = _login_for_token(email, password, totp, sms, log)
    try:
        refresh = str(token.get("refresh_token") or "")
        if not refresh:
            raise RuntimeError("响应缺少 refresh_token(scope 未含 offline_access?)")
        claims = decode_jwt_payload(str(token.get("id_token") or token.get("access_token") or "")) or {}
        auth = claims.get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, dict) else {}
        now = _state()._now_iso()
        record = {
            "email": email,
            "password": password,
            "password_set": True,
            "totp_secret": totp,
            "mfa_enabled": True,
            "mfa_factor_type": "totp",
            "access_token": str(token.get("access_token") or ""),
            "refresh_token": refresh,
            "id_token": str(token.get("id_token") or ""),
            "oauth_client_id": CODEX_CLIENT_ID,
            "account_id": str(auth.get("chatgpt_account_id") or claims.get("sub") or ""),
            "plan_type": str(auth.get("chatgpt_plan_type") or "free"),
            "registration_method": "codex_oauth",
            "source_type": "import",
            "browser_type": "curl_cffi/chrome",
            "device_id": client.device_id,
            "health_status": "unknown",
            "registered_at": now,
            "updated_at": now,
            "last_token_refresh_at": now,
            "last_token_refresh_error": "",
        }
        _state().upsert_account_record(record, _state().ACCOUNTS_FILE)
        log("账号已写入账号库(AT/RT/密码/2FA)", "ok")
        return {
            "email": email,
            "status": "success",
            "refresh_token": refresh,
            "expires_in": token.get("expires_in"),
            "error": "",
        }
    finally:
        try:
            client.session.close()
        except Exception:
            pass


def _run_import_batch(creds: list, sms_cfg: dict) -> None:
    try:
        for email, password, totp in creds:
            if _STOP.is_set():
                _log("任务已停止(剩余账号跳过)", "warning")
                break
            try:
                result = _import_one(email, password, totp, sms_cfg)
                with _LOCK:
                    _STATE["success"] += 1
                    _STATE["results"].append(result)
            except Exception as exc:
                with _LOCK:
                    _STATE["failed"] += 1
                    _STATE["results"].append(
                        {"email": email, "status": "failed", "refresh_token": "", "expires_in": None, "error": str(exc)[:260]}
                    )
                _log(f"{email} | 失败: {exc}", "error")
            finally:
                with _LOCK:
                    _STATE["done"] += 1
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")


class CodexOauthImportRequest(BaseModel):
    text: str = Field(default="", max_length=200000)


@router.post("/api/codex-oauth/import")
async def codex_oauth_import(body: CodexOauthImportRequest):
    def _do():
        with _LOCK:
            if _STATE["running"]:
                raise HTTPException(409, "已有接码/导入任务在运行")
        creds = _parse_credential_lines(body.text)
        if not creds:
            raise HTTPException(400, "未识别到有效账号行(格式:邮箱----密码----2FA)")
        cfg = _state()._read_config()
        sms_cfg = _sms_config_section(cfg)
        with _LOCK:
            _STATE.update(
                {
                    "running": True,
                    "total": len(creds),
                    "done": 0,
                    "success": 0,
                    "failed": 0,
                    "logs": [],
                    "results": [],
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "finished_at": "",
                }
            )
        _STOP.clear()
        threading.Thread(
            target=_run_import_batch,
            args=(creds, sms_cfg),
            name="codex-oauth-import",
            daemon=True,
        ).start()
        _log(f"导入任务启动:{len(creds)} 个账号 · 接码渠道 {SmsSettings.from_mapping(sms_cfg).provider_label}")
        return {"ok": True, "total": len(creds)}

    return await asyncio.to_thread(_do)


class CodexOauthRunRequest(BaseModel):
    emails: list = Field(default_factory=list, max_length=200)


@router.post("/api/codex-oauth/run")
async def codex_oauth_run(body: CodexOauthRunRequest):
    def _do():
        with _LOCK:
            if _STATE["running"]:
                raise HTTPException(409, "已有接码任务在运行")
        wanted = {str(e).strip().lower() for e in body.emails if str(e).strip()}
        if not wanted:
            raise HTTPException(400, "未选择账号")
        records = _state().load_accounts(_state().ACCOUNTS_FILE)
        picked = [
            dict(rec)
            for rec in records
            if str(rec.get("email") or "").strip().lower() in wanted
        ]
        if not picked:
            raise HTTPException(404, "选中账号在库中不存在")
        cfg = _state()._read_config()
        sms_cfg = _sms_config_section(cfg)
        settings = SmsSettings.from_mapping(sms_cfg)
        if not settings.enabled or not settings.credentials_configured:
            raise HTTPException(400, f"接码未启用或未配置凭证({settings.provider_label})")
        with _LOCK:
            _STATE.update(
                {
                    "running": True,
                    "total": len(picked),
                    "done": 0,
                    "success": 0,
                    "failed": 0,
                    "logs": [],
                    "results": [],
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "finished_at": "",
                }
            )
        _STOP.clear()
        threading.Thread(
            target=_run_batch,
            args=(picked, sms_cfg),
            name="codex-oauth-batch",
            daemon=True,
        ).start()
        _log(f"任务启动:{len(picked)} 个账号 · 接码渠道 {settings.provider_label}")
        return {"ok": True, "total": len(picked)}

    return await asyncio.to_thread(_do)


@router.get("/api/codex-oauth/status")
async def codex_oauth_status():
    with _LOCK:
        payload = {
            key: (list(value) if key in ("logs", "results") else value)
            for key, value in _STATE.items()
        }
    return {"ok": True, **payload}


@router.post("/api/codex-oauth/stop")
async def codex_oauth_stop():
    _STOP.set()
    _log("已请求停止,当前账号完成后中止", "warning")
    return {"ok": True}
