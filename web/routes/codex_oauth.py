# -*- coding: utf-8 -*-
# Codex OAuth RT issuance: batch protocol login with SMS phone verification.
#
# Bridges the standalone codex-oauth-project into the panel so selected
# accounts can mint a real refresh_token (offline_access scope) through the
# Codex PKCE client, with SMSBower / Firefox.fun SMS providers for the
# add-phone step when OpenAI requires it.

from __future__ import annotations

import asyncio
import json
import re
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
    SmsActivation,
    SmsBowerClient,
    SmsError,
    SmsSettings,
    check_sms_balance,
    normalize_firefox_country,
    parse_firefox_docking_code,
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
    # mode 区分两类共享此状态的批任务：codex=接码(短信验证), import=导入(协议登录)
    "mode": "codex",
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


# ---------------------------------------------------------------------------
# 接码任务不做断点续跑：一个账号只能绑定一次手机号，中断后剩余账号
# 由操作者手动重新发起（选中账号 → 接码），进度仅在内存中展示。


_RT_HISTORY_PATH = Path("/root/full/gpt-register-fp/data/codex_rt_history.json")
_RT_HISTORY_LOCK = threading.Lock()


def _clip_history_logs(logs: list) -> list:
    """Normalize and cap per-account flow logs persisted into RT history."""
    rows = [
        {
            "t": str(item.get("t") or ""),
            "level": str(item.get("level") or "info"),
            "m": str(item.get("m") or "")[:300],
        }
        for item in (logs or [])
        if isinstance(item, dict)
    ]
    return rows[-200:]


def _append_rt_history(email: str, entry: dict) -> None:
    """Persist the latest RT-reissue outcome per account (newest first)."""
    email = str(email or "").strip().lower()
    if not email:
        return
    with _RT_HISTORY_LOCK:
        data = {}
        try:
            if _RT_HISTORY_PATH.is_file():
                data = json.loads(_RT_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        rows = [row for row in (data.get(email) or []) if isinstance(row, dict)]
        rows.insert(0, entry)
        data[email] = rows[:10]
        try:
            _RT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _RT_HISTORY_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            pass


def _phone_requirement(data: Any) -> tuple:
    # Detect the add-phone requirement from an MFA response.
    if not isinstance(data, dict):
        return None, ""
    step = ""
    auth_step = data.get("auth_step")
    if isinstance(auth_step, dict):
        step = str(auth_step.get("step") or auth_step.get("id") or "")
    step = step or str(data.get("step") or data.get("next") or "")
    # MFA verify responses signal the next page via continue_url + page.type,
    # e.g. continue_url=https://auth.openai.com/add-phone, page.type=add_phone.
    continue_url = str(data.get("continue_url") or "")
    page = data.get("page")
    page_type = str(page.get("type") or "") if isinstance(page, dict) else ""
    lowered = str(data.get("error") or "").lower()
    if "add-phone" in step or "phone_verification" in step:
        return True, step
    if "add-phone" in continue_url.lower():
        return True, continue_url
    if "phone" in page_type.lower():
        return True, page_type
    if "add-phone" in lowered:
        return True, str(data.get("error"))
    if step and "phone" in step.lower():
        return True, step
    if "workspace" in step or "consent" in step:
        return False, step
    if "workspace" in page_type.lower() or "consent" in page_type.lower():
        return False, page_type
    return None, step


def _known_sms_country(value: str) -> bool:
    """Whether a numeric id is a real SMSBower country id (not a provider id).

    The panel's country picker emits provider tier ids (``6=1170|3237``).
    Tier ids live in a disjoint numeric range, so the static country tables
    are enough to tell a bare tier id from a country id.
    """
    text = str(value or "").strip()
    if not text.isdigit():
        return False
    cid = int(text)
    return cid in SMSBOWER_COUNTRY_INFO or cid in SMSBOWER_EXTRA_CALLING


def _country_specs(sms_cfg: dict) -> list:
    """Parse country order entries.

    Supported syntax per entry (entries separated by commas):
      country            -> provider choice left to the market
      country:price      -> numeric price ceiling, e.g. 4:0.2
      country=t1|t2     -> provider tier priority, e.g. 6=1170|3237
    Older UI builds wrote the tier list with commas (``6=1170, 3237``).
    A bare number that is not a known country id is folded back into the
    preceding ``=`` token so those configs keep working.
    Returns (country, tier_ids, max_price) triples.
    """
    provider = str(sms_cfg.get("provider") or "").strip().lower()
    key = "firefox_country_order" if provider == "firefox" else "country_order"
    tokens: list[dict] = []
    for raw in str(sms_cfg.get(key) or "").replace("，", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        if "=" in item:
            country, _, tail = item.partition("=")
            tiers = [p.strip() for p in tail.replace("｜", "|").split("|") if p.strip()]
            tokens.append({"country": country.strip(), "tiers": tiers, "price": ""})
        elif ":" in item:
            country, _, tail = item.partition(":")
            tokens.append({"country": country.strip(), "tiers": [], "price": tail.strip()})
        elif (
            tokens
            and tokens[-1]["tiers"]
            and item.isdigit()
            and not _known_sms_country(item)
        ):
            # Legacy comma tier list: ``6=1170, 3237`` -> tiers [1170, 3237].
            tokens[-1]["tiers"].append(item)
        else:
            tokens.append({"country": item, "tiers": [], "price": ""})
    specs: list = []
    for token in tokens:
        country = token["country"]
        if not country:
            continue
        price = token["price"]
        if price:
            try:
                float(price)
            except ValueError:
                price = ""
        specs.append((country, list(token["tiers"]), price or None))
    if provider != "firefox":
        specs = [
            (c, tiers, price)
            for c, tiers, price in specs
            if str(c).isdigit() and all(str(t).isdigit() for t in tiers)
        ]
    return specs


def _provider_specs(sms_cfg: dict) -> dict:
    """Per-country provider id selections: {"73": "3237"}."""
    raw = str(sms_cfg.get("provider_map") or "").strip()
    out: dict = {}
    for pair in raw.replace("，", ",").split(","):
        pair = pair.strip()
        if not pair:
            continue
        country, _, pid = pair.partition("=")
        country = country.strip()
        pid = pid.strip()
        if country and pid:
            # Multiple provider ids per country are allowed; the comma list
            # keeps the operator's priority order for providerIds.
            out[country] = pid
    return out


class _PanelSmsBowerClient(SmsBowerClient):
    """SMSBower client with per-country provider tiers and maxPrice support."""

    @staticmethod
    def _normalize_specs(country_specs: list) -> list:
        specs: list = []
        for spec in country_specs or []:
            if isinstance(spec, tuple) and len(spec) == 3:
                country, tiers, price = spec
                specs.append((str(country), list(tiers or []), str(price or "").strip()))
            elif isinstance(spec, tuple) and len(spec) == 2:
                country, price = spec
                specs.append((str(country), [], str(price or "").strip()))
        return specs

    def __init__(self, settings: SmsSettings, country_specs: list, *, timeout: float = 30.0):
        super().__init__(settings, timeout=timeout)
        self.country_specs = self._normalize_specs(country_specs) or [
            (str(c), [], "") for c in settings.country_order
        ]

    def acquire(self) -> SmsActivation:
        last_error = "没有可用号码"
        for country, tiers, max_price in self.country_specs:
            # 档位按操作者选择顺序依次尝试:先选的先试。
            attempts = [str(tier).strip() for tier in tiers if str(tier).strip()] or [""]
            for tier in attempts:
                params = {
                    "service": self.settings.service,
                    "country": country,
                }
                if tier:
                    params["providerIds"] = tier
                price = max_price or self.settings.max_price
                if price:
                    params["maxPrice"] = price
                try:
                    body = self._request("getNumber", **params)
                except SmsError as exc:
                    last_error = f"{exc} (country={country} tier={tier or 'any'})"
                    continue
                if not body.startswith("ACCESS_NUMBER:"):
                    last_error = (
                        f"SMSBower 未分配号码: {body[:120]} "
                        f"(country={country} tier={tier or 'any'})"
                    )
                    continue
                parts = body.split(":", 2)
                if len(parts) != 3 or not parts[1] or not parts[2]:
                    last_error = f"SMSBower 响应格式异常: {body[:120]}"
                    continue
                return SmsActivation(parts[1], parts[2], int(country), self.settings.api_key)
        raise SmsError(last_error)


class _PanelFirefoxSmsClient(FirefoxSmsClient):
    """Firefox client honouring per-country maxPrice specs."""

    def __init__(self, settings: SmsSettings, country_specs: list, *, timeout: float = 30.0):
        super().__init__(settings, timeout=timeout)
        specs: list = []
        for spec in country_specs or []:
            # Spec triples carry (country, tiers, max_price); tiers are a
            # SMSBower-only concept and are ignored here.
            if isinstance(spec, tuple) and len(spec) == 3:
                country, _tiers, price = spec
                specs.append((country, str(price or "").strip()))
            else:
                specs.append((spec[0], str(spec[1] or "").strip()))
        self.country_specs = specs or [(str(c), "") for c in settings.firefox_country_order]

    def acquire(self) -> SmsActivation:
        last_error = "没有可用号码"
        dock_country, dock_item_id, dock_did = parse_firefox_docking_code(self.settings.firefox_docking_code)
        if dock_country and dock_item_id and dock_did:
            specs = [(dock_country, None)]
            item_id = dock_item_id
            did = dock_did
        else:
            specs = self.country_specs
            item_id = self.settings.firefox_item_id
            did = self.settings.firefox_did
        self._login()
        for country, max_price in specs:
            country = normalize_firefox_country(country)
            params = {
                "iid": item_id,
                "country": country,
                "dock": "0",
                "otpmode": "sms",
            }
            if did:
                params["did"] = did
            price = max_price or self.settings.max_price
            if price:
                params["maxPrice"] = price
            try:
                body = self._authorized_request("getPhone", **params)
            except SmsError as exc:
                last_error = str(exc)
                continue
            if not body.startswith("1|"):
                last_error = f"火狐狸未分配号码：{self._failure(body)[:120]}"
                continue
            parts = body.split("|")
            if len(parts) < 8 or not parts[1].strip() or not parts[7].strip():
                last_error = f"火狐狸响应格式异常：{body[:160]}"
                continue
            returned_country = normalize_firefox_country(parts[3] if len(parts) > 3 else "") or country
            returned_prefix = parts[4] if len(parts) > 4 else ""
            phone = self._normalize_phone(parts[7], returned_prefix, returned_country)
            return SmsActivation(
                activation_id=parts[1].strip(),
                phone=phone,
                country=returned_country,
                api_key=self._token,
            )
        raise SmsError(last_error)


def _sms_client(settings: SmsSettings, country_specs: list | None = None):
    if not settings.enabled or not settings.credentials_configured:
        return None
    specs = list(country_specs or [])
    if settings.provider == "firefox":
        return _PanelFirefoxSmsClient(settings, specs, timeout=30.0)
    return _PanelSmsBowerClient(settings, specs, timeout=30.0)


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
            return activation.phone
        except Exception as exc:
            last_error = str(exc)
            try:
                sms.cancel(activation)
            except Exception:
                pass
            log(f"号码 {activation.phone} 失败: {last_error}", "warning")
    raise RuntimeError(f"接码 {attempts_max} 次均失败: {last_error[:160]}")


def _process_one(rec: dict, sms_cfg: dict, steps: list | None = None) -> dict:
    email = str(rec.get("email") or "").strip()
    password = str(rec.get("password") or "")
    totp = str(rec.get("totp_secret") or "")

    def log(message: str, level: str = "info") -> None:
        _log(f"{email} | {message}", level)
        if steps is not None:
            steps.append(
                {
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "level": level,
                    "m": str(message)[:300],
                }
            )

    if not password or not totp:
        raise RuntimeError("账号缺少密码或 2FA 密钥,无法补发")

    settings = SmsSettings.from_mapping(sms_cfg)
    provider_map = _provider_specs(sms_cfg)
    specs = []
    for country, tier_ids, max_price in _country_specs(sms_cfg):
        extra_ids = [
            pid.strip()
            for pid in str(provider_map.get(str(country)) or "").split(",")
            if pid.strip()
        ]
        merged = [str(t) for t in tier_ids]
        for pid in extra_ids:
            if pid not in merged:
                merged.append(pid)
        specs.append((country, merged, max_price))
    sms = _sms_client(settings, specs)

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
        phone_used = ""
        # Team 账号按导入时指定的工作区名选择;个人账号为空,默认第一个。
        workspace_name = str(rec.get("workspace_name") or "").strip()
        if workspace_name:
            log(f"使用工作区「{workspace_name}」")
        phone_required, reason = _phone_requirement(mfa_data)
        if phone_required is True:
            log(f"服务端要求手机验证({reason or 'add-phone'})")
            if sms is None:
                raise RuntimeError("需要手机验证,但接码未启用或未配置")
            phone_used = _auto_phone(client, sms, log) or ""
        else:
            log("无需手机验证,直接进入 workspace")
        try:
            callback = client.select_workspace(workspace_name)
        except Exception as exc:
            if sms is not None and "phone" in str(exc).lower():
                log("workspace 返回手机号步骤,切换到接码流程")
                _auto_phone(client, sms, log)
                callback = client.select_workspace(workspace_name)
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
        _state().upsert_account_record(updates, _state().ACCOUNTS_FILE, create=False)
        log("RT 已写回账号库", "ok")
        return {
            "email": email,
            "status": "success",
            "refresh_token": refresh,
            "expires_in": token.get("expires_in"),
            "error": "",
            "phone": phone_used,
            "logs": list(steps or []),
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
            steps: list = []
            try:
                result = _process_one(rec, sms_cfg, steps)
                with _LOCK:
                    _STATE["success"] += 1
                    _STATE["results"].append(result)
                _append_rt_history(
                    result.get("email") or rec.get("email"),
                    {
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "status": "success",
                        "rt": str(result.get("refresh_token") or "")[:600],
                        "phone": str(result.get("phone") or ""),
                        "error": "",
                        "logs": _clip_history_logs(result.get("logs") or steps),
                    },
                )
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
                _append_rt_history(
                    rec.get("email"),
                    {
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "status": "failed",
                        "rt": "",
                        "phone": "",
                        "error": str(exc)[:300],
                        "logs": _clip_history_logs(steps),
                    },
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
        "firefox_docking_code_history": [
            str(item).strip()
            for item in raw.get("firefox_docking_code_history") or []
            if str(item).strip()
        ],
    }

# SMSBower country id -> (english name, chinese name, calling code).
# The id space follows the sms-activate family shared by SMSBower; calling
# codes let the picker show the +44-style prefix before a number is bought.
SMSBOWER_COUNTRY_INFO: dict[int, tuple[str, str, str]] = {
     0: ("Russian Federation", "俄罗斯联邦", "+7"),     1: ("Ukraine", "乌克兰", "+380"),     2: ("Kazakhstan", "哈萨克斯坦", "+7"),
     3: ("China", "中国", "+86"),     4: ("Philippines", "菲律宾", "+63"),     5: ("Myanmar", "缅甸", "+95"),
     6: ("Indonesia", "印度尼西亚", "+62"),     7: ("Malaysia", "马来西亚", "+60"),     8: ("Kenya", "肯尼亚", "+254"),
     9: ("Tanzania", "坦桑尼亚", "+255"),     10: ("Viet nam", "越南", "+84"),     11: ("Kyrgyzstan", "吉尔吉斯斯坦", "+996"),
     12: ("United States (virtual)", "美国（虚拟）", "+1"),     13: ("Israel", "以色列", "+972"),     14: ("Hong Kong", "香港", "+852"),
     15: ("Poland", "波兰", "+48"),     16: ("United Kingdom", "英国", "+44"),     17: ("Madagascar", "马达加斯加", "+261"),
     18: ("Congo (Dem. Republic)", "刚果（民主共和国）", "+243"),     19: ("Nigeria", "尼日利亚", "+234"),     20: ("Macau", "澳门", "+853"),
     21: ("Egypt", "埃及", "+20"),     22: ("India", "印度", "+91"),     23: ("Ireland", "爱尔兰", "+353"),
     24: ("Cambodia", "柬埔寨", "+855"),     25: ("Lao People`s", "老挝人民", "+856"),     26: ("Haiti", "海地", "+509"),
     27: ("Cote d`Ivoire Ivory Coast", "科特迪瓦", "+225"),     28: ("Gambia", "冈比亚", "+220"),     29: ("Serbia", "塞尔维亚", "+381"),
     30: ("Yemen", "也门", "+967"),     31: ("South Africa", "南非", "+27"),     32: ("Romania", "罗马尼亚", "+40"),
     33: ("Colombia", "哥伦比亚", "+57"),     34: ("Estonia", "爱沙尼亚", "+372"),     35: ("Azerbaijan", "阿塞拜疆", "+994"),
     36: ("Canada", "加拿大", "+1"),     37: ("Morocco", "摩洛哥", "+212"),     38: ("Ghana", "加纳", "+233"),
     39: ("Argentinas", "阿根廷", "+54"),     40: ("Uzbekistan", "乌兹别克斯坦", "+998"),     41: ("Cameroon", "喀麦隆", "+237"),
     42: ("Chad", "乍得", "+235"),     43: ("Germany", "德国", "+49"),     44: ("Lithuania", "立陶宛", "+370"),
     45: ("Croatia", "克罗地亚", "+385"),     46: ("Sweden", "瑞典", "+46"),     47: ("Iraq", "伊拉克", "+964"),
     48: ("Netherlands", "荷兰", "+31"),     49: ("Latvia", "拉脱维亚", "+371"),     50: ("Austria", "奥地利", "+43"),
     51: ("Belarus", "白俄罗斯", "+375"),     52: ("Thailand", "泰国", "+66"),     53: ("Saudi Arabia", "沙特阿拉伯", "+966"),
     54: ("Mexico", "墨西哥", "+52"),     55: ("Taiwan", "台湾", "+886"),     56: ("Spain", "西班牙", "+34"),
     57: ("Iran", "伊朗", "+98"),     58: ("Algeria", "阿尔及利亚", "+213"),     59: ("Slovenia", "斯洛文尼亚", "+386"),
     60: ("Bangladesh", "孟加拉国", "+880"),     61: ("Senegal", "塞内加尔", "+221"),     62: ("Turkey", "土耳其", "+90"),
     63: ("Czech Republic", "捷克共和国", "+420"),     64: ("Sri Lanka", "斯里兰卡", "+94"),     65: ("Peru ", "秘鲁", "+51"),
     66: ("Pakistan", "巴基斯坦", "+92"),     67: ("New Zealand", "新西兰", "+64"),     68: ("Guinea", "几内亚", "+224"),
     69: ("Mali", "马里", "+223"),     70: ("Venezuela", "委内瑞拉", "+58"),     71: ("Ethiopia", "埃塞俄比亚", "+251"),
     72: ("Mongolia", "蒙古", "+976"),     73: ("Brazil", "巴西", "+55"),     74: ("Afghanistan", "阿富汗", "+93"),
     75: ("Uganda", "乌干达", "+256"),     76: ("Angola", "安哥拉", "+244"),     77: ("Cyprus", "塞浦路斯", "+357"),
     78: ("France", "法国", "+33"),     79: ("Papua new gvineya", "巴布亚新几内亚", "+675"),     80: ("Mozambique", "莫桑比克", "+258"),
     81: ("Nepal", "尼泊尔", "+977"),     82: ("Belgium", "比利时", "+32"),     83: ("Bulgaria", "保加利亚", "+359"),
     84: ("Hungary", "匈牙利", "+36"),     85: ("Moldova, Republic of", "摩尔多瓦共和国", "+373"),     86: ("Italy", "意大利", "+39"),
     87: ("Paraguay", "巴拉圭", "+595"),     88: ("Honduras", "洪都拉斯", "+504"),     89: ("Tunisia", "突尼斯", "+216"),
     90: ("Nicaragua", "尼加拉瓜", "+505"),     91: ("Timor-Leste", "东帝汶", "+670"),     92: ("Bolivia", "玻利维亚", "+591"),
     93: ("Costa Rica", "哥斯达黎加", "+506"),     94: ("Guatemala", "危地马拉", "+502"),     95: ("United Arab Emirates", "阿联酋", "+971"),
     96: ("Zimbabwe", "津巴布韦", "+263"),     97: ("Puerto Rico", "波多黎各", "+1787"),     99: ("Togo", "多哥", "+228"),
     100: ("Kuwait", "科威特", "+965"),     101: ("El Salvador", "萨尔瓦多", "+503"),     102: ("Libya", "利比亚", "+218"),
     103: ("Jamaica", "牙买加", "+1876"),     104: ("Trinidad and Tobago", "特立尼达和多巴哥", "+1868"),     105: ("Ecuador", "厄瓜多尔", "+593"),
     106: ("Swaziland", "斯威士兰", "+268"),     107: ("Oman", "阿曼", "+968"),     108: ("Bosnia and Herzegovina", "波斯尼亚和黑塞哥维那", "+387"),
     109: ("Dominican Republic", "多米尼加共和国", "+1809"),     111: ("Qatar", "卡塔尔", "+974"),     112: ("Panama", "巴拿马", "+507"),
     113: ("Cuba", "古巴", "+53"),     114: ("Mauritania", "毛里塔尼亚", "+222"),     115: ("Sierra Leone", "塞拉利昂", "+232"),
     116: ("Jordan", "约旦", "+962"),     117: ("Portugal", "葡萄牙", "+351"),     118: ("Barbados", "巴巴多斯", "+1246"),
     119: ("Burundi", "布隆迪", "+257"),     120: ("Benin", "贝宁", "+229"),     121: ("Brunei Darussalam", "文莱达鲁萨兰国", "+673"),
     122: ("Bahamas", "巴哈马", "+1242"),     123: ("Botswana", "博茨瓦纳", "+267"),     124: ("Belize", "伯利兹", "+501"),
     125: ("Central African Republic", "中非共和国", "+236"),     126: ("Dominica", "多米尼加", "+1767"),     127: ("Grenada", "格林纳达", "+1473"),
     128: ("Georgia", "格鲁吉亚", "+995"),     129: ("Greece", "希腊", "+30"),     130: ("Guinea-Bissau", "几内亚比绍", "+245"),
     131: ("Guyana", "古亚那", "+592"),     132: ("Iceland", "冰岛", "+354"),     133: ("Comoros", "科摩罗", "+269"),
     134: ("Saint Kitts and Nevis", "圣基茨和尼维斯", "+1869"),     135: ("Liberia", "利比里亚", "+231"),     136: ("Lesotho", "莱索托", "+266"),
     137: ("Malawi", "马拉维", "+265"),     138: ("Namibia", "纳米比亚", "+264"),     139: ("Niger", "尼日尔", "+227"),
     140: ("Rwanda", "卢旺达", "+250"),     141: ("Slovakia", "斯洛伐克", "+421"),     142: ("Suriname", "苏里南", "+597"),
     143: ("Tajikistan", "塔吉克斯坦", "+992"),     144: ("Monaco", "摩纳哥", "+377"),     145: ("Bahrain", "巴林", "+973"),
     146: ("Reunion", "留尼汪岛", "+262"),     147: ("Zambia", "赞比亚", "+260"),     148: ("Armenia", "亚美尼亚", "+374"),
     149: ("Somalia", "索马里", "+252"),     150: ("Congo", "刚果", "+242"),     151: ("Chile", "智利", "+56"),
     152: ("Burkina Faso", "布基纳法索", "+226"),     153: ("Lebanon", "黎巴嫩", "+961"),     154: ("Gabon", "加蓬", "+241"),
     155: ("Albania", "阿尔巴尼亚", "+355"),     156: ("Uruguay", "乌拉圭", "+598"),     157: ("Mauritius", "毛里求斯", "+230"),
     158: ("Bhutan", "不丹", "+975"),     159: ("Maldives", "马尔代夫", "+960"),     160: ("Guadeloupe", "瓜德罗普", "+590"),
     161: ("Turkmenistan", "土库曼斯坦", "+993"),     162: ("French Guiana", "法属圭亚那", "+594"),     163: ("Finland", "芬兰", "+358"),
     164: ("Saint Lucia", "圣卢西亚", "+1758"),     165: ("Luxembourg", "卢森堡", "+352"),     166: ("Saint Vincent", "圣文森特", "+1784"),
     167: ("Equatorial Guinea", "赤道几内亚", "+240"),     168: ("Djibouti", "吉布提", "+253"),     169: ("Antigua and Barbuda", "安提瓜和巴布达", "+1268"),
     170: ("Cayman islands", "开曼群岛", "+1345"),     171: ("Montenegro", "黑山", "+382"),     172: ("Denmark", "丹麦", "+45"),
     173: ("Switzerland", "瑞士", "+41"),     174: ("Norway", "挪威", "+47"),     175: ("Australia", "澳大利亚", "+254"),
     176: ("Eritrea", "厄立特里亚", "+291"),     177: ("South Sudan", "南苏丹", "+211"),     178: ("Sao Tome and Principe", "圣多美和普林西比", "+239"),
     179: ("Aruba", "阿鲁巴", "+297"),     180: ("Montserrat", "蒙特塞拉特", "+1664"),     181: ("Anguilla", "安圭拉", "+1264"),
     183: ("Macedonia", "马其顿", "+389"),     184: ("Seychelles", "塞舌尔", "+248"),     185: ("New Caledonia", "新喀里多尼亚", "+687"),
     186: ("Cape Verde", "佛得角", "+238"),     187: ("United States", "美国", "+1"),     188: ("Palestine", "巴勒斯坦", "+970"),
     189: ("Fiji", "斐济", "+679"),     196: ("Singapore", "新加坡", "+65"),     199: ("Malta", "马耳他", "+356"),
     201: ("Gibraltar", "Gibraltar", "+670"),     204: ("Niue", "Niue", "+683"),     1001: ("Japan", "日本", "+81"),
     1002: ("Korea", "韩国", "+82"),     1003: ("Bermuda", "百慕大", "+1441"),     1004: ("Kosovo", "科索沃", "+383"),
     1005: ("Liechtenstein", "列支敦士登", "+423"),     1006: ("Sint Maarten", "圣马丁岛", "+1721"),     1007: ("Vanuatu", "瓦努阿图", "+678"),
     1008: ("Greenland", "格陵兰", "+299"),     1010: ("Sudan", "苏丹", "+249"),     1011: ("Martinique", "马提尼克", "+596"),
     1333: ("Syrian Arab Republic", "叙利亚阿拉伯共和国", "+963"),
}


# Live provider spellings that don't match the names above (normalized keys).
SMSBOWER_CALLING_ALIASES: dict[str, str] = {
    "russianfederation": "+7", "unitedstates": "+1", "unitedstatesvirtual": "+1",
    "unitedkingdom": "+44", "newzealand": "+64", "kazakhstan": "+7",
    "argentinas": "+54", "cotedivoireivorycoast": "+225", "laopeoples": "+856",
    "papuanewgvineya": "+675", "macedonia": "+389", "congodemrepublic": "+243",
    "moldovarepublicof": "+373", "bruneidarussalam": "+673", "macau": "+853",
    "singapore": "+65", "southkorea": "+82", "northkorea": "+850",
    "swaziland": "+268", "ecuador": "+593", "elsalvador": "+503",
    "paraguay": "+595", "costarica": "+506", "syrianarabrepublic": "+963",
    "cuba": "+53", "japan": "+81", "korea": "+82", "bermuda": "+1441",
    "sintmaarten": "+1721", "vanuatu": "+678", "guyana": "+592",
    "suriname": "+597", "belize": "+501", "botswana": "+267", "lesotho": "+266",
    "malawi": "+265", "namibia": "+264", "eritrea": "+291", "djibouti": "+253",
    "somalia": "+252", "comoros": "+269", "capeverde": "+238",
    "newcaledonia": "+687", "frenchguiana": "+594", "guadeloupe": "+590",
    "martinique": "+596", "reunion": "+262", "seychelles": "+248",
    "mauritania": "+222", "equatorialguinea": "+240",
    "centralafricanrepublic": "+236", "southsudan": "+211", "turkmenistan": "+993",
    "easttimor": "+670", "ivory": "+225", "gabon": "+241", "benin": "+229",
    "togo": "+228", "niger": "+227", "burkinafaso": "+226", "sierraleone": "+232",
    "mauritius": "+230", "eswatini": "+268", "timorleste": "+670",
    "unitedarabemirates": "+971", "palestine": "+970", "montenegro": "+382",
    "kosovo": "+383", "bosniaandherzegovina": "+387", "czechrepublic": "+420",
    "dominicanrepublic": "+1809", "trinidadandtobago": "+1868",
    "saudiarabia": "+966", "bahamas": "+1242", "barbados": "+1246",
    "jamaica": "+1876", "puertorico": "+1787", "caymanislands": "+1345",
    "anguilla": "+1264", "aruba": "+297", "montserrat": "+1664",
    "dominica": "+1767", "saintkittsandnevis": "+1869", "saintlucia": "+1758",
    "saintvincent": "+1784", "grenada": "+1473", "antiguaandbarbuda": "+1268",
    "saotomeandprincipe": "+239",
}


def _norm_sms_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").strip().lower())


def _smsbower_calling_for(eng: str, chn: str, cid: int) -> str:
    """Resolve the +prefix by country name first, id second.

    SMSBower reshuffles country ids from time to time; matching the
    normalized live name keeps prefixes correct even when the id table
    drifts out of date.
    """
    for name in (eng, chn):
        key = _norm_sms_name(name)
        if key and key in SMSBOWER_CALLING_BY_NAME:
            return SMSBOWER_CALLING_BY_NAME[key]
        text = str(name or "").strip()
        if text and text in SMSBOWER_CALLING_BY_NAME:
            return SMSBOWER_CALLING_BY_NAME[text]
    meta = SMSBOWER_COUNTRY_INFO.get(cid)
    if meta:
        return meta[2]
    return SMSBOWER_EXTRA_CALLING.get(cid, "")


SMSBOWER_CALLING_BY_NAME: dict[str, str] = {}
for _eng, _chn, _calling in SMSBOWER_COUNTRY_INFO.values():
    for _key in (_norm_sms_name(_eng), str(_chn or "").strip()):
        if _key:
            SMSBOWER_CALLING_BY_NAME.setdefault(_key, _calling)
SMSBOWER_CALLING_BY_NAME.update(SMSBOWER_CALLING_ALIASES)



# Supplemental calling codes for ids missing from the curated table above.
# Values follow the provider country id space (sms-activate family).
SMSBOWER_EXTRA_CALLING: dict[int, str] = {
    53: "+1", 58: "+58", 61: "+420", 63: "+34", 64: "+39",
    65: "+358", 67: "+30", 70: "+36", 71: "+373", 72: "+41",
    75: "+30", 77: "+7", 100: "+254", 150: "+354", 151: "+62",
    152: "+387", 153: "+370", 157: "+299", 158: "+352", 159: "+356",
    160: "+377", 161: "+376", 162: "+423", 163: "+378", 164: "+213",
    165: "+216", 166: "+218", 167: "+220", 168: "+229", 169: "+227",
    170: "+257", 171: "+250", 172: "+253", 173: "+291", 174: "+252",
    175: "+254", 176: "+255", 177: "+256", 178: "+266", 179: "+267",
    180: "+265", 181: "+269", 182: "+222", 183: "+223", 184: "+238",
    185: "+241", 186: "+242", 187: "+243", 188: "+236", 189: "+240",
    190: "+211", 191: "+93", 192: "+94", 193: "+98", 194: "+970",
    195: "+968", 196: "+974", 197: "+973", 198: "+77", 199: "+976",
    200: "+63", 201: "+670", 202: "+673", 203: "+675", 204: "+679",
    205: "+683", 206: "+685", 207: "+960", 208: "+961", 209: "+962",
    210: "+963", 211: "+964", 212: "+965", 213: "+966", 214: "+967",
    215: "+971", 216: "+972", 217: "+975", 218: "+992", 219: "+994",
    220: "+995", 221: "+996", 222: "+998", 223: "+886", 225: "+350",
    226: "+383", 227: "+389", 228: "+385", 229: "+355", 230: "+386",
    231: "+382", 232: "+258", 233: "+676", 235: "+222", 236: "+233",
    237: "+225", 238: "+221", 239: "+245", 240: "+244", 242: "+243",
    243: "+242", 244: "+252", 245: "+262", 246: "+590", 247: "+508",
    248: "+376", 249: "+383",
}


_SMS_COUNTRIES_CACHE: dict = {"at": 0.0, "payload": None, "version": 2}


def _smsbower_request(api_key: str, action: str, **params) -> str:
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    query = {"api_key": api_key, "action": action}
    query.update({k: str(v) for k, v in params.items() if v})
    request = Request(
        "https://smsbower.page/stubs/handler_api.php?" + urlencode(query),
        headers={"User-Agent": "codex-oauth-local/1.0", "Accept": "text/plain, */*"},
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace").strip()


@router.get("/api/sms-countries")
async def sms_countries(provider: str = "smsbower"):
    def _do():
        cfg = _state()._read_config()
        section = _sms_config_section(cfg)
        want = str(provider or "smsbower").strip().lower()
        import time as _time

        cache_payload = _SMS_COUNTRIES_CACHE.get("payload")
        if (
            want != "firefox"
            and cache_payload is not None
            and _SMS_COUNTRIES_CACHE.get("version") == 2
            and _time.time() - _SMS_COUNTRIES_CACHE["at"] < 60
        ):
            cached = dict(_SMS_COUNTRIES_CACHE["payload"])
            cached["cached"] = True
            return cached
        try:
            if want == "firefox":
                settings = SmsSettings.from_mapping(section)
                from urllib.parse import urlencode
                from urllib.request import Request, urlopen

                query = {"act": "PagCountry"}
                token = settings.firefox_api_token
                if token:
                    query["token"] = token
                request = Request(
                    settings.firefox_api_url + "?" + urlencode(query),
                    headers={"User-Agent": "codex-oauth-local/1.0"},
                )
                with urlopen(request, timeout=30) as response:
                    body = response.read().decode("utf-8", "replace").strip()
                import json as _json

                data = _json.loads(body)
                rows = data.get("data") if isinstance(data, dict) else data
                out = []
                for item in rows or []:
                    if not isinstance(item, dict):
                        continue
                    country_id = str(item.get("Country_ID") or item.get("id") or "").strip()
                    if not country_id:
                        continue
                    out.append(
                        {
                            "id": country_id,
                            "name": str(item.get("Country_Title") or country_id),
                            "calling": "+" + str(item.get("Country_Area") or "").lstrip("+"),
                            "count": 0,
                            "price": "",
                        }
                    )
                out.sort(key=lambda row: row["name"])
                return {"ok": True, "provider": "firefox", "items": out}
            # smsbower default branch
            api_key = str(section.get("api_key") or "")
            if not api_key:
                raise HTTPException(400, "请先保存 SMSBower API Key")
            countries_raw = _smsbower_request(api_key, "getCountries")
            prices_raw = _smsbower_request(api_key, "getPrices", service="dr")
            tiers_raw = ""
            try:
                tiers_raw = _smsbower_request(api_key, "getPricesV3", service="dr")
            except Exception:
                tiers_raw = ""
            tier_map: dict = {}
            if tiers_raw.startswith("{"):
                try:
                    tiers_doc = json.loads(tiers_raw)
                except Exception:
                    tiers_doc = {}
                for country_id, services in (tiers_doc or {}).items():
                    if not isinstance(services, dict):
                        continue
                    rows = services.get("dr")
                    if not isinstance(rows, dict):
                        continue
                    tiers = []
                    for pid, info in rows.items():
                        if not isinstance(info, dict):
                            continue
                        try:
                            tiers.append(
                                {
                                    "id": str(info.get("provider_id") or pid),
                                    "count": int(info.get("count") or 0),
                                    "price": float(info.get("price") or 0),
                                }
                            )
                        except (TypeError, ValueError):
                            continue
                    tiers.sort(key=lambda row: row["price"])
                    tier_map[str(country_id)] = tiers
            import json as _json

            countries = _json.loads(countries_raw) if countries_raw.startswith("{") else {}
            prices = _json.loads(prices_raw) if prices_raw.startswith("{") else {}
            out = []
            for country_id, info in (countries or {}).items():
                if not isinstance(info, dict):
                    continue
                # Some provider rows are malformed (empty key / null id, e.g.
                # Faroe Islands).  Skip them instead of failing the whole list.
                raw_id = str(info.get("id") or "").strip() or str(country_id or "").strip()
                if not raw_id.isdigit():
                    continue
                cid = int(raw_id)
                eng = str(info.get("eng") or info.get("rus") or f"Country {cid}")
                chn = str(info.get("chn") or "")
                meta = SMSBOWER_COUNTRY_INFO.get(cid)
                calling = _smsbower_calling_for(eng, chn, cid)
                # Live names from getCountries win; the static table only
                # supplies calling codes so ids can never mismatch names.
                display = chn or eng or (meta[0] if meta else f"Country {cid}")
                price_info = (prices.get(str(cid)) or {}).get("dr") or {}
                count = int(price_info.get("count") or 0) if isinstance(price_info, dict) else 0
                price = str(price_info.get("cost") or "") if isinstance(price_info, dict) else ""
                out.append(
                    {
                        "id": str(cid),
                        "name": display,
                        "calling": calling,
                        "count": count,
                        "price": price,
                        "tiers": tier_map.get(str(cid)) or [],
                    }
                )
            out.sort(key=lambda row: (-row["count"], row["name"]))
            payload = {"ok": True, "provider": "smsbower", "items": out}
            _SMS_COUNTRIES_CACHE["payload"] = payload
            _SMS_COUNTRIES_CACHE["at"] = _time.time()
            return payload
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"获取国家列表失败: {str(exc)[:180]}") from exc

    return await asyncio.to_thread(_do)


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
        public["firefox_docking_code_history"] = [
            str(item).strip()
            for item in section.get("firefox_docking_code_history") or []
            if str(item).strip()
        ]
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
            new_docking_code = str(body.firefox_docking_code or "").strip()
            if new_docking_code:
                # Keep a recency-ordered history of docking codes so the
                # operator can switch back to a previously used country+item
                # pair without digging through the provider console again.
                history = [
                    str(item).strip()
                    for item in current.get("firefox_docking_code_history") or []
                    if str(item).strip()
                ]
                history = [item for item in history if item != new_docking_code]
                history.insert(0, new_docking_code)
                current["firefox_docking_code_history"] = history[:20]
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


@router.delete("/api/sms-config/docking-history")
async def sms_config_clear_docking_history():
    """清空火狐狸历史对接码(演示/过期条目会一直挂在选择列表里)。"""

    def _do():
        with _state()._edit_config() as cfg:
            section = cfg.setdefault("sms_code", {})
            removed = len(
                [
                    item
                    for item in section.get("firefox_docking_code_history") or []
                    if str(item).strip()
                ]
            )
            section["firefox_docking_code_history"] = []
        return {"ok": True, "removed": removed}

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


def _persist_imported_account(
    account: dict, tokens: dict, log
) -> dict:
    """导入账号专用持久化：新账号创建，已有账号更新。

    与重新认证(reauth)的 create=False 不同，导入是新写入，
    必须允许创建记录，否则账号登录成功却不会进库。
    """
    from common.account_health import check_account
    from common.account_store import upsert_account_record

    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("协议登录未返回 AT")
    refreshed = {
        **account,
        "access_token": access_token,
        "refresh_token": str(tokens.get("refresh_token") or "").strip()
        or str(account.get("refresh_token") or ""),
        "id_token": str(tokens.get("id_token") or "").strip(),
        "session_token": str(tokens.get("session_token") or "").strip(),
        "session": tokens.get("session") or account.get("session"),
    }
    for key in (
        "plan_type",
        "type",
        "browser_type",
        "tls_impersonate",
        "user_agent",
        "sec_ch_ua",
        "sec_ch_ua_full_version_list",
        "platform_version",
        "accept_language",
        "locale",
        "timezone",
        "timezone_offset_min",
        "protocol_screen",
        "protocol_hardware_concurrency",
        "protocol_device_memory",
        "protocol_platform",
        "protocol_webgl_vendor",
        "protocol_webgl_renderer",
        "device_id",
        "protocol_profile_id",
    ):
        if key in tokens:
            refreshed[key] = tokens[key]
    try:
        checked = check_account(refreshed, persist=False, path=_state().ACCOUNTS_FILE)
    except Exception as exc:
        log(f"导入后测活失败,账号仅入库标记待续期: {exc}", "warning")
        checked = {**refreshed, "health_status": "unknown", "health_error": ""}
    else:
        if _state().normalized_health_status(checked) not in {"alive", "limited"}:
            detail = str(checked.get("health_error") or "导入后测活失败")
            raise RuntimeError(detail[:400])
    return upsert_account_record(checked, _state().ACCOUNTS_FILE, create=True)


def _import_one(
    index: int,
    email: str,
    password: str,
    totp: str,
    workspace: str = "",
    account_type: str = "personal",
) -> dict:
    # Import uses the panel's own protocol login (email -> password -> TOTP ->
    # chatgpt.com session), the same engine as the protocol-reauth page.  No
    # SMS and no Codex OAuth PKCE flow are involved.
    # Returns one result row with per-account logs; never raises so the
    # concurrent batch runner can keep going.
    from register.protocol import login_with_password_totp_one
    from register.sessions import JobCancelled

    result: dict[str, Any] = {
        "email": email,
        "status": "failed",
        "refresh_token": "",
        "token_kind": "at",
        "expires_in": None,
        "error": "",
        "logs": [],
    }
    account_logs: list = []

    def log(message: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        account_logs.append({"t": stamp, "level": level, "m": str(message)[:300]})
        _log(f"[{index}] {email} | {message}", level)

    def account_log_sink(event: dict) -> None:
        # Structured protocol step events -> readable per-account log lines.
        status = str(event.get("status") or "")
        text = str(
            event.get("step") or event.get("message") or event.get("error") or ""
        ).strip()
        if not text:
            return
        level = (
            "error" if status == "failed" else ("ok" if status == "success" else "info")
        )
        log(text, level)

    captured_errors: list = []

    def error_sink(message: str) -> None:
        captured_errors.append(str(message))

    if _STOP.is_set():
        result["error"] = "任务已停止"
        log("任务已停止，账号未执行", "warning")
        result["logs"] = account_logs
        return result

    try:
        log("协议登录启动(密码 + TOTP)")
        tokens = login_with_password_totp_one(
            index,
            email=email,
            password=password,
            totp_secret=totp,
            proxy="",
            workspace_name=str(workspace or "").strip(),
            stop_check=lambda: _STOP.is_set(),
            error_sink=error_sink,
            account_log_sink=account_log_sink,
        )
        if not tokens or not str(tokens.get("access_token") or "").strip():
            # The real failure reason arrives via error_sink; keep the generic
            # text only as a fallback.
            raise RuntimeError(
                captured_errors[-1] if captured_errors else "协议登录未取得访问凭证"
            )
        account = {
            "email": email,
            "password": password,
            "password_set": True,
            "totp_secret": totp,
            "mfa_enabled": True,
            "mfa_factor_type": "totp",
            "registration_method": "protocol",
            "source_type": "imported",
            "registration_state": "complete",
            "proxy": "",
        }
        # 账号类型只有 personal/team；套餐由登录后测活自动识别，
        # team 标记仅作测活失败时的套餐兜底
        if account_type == "team":
            account["plan_type"] = "team"
            # 记录导入时选择的工作区名,接码/重登时按此工作区选择。
            account["workspace_name"] = str(workspace or "").strip()
        saved = _persist_imported_account(account, tokens, log)
        at = str(saved.get("access_token") or tokens.get("access_token") or "")
        log("协议登录成功,账号已写入账号库", "ok")
        result.update(
            {
                "status": "success",
                "refresh_token": at,
                "expires_in": saved.get("expires_at") or None,
                "error": "",
            }
        )
    except JobCancelled:
        result["error"] = "任务已停止"
        log("任务已停止", "warning")
    except Exception as exc:
        message = str(exc)[:260]
        result["error"] = message
        log(f"失败: {message}", "error")
    result["logs"] = account_logs[-200:]
    return result


def _run_import_batch(
    creds: list,
    workspace: str = "",
    concurrency: int = 1,
    account_type: str = "personal",
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    workers = max(1, min(int(concurrency or 1), 8, len(creds) or 1))
    try:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="import"
        ) as pool:
            futures = [
                pool.submit(
                    _import_one,
                    index,
                    email,
                    password,
                    totp,
                    workspace,
                    account_type,
                )
                for index, (email, password, totp) in enumerate(creds, start=1)
            ]
            for future in as_completed(futures):
                result = future.result()
                with _LOCK:
                    _STATE["done"] += 1
                    _STATE["results"].append(result)
                    if result.get("status") == "success":
                        _STATE["success"] += 1
                    else:
                        _STATE["failed"] += 1
    except Exception as exc:
        _log(f"导入批次异常终止: {exc}", "error")
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")


class CodexOauthImportRequest(BaseModel):
    text: str = Field(default="", max_length=200000)
    # team 账号 MFA 后会进入工作区选择页，需要指定工作区名；个人账号留空。
    # 套餐(free/plus/pro/team)由登录后测活自动识别，不由导入时指定。
    account_type: str = Field(default="personal", max_length=16)
    workspace: str = Field(default="", max_length=128)
    # 同时进行协议登录的账号数(1-8)
    concurrency: int = Field(default=1, ge=1, le=8)


@router.post("/api/codex-oauth/import")
async def codex_oauth_import(body: CodexOauthImportRequest):
    def _do():
        with _LOCK:
            if _STATE["running"]:
                raise HTTPException(409, "已有接码/导入任务在运行")
        creds = _parse_credential_lines(body.text)
        if not creds:
            raise HTTPException(400, "未识别到有效账号行(格式:邮箱----密码----2FA)")
        # 账号类型只支持 personal/team；套餐由登录后测活识别
        account_type = str(body.account_type or "personal").strip().lower()
        if account_type != "team":
            account_type = "personal"
        workspace = str(body.workspace or "").strip()
        if account_type == "team" and not workspace:
            raise HTTPException(400, "Team 账号请填写工作区名称")
        if account_type != "team":
            workspace = ""
        concurrency = int(body.concurrency or 1)
        with _LOCK:
            _STATE.update(
                {
                    "running": True,
                    "mode": "import",
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
            args=(creds, workspace, concurrency, account_type),
            name="codex-oauth-import",
            daemon=True,
        ).start()
        type_label = "Team" if account_type == "team" else "个人"
        plan = f"Team 工作区「{workspace}」" if workspace else type_label
        _log(
            f"导入任务启动:{len(creds)} 个账号 · 协议登录(密码+TOTP) · {plan} · 并发 {min(concurrency, len(creds))}"
        )
        return {"ok": True, "total": len(creds)}

    return await asyncio.to_thread(_do)


class CodexOauthRunRequest(BaseModel):
    emails: list = Field(default_factory=list, max_length=200)


def _start_sms_batch(wanted: set[str]) -> dict:
    """按选中邮箱启动接码批次。"""
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
                "mode": "codex",
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


@router.post("/api/codex-oauth/run")
async def codex_oauth_run(body: CodexOauthRunRequest):
    def _do():
        with _LOCK:
            if _STATE["running"]:
                raise HTTPException(409, "已有接码任务在运行")
        wanted = {str(e).strip().lower() for e in body.emails if str(e).strip()}
        if not wanted:
            raise HTTPException(400, "未选择账号")
        return _start_sms_batch(wanted)

    return await asyncio.to_thread(_do)


@router.get("/api/codex-oauth/status")
async def codex_oauth_status():
    with _LOCK:
        payload = {
            key: (list(value) if key in ("logs", "results") else value)
            for key, value in _STATE.items()
        }
    return {"ok": True, **payload}


@router.get("/api/codex-oauth/history")
async def codex_oauth_history(emails: str = ""):
    """Return persisted RT-reissue history rows for the requested accounts.

    Accounts that obtained their RT before this log existed still have a
    usable RT, so synthesize one row from the stored token instead of
    showing an empty list the operator cannot act on.
    """

    def _do():
        wanted = {e.strip().lower() for e in str(emails or "").split(",") if e.strip()}
        try:
            data = json.loads(_RT_HISTORY_PATH.read_text(encoding="utf-8")) if _RT_HISTORY_PATH.is_file() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if wanted:
            data = {k: v for k, v in data.items() if k in wanted}
            # Backfill: no recorded rows but the account owns an RT.
            missing = [e for e in wanted if e not in data or not data.get(e)]
            if missing:
                try:
                    accounts = _state()._load_accounts() if hasattr(_state(), "_load_accounts") else []
                except Exception:
                    accounts = []
                by_email = {}
                for acc in accounts or []:
                    mail = str(acc.get("email") or "").strip().lower()
                    if mail:
                        by_email[mail] = acc
                for mail in missing:
                    acc = by_email.get(mail)
                    if not acc:
                        continue
                    rt = str(acc.get("refresh_token") or "").strip()
                    if not rt:
                        continue
                    data[mail] = [
                        {
                            "status": "success",
                            "at": "早于日志功能(时间未记录)",
                            "phone": "",
                            "rt": rt,
                            "note": "该 RT 来自历史接码，当时未记录日志",
                        }
                    ]
        return {"ok": True, "history": data}

    return await asyncio.to_thread(_do)


@router.post("/api/codex-oauth/stop")
async def codex_oauth_stop():
    _STOP.set()
    _log("已请求停止,当前账号完成后中止", "warning")
    return {"ok": True}
