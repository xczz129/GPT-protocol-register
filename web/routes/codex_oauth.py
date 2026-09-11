# -*- coding: utf-8 -*-
# Codex OAuth RT issuance: batch protocol login with SMS phone verification.
#
# Codex OAuth integration for the panel: selected accounts can mint a real
# refresh_token (offline_access scope) through the local Codex PKCE client,
# with SMSBower / Firefox.fun SMS providers for the add-phone step when needed.

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import sys
import threading
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

# Keep the Codex OAuth implementation inside this project so the panel can be
# deployed and exported as one self-contained source tree.  The project root is
# already on sys.path when cli.py/web_app.py starts; this guard also covers
# direct imports from tests and tooling.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from common.account_store import account_delete_key, update_account_record_fields  # noqa: E402
from common.team_workspace import requires_team_workspace, session_identity, validate_session_replacement  # noqa: E402
from common.relay_recovery import (  # noqa: E402
    auth_known_invalid, read_relay_entry, relay_entry_status, relay_state, safe_stored_bundle, scan_relay,
)
from codex_oauth.oauth import (  # noqa: E402
    ProtocolClient,
    decode_jwt_payload,
    exchange_code,
    token_metadata,
    totp_from_secret,
)
from codex_oauth.sms import (  # noqa: E402
    FIREFOX_COUNTRY_NAMES,
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

# cli-proxy-api 容器挂载的 auths 目录;接码成功后自动写入,账号即可被中转使用
CLIPROXY_AUTHS_DIR = Path("/opt/codex-services/data/cliproxy/auths")
_RELAY_WRITE_LOCK = threading.Lock()


def _cliproxy_auth_filename(email: str, account_id: str, plan_type: str) -> str:
    digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:8]
    plan_norm = re.sub(r"[^a-z0-9]+", "-", str(plan_type or "").lower()).strip("-")
    safe_email = re.sub(r"[^a-zA-Z0-9@._+\-]", "_", email)
    base = f"codex-{digest}-{safe_email}"
    return f"{base}-{plan_norm}.json" if plan_norm else f"{base}.json"


def _sync_cliproxy_auth(email: str, token: dict, log, *, expected_hash: str | None = None) -> str:
    """Write and read back a fresh/safely reusable bundle; no silent success."""
    if not CLIPROXY_AUTHS_DIR.is_dir():
        raise RuntimeError("未检测到中转 auths 目录，凭据尚未同步")
    identity = session_identity(token)
    account_id = identity["account_id"]
    if not account_id:
        raise RuntimeError("Token 缺少实际工作区 ID，凭据尚未同步")
    if not str(token.get("access_token") or "").strip() or not str(token.get("refresh_token") or "").strip():
        raise RuntimeError("AT/RT 不完整，凭据尚未同步")
    plan_type = identity["plan_type"]
    filename = _cliproxy_auth_filename(email, account_id, plan_type)
    target = CLIPROXY_AUTHS_DIR / filename
    if target.is_symlink():
        raise RuntimeError("中转目标是符号链接，未覆盖")
    existed = target.exists()
    record = token_metadata(token, email)
    record.update(account_id=account_id, plan_type=plan_type, user_id=identity["user_id"], oauth_client_id=CODEX_CLIENT_ID)
    descriptor, temp_name = tempfile.mkstemp(prefix=".codex-sync-", suffix=".tmp", dir=str(CLIPROXY_AUTHS_DIR))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.chmod(temp_name, 0o600)
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        if expected_hash is not None:
            observed_hash = read_relay_entry(target).get("sha256") or "missing"
            if observed_hash != expected_hash:
                raise RuntimeError("中转文件在同步期间被其他操作更新，未覆盖，请重新检查")
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    saved = read_relay_entry(target)["record"]
    if any(saved.get(key) != record.get(key) for key in ("email", "account_id", "access_token", "refresh_token")):
        raise RuntimeError("中转凭据写后核验失败，请重试同步")
    # 覆盖/写入后清掉面板的额度缓存,下次查询会用新凭据重新拉取
    try:
        from web.routes.cliproxy import _QUOTA_CACHE, _QUOTA_LOCK

        with _QUOTA_LOCK:
            _QUOTA_CACHE.pop(filename, None)
    except Exception:
        pass
    log(f"{'已覆盖更新中转(旧凭据失效时重新登录的常规操作)' if existed else '已同步到中转'} {filename}", "ok")
    return filename


def _relay_context() -> dict:
    from web.routes.cliproxy import _QUOTA_CACHE, _QUOTA_LOCK

    with _QUOTA_LOCK:
        cache = copy.deepcopy(_QUOTA_CACHE)
    return scan_relay(CLIPROXY_AUTHS_DIR, cache)


def _current_relay_context(rec: dict, context: dict) -> dict:
    """Recheck only this account's indexed candidates after waiting in a queue."""
    email = str(rec.get("email") or "").strip().lower()
    identity = session_identity(rec)
    names = {entry["file"] for entry in context.get("by_email", {}).get(email, [])}
    if identity["account_id"]:
        names.add(_cliproxy_auth_filename(email, identity["account_id"], identity["plan_type"]))
    result = {"configured": CLIPROXY_AUTHS_DIR.is_dir(), "by_email": {email: []}, "by_file": {}}
    for name in names:
        path = CLIPROXY_AUTHS_DIR / name
        if not path.exists():
            continue
        snapshot = context.get("by_file", {}).get(name) or {}
        entry = read_relay_entry(path, snapshot.get("cache"))
        if snapshot.get("sha256") and entry.get("sha256") != snapshot["sha256"]:
            entry["cache"] = {}
        result["by_file"][name] = entry
        result["by_email"][email].append(entry)
    return result


class _RelayRequiresAuth(RuntimeError):
    pass


def _sync_existing_relay(rec: dict, context: dict, log) -> tuple[str, bool]:
    """Reuse only verified bundles and preserve any already-healthy relay file."""
    rec = _current_sms_record(rec)
    token = safe_stored_bundle(rec)
    identity = session_identity(token)
    filename = _cliproxy_auth_filename(rec["email"], identity["account_id"], identity["plan_type"])
    snapshot_state = relay_state(rec, context)
    pending = _fresh_relay_pending(rec)
    with _RELAY_WRITE_LOCK:
        # Recheck the matching file as well as the canonical write target.
        names = {filename, snapshot_state.get("file") or filename}
        for name in names:
            path = CLIPROXY_AUTHS_DIR / name
            if not path.exists():
                continue
            snapshot = context.get("by_file", {}).get(name) or {}
            entry = read_relay_entry(path, snapshot.get("cache"))
            if snapshot.get("sha256") and entry.get("sha256") != snapshot.get("sha256"):
                entry["cache"] = {}
            status = relay_entry_status(rec, entry)
            if status == "ready" and not pending:
                log(f"中转已有完整凭据，保留当前文件 {name}", "ok")
                return name, False
            if name == filename:
                record = entry.get("record") or {}
                if pending and status in ("ready", "invalid", "incomplete"):
                    if status == "invalid" and record.get("refresh_token") == token["refresh_token"]:
                        raise _RelayRequiresAuth("当前同一 RT 已有明确认证失效记录，需要重新授权")
                    if status == "ready" and record.get("access_token") == token["access_token"] and record.get("refresh_token") == token["refresh_token"]:
                        _mark_relay_synced(rec, token)
                        return name, True
                elif status == "incomplete" and record.get("access_token") and record.get("refresh_token"):
                    # Repair missing metadata without rolling back a relay RT
                    # that may have rotated after the account-store snapshot.
                    try:
                        token = safe_stored_bundle({**rec, **record})
                    except RuntimeError as exc:
                        raise _RelayRequiresAuth(str(exc)) from exc
                elif entry.get("unsafe") or (record.get("access_token") and record.get("refresh_token")):
                    raise _RelayRequiresAuth("中转目标含已有凭据但无法安全复用，需要重新授权，未覆盖")
                if record.get("email") and str(record["email"]).casefold() != str(rec["email"]).casefold():
                    raise _RelayRequiresAuth("中转目标属于其他邮箱，未覆盖")
        expected = rec.get("relay_sync_base_hash") if pending else (
            (context.get("by_file", {}).get(filename) or {}).get("sha256") or "missing"
        )
        result = _sync_cliproxy_auth(rec["email"], token, log, expected_hash=expected)
        if not result:
            raise RuntimeError("中转同步未返回已写入文件，凭据尚未同步")
        _mark_relay_synced(rec, token)
        return result, True


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
    "batch_id": "",
    "status": "idle",
    "items": [],
    "options": {},
    "counts": {},
    "force": False,
    "concurrency": 1,
}
_MAX_LOGS = 800


def _reserve_batch(mode: str, total: int, batch_id: str) -> threading.Event:
    """Atomically occupy the shared import/SMS slot and its stop event."""
    global _STOP
    with _LOCK:
        if _STATE["running"]:
            raise HTTPException(409, "已有接码/导入任务在运行")
        stop_event = threading.Event()
        _STOP = stop_event
        _STATE.update(
            running=True, mode=mode, batch_id=batch_id, total=total,
            done=0, success=0, failed=0, logs=[], results=[],
            started_at=datetime.now().isoformat(timespec="seconds"), finished_at="",
            status="running", items=[], options={}, counts={}, force=False, concurrency=1,
        )
        return stop_event


def _finish_batch(batch_id: str) -> None:
    with _LOCK:
        if _STATE.get("batch_id", "") == batch_id:
            _STATE["running"] = False
            _STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _STATE["status"] = "cancelled" if (
                _STATE.get("status") == "stopping" or (_STATE.get("counts") or {}).get("cancelled")
            ) else "completed"


def _sms_counts_locked() -> None:
    """Reconcile terminal, queued and active rows while holding _LOCK."""
    items = _STATE.get("items") or []
    counts = {key: 0 for key in ("success", "synced", "skipped", "failed", "cancelled", "pending", "active")}
    for item in items:
        status = item.get("status", "pending")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(items)
    counts["done"] = sum(counts[key] for key in ("success", "synced", "skipped", "failed", "cancelled"))
    _STATE["counts"] = counts


def _set_sms_item(email: str, batch_id: str, **updates) -> None:
    with _LOCK:
        if _STATE.get("mode") != "codex" or _STATE.get("batch_id", "") != batch_id:
            return
        for item in _STATE.get("items", []):
            if item["email"] == email:
                item.update(updates, updated_at=datetime.now().isoformat(timespec="seconds"))
                _sms_counts_locked()
                return


class _SmsCancelled(RuntimeError):
    pass


def _launch_batch(target, args: tuple, *, batch_id: str, stop_event: threading.Event, name: str) -> None:
    try:
        threading.Thread(target=target, args=args, name=name, daemon=True).start()
    except Exception:
        stop_event.set()
        _finish_batch(batch_id)
        raise


def _log(message: str, level: str = "info", *, batch_id: str = "") -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with _LOCK:
        if batch_id and _STATE.get("batch_id") != batch_id:
            return
        _STATE["logs"].append({"t": stamp, "level": level, "m": str(message)[:300]})
        overflow = len(_STATE["logs"]) - _MAX_LOGS
        if overflow > 0:
            del _STATE["logs"][:overflow]


# ---------------------------------------------------------------------------
# 接码任务不做断点续跑：一个账号只能绑定一次手机号，中断后剩余账号
# 由操作者手动重新发起（选中账号 → 接码），进度仅在内存中展示。


_RT_HISTORY_PATH = Path("/root/full/gpt-register-fp/data/codex_rt_history.json")
_RT_HISTORY_LOCK = threading.Lock()

# 导入(协议登录)逐账号历史：每个账号完成时追加一行 JSONL，供面板的
# "历史日志" 弹窗按批次查看每个账号的完整登录日志。
_IMPORT_HISTORY_PATH = Path("/root/full/gpt-register-fp/data/import_history.jsonl")
_IMPORT_HISTORY_LOCK = threading.Lock()
_IMPORT_HISTORY_MAX_BYTES = 5 * 1024 * 1024
_IMPORT_HISTORY_KEEP_LINES = 1500


def _rotate_import_history() -> None:
    """Keep the JSONL history bounded; called once per batch start."""
    try:
        if (
            _IMPORT_HISTORY_PATH.is_file()
            and _IMPORT_HISTORY_PATH.stat().st_size > _IMPORT_HISTORY_MAX_BYTES
        ):
            lines = _IMPORT_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
            _IMPORT_HISTORY_PATH.write_text(
                "\n".join(lines[-_IMPORT_HISTORY_KEEP_LINES:]) + "\n",
                encoding="utf-8",
            )
    except Exception:
        pass


def _append_import_history(entry: dict) -> None:
    try:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with _IMPORT_HISTORY_LOCK:
            _IMPORT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _IMPORT_HISTORY_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        pass


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


_SMSBOWER_PRICE_CACHE: dict = {"at": 0.0, "map": {}}


def _smsbower_tier_prices(api_key: str, service: str) -> dict:
    """getPricesV3 的 ``{country: {tier: price}}``,缓存 5 分钟。

    只用于日志展示(已分配号码时带出档位价格);查询失败时返回上一次
    的结果,接码流程不因价格展示失败而中断。
    """
    import time as _time

    now = _time.time()
    if _SMSBOWER_PRICE_CACHE["map"] and now - _SMSBOWER_PRICE_CACHE["at"] < 300:
        return _SMSBOWER_PRICE_CACHE["map"]
    try:
        raw = _smsbower_request(api_key, "getPricesV3", service=service)
        doc = json.loads(raw) if raw.startswith("{") else {}
    except Exception:
        return _SMSBOWER_PRICE_CACHE["map"]
    out: dict = {}
    for country_id, services in (doc or {}).items():
        rows = services.get(service) if isinstance(services, dict) else None
        if not isinstance(rows, dict):
            continue
        tiers = {}
        for pid, info in rows.items():
            if isinstance(info, dict) and info.get("price") is not None:
                tiers[str(pid)] = str(info.get("price"))
        out[str(country_id)] = tiers
    _SMSBOWER_PRICE_CACHE.update(at=now, map=out)
    return out


class _PanelSmsBowerClient(SmsBowerClient):
    """SMSBower client with per-country provider tiers and maxPrice support.

    取号组合(国家×档位)带轮转游标:上一次分配到的组合,下一次 acquire 从其
    后继组合开始,依次循环。这样 OpenAI 拒掉某个组合的号码后(如 VoIP 号),
    换号重试会切到下一个价位,价位用完切下一个国家,全部轮完再从头来。
    """

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
        # 轮转游标:指向下一次 acquire 的起始组合下标。
        self._cursor = 0

    def _combinations(self) -> list:
        """展平国家×档位组合,保持操作者配置的优先顺序。"""
        combos: list = []
        for country, tiers, max_price in self.country_specs:
            tier_list = [str(tier).strip() for tier in tiers if str(tier).strip()] or [""]
            for tier in tier_list:
                combos.append((country, tier, max_price))
        return combos

    def acquire(self) -> SmsActivation:
        combos = self._combinations()
        if not combos:
            raise SmsError("没有配置任何国家/档位")
        last_error = "没有可用号码"
        prices = _smsbower_tier_prices(self.settings.api_key, self.settings.service)
        total = len(combos)
        for offset in range(total):
            index = (self._cursor + offset) % total
            country, tier, max_price = combos[index]
            params = {
                "service": self.settings.service,
                "country": country,
            }
            if tier:
                params["providerIds"] = tier
            # An annotated country price must never defeat the run-wide cap.
            caps = [p for p in (max_price, self.settings.max_price) if p]
            price = str(min(float(p) for p in caps)) if caps else ""
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
            # 号码来自当前组合;游标推进到后继组合,失败换号时不再重复它。
            self._cursor = (index + 1) % total
            label = self._country_label(country, tier, prices)
            raw_price = prices.get(str(country), {}).get(str(tier))
            try:
                estimate = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                estimate = None
            return SmsActivation(
                parts[1], parts[2], int(country), self.settings.api_key, label=label,
                provider="smsbower", tier=str(tier), estimated_price=estimate,
            )
        raise SmsError(last_error)

    @staticmethod
    def _country_label(country: str, tier: str, prices: dict) -> str:
        """国家中文名 + 档位/价格,如 ``印度尼西亚 #1170 $0.107``。"""
        try:
            meta = SMSBOWER_COUNTRY_INFO.get(int(country))
        except (TypeError, ValueError):
            meta = None
        label = meta[1] if meta else str(country)
        if tier:
            label += f" #{tier}"
            tier_price = prices.get(str(country), {}).get(str(tier))
            if tier_price:
                label += f" ${tier_price}"
        return label


class _PanelFirefoxSmsClient(FirefoxSmsClient):
    """Firefox client:对接码锁定国家+项目,取号只认对接码。

    对接码一次锁定国家与项目(如 ``eng-1096-DEMO01``),不再回退国家
    列表;未配置对接码直接报错,提示去火狐狸接码页复制。
    """

    def __init__(self, settings: SmsSettings, country_specs: list, *, timeout: float = 30.0):
        super().__init__(settings, timeout=timeout)
        # country_specs 仅 SMSBower 使用;火狐狸忽略。

    def acquire(self) -> SmsActivation:
        dock_country, item_id, did = parse_firefox_docking_code(self.settings.firefox_docking_code)
        if not (dock_country and item_id and did):
            raise SmsError("火狐狸需要配置对接码(格式: eng-1096-DEMO01,在火狐狸接码页复制)")
        self._login()
        params = {
            "iid": item_id,
            "country": dock_country,
            "dock": "0",
            "otpmode": "sms",
            "did": did,
        }
        if self.settings.max_price:
            params["maxPrice"] = self.settings.max_price
        body = self._authorized_request("getPhone", **params)
        if not body.startswith("1|"):
            raise SmsError(f"火狐狸未分配号码：{self._failure(body)[:120]}")
        parts = body.split("|")
        if len(parts) < 8 or not parts[1].strip() or not parts[7].strip():
            raise SmsError(f"火狐狸响应格式异常：{body[:160]}")
        returned_country = normalize_firefox_country(parts[3] if len(parts) > 3 else "") or dock_country
        returned_prefix = parts[4] if len(parts) > 4 else ""
        phone = self._normalize_phone(parts[7], returned_prefix, returned_country)
        label = FIREFOX_COUNTRY_NAMES.get(returned_country, returned_country)
        return SmsActivation(
            activation_id=parts[1].strip(),
            phone=phone,
            country=returned_country,
            api_key=self._token,
            label=label,
            provider="firefox",
        )


def _sms_client(settings: SmsSettings, country_specs: list | None = None):
    if not settings.enabled or not settings.credentials_configured:
        return None
    specs = list(country_specs or [])
    if settings.provider == "firefox":
        return _PanelFirefoxSmsClient(settings, specs, timeout=30.0)
    return _PanelSmsBowerClient(settings, specs, timeout=30.0)


def _proxy_retryable_error(error: BaseException | str) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "http 429", "http_429", "rate limit", "rate_limit", "proxy",
            "connection reset", "connection closed", "timed out", "tls connect",
            "curl: (7)", "curl: (28)", "curl: (35)", "curl: (56)",
        )
    )


# 2FA 验证码被拒(Incorrect code)时的自动重试上限:
# 第 1 次等下一个 30s 窗口换新码;第 2/3 次用 +1/-1 相邻窗口码,
# 补偿本地与服务器之间的时钟偏移。
_MFA_CODE_RETRIES = 3


def _mfa_code_error(error: BaseException | str) -> bool:
    lowered = str(error or "").lower()
    return "mfa/verify" in lowered and (
        "incorrect code" in lowered or "incorrect_code" in lowered
    )


def _persist_sms_tokens(previous: dict, token: dict) -> dict:
    """Validate fresh evidence and update only tokens whose starting row is current."""
    identity = validate_session_replacement(
        previous, token, require_identity=requires_team_workspace(previous),
    )
    target = CLIPROXY_AUTHS_DIR / _cliproxy_auth_filename(
        str(previous.get("email") or ""), identity["account_id"], identity["plan_type"],
    )
    relay_hash = read_relay_entry(target).get("sha256") or "missing"
    fields = {
        "refresh_token": str(token.get("refresh_token") or ""),
        "id_token": str(token.get("id_token") or ""),
        "access_token": str(token.get("access_token") or ""),
        "oauth_client_id": CODEX_CLIENT_ID,
        "last_token_refresh_at": _state()._now_iso(), "last_token_refresh_error": "",
        "relay_sync_pending": True,
        "relay_sync_token_hash": hashlib.sha256(str(token.get("refresh_token") or "").encode()).hexdigest(),
        "relay_sync_base_hash": relay_hash,
    }
    for field in ("user_id", "plan_type"):
        if identity.get(field):
            fields[field] = identity[field]
    key = account_delete_key(previous)
    expected = {field: previous.get(field) for field in (
        "access_token", "refresh_token", "email", "selected_workspace_id",
        "selected_workspace_name", "workspace_name", "import_account_type", "plan_type",
    )}
    changed, _ = update_account_record_fields(
        {key: fields}, expected_fields={key: expected}, include_records=False,
        path=_state().ACCOUNTS_FILE,
    )
    if not changed:
        raise RuntimeError("sms_credentials_changed: 账号已删除或凭据/工作区在接码期间更新，未覆盖当前记录")
    return {"base_hash": relay_hash}


def _fresh_relay_pending(rec: dict) -> bool:
    rt = str(rec.get("refresh_token") or "")
    return bool(rt and rec.get("relay_sync_pending") is True and rec.get("relay_sync_token_hash") == hashlib.sha256(rt.encode()).hexdigest())


def _mark_relay_synced(previous: dict, token: dict) -> None:
    current = {**previous, **{key: token.get(key) or "" for key in ("access_token", "refresh_token")}}
    identity = session_identity(token)
    if identity["user_id"]:
        current["user_id"] = identity["user_id"]
    key = account_delete_key(current)
    update_account_record_fields(
        {key: {"relay_sync_pending": False}},
        expected_fields={key: {"refresh_token": token.get("refresh_token")}},
        include_records=False, path=_state().ACCOUNTS_FILE,
    )


def _process_one(
    rec: dict,
    sms_cfg: dict,
    steps: list | None = None,
    *,
    stop_event: threading.Event | None = None,
    batch_id: str = "",
    phone_attempts: list | None = None,
) -> dict:
    stop_event = stop_event if stop_event is not None else _STOP
    email = str(rec.get("email") or "").strip()
    password = str(rec.get("password") or "")
    totp = str(rec.get("totp_secret") or "")
    phone_attempts = phone_attempts if phone_attempts is not None else []

    def stage(name: str, message: str, *, wait_deadline: float | None = None) -> None:
        if stop_event.is_set():
            raise _SmsCancelled("任务已停止")
        _set_sms_item(
            email, batch_id, status="active", stage=name, message=message,
            attempt=len(phone_attempts), wait_deadline=wait_deadline,
        )

    def log(message: str, level: str = "info") -> None:
        _log(f"{email} | {message}", level, batch_id=batch_id)
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

    # Team 账号按导入时指定的工作区名选择;个人账号为空,默认第一个。
    strict_team = requires_team_workspace(rec)
    workspace_name = str(rec.get("selected_workspace_name") or rec.get("workspace_name") or "").strip()
    expected_workspace_id = str(rec.get("selected_workspace_id") or "").strip()
    if strict_team and not expected_workspace_id and requires_team_workspace({
        "access_token": rec.get("access_token"), "session": rec.get("session"),
    }):
        expected_workspace_id = session_identity(rec)["account_id"]
    if workspace_name:
        log(f"使用工作区「{workspace_name}」")

    # OpenAI 的 add-phone 会话没有换号接口:提交过一个号码后,同一会话再
    # 提交新号码会得到 invalid_auth_step。因此每个号码配一整轮完整登录
    # (新会话 bootstrap → 邮箱 → 密码 → 2FA → 接码),号码失败取消后
    # 重新登录走下一轮。
    rounds = int(settings.max_phone_attempts or 5) if sms is not None else 1
    last_error = ""
    mfa_code_failures = 0
    for attempt in range(1, rounds + 1 + _MFA_CODE_RETRIES):
        if stop_event.is_set():
            raise _SmsCancelled("任务已停止")
        # 接码流程不再走注册代理,统一本地直连
        client = ProtocolClient(email, timeout=30)
        try:
            log("本地直连" + (f"(第 {attempt} 轮)" if attempt > 1 else ""))
            stage("login", "初始化登录会话")
            log("初始化会话与 PKCE")
            client.bootstrap()
            stage("login", "提交账号邮箱")
            client.submit_email()
            log("邮箱已提交")
            stage("password", "验证密码")
            client.verify_password(password)
            log("密码验证通过")
            stage("mfa", "验证二次认证")
            mfa_id = client.mfa_id()
            client.issue_mfa_challenge(mfa_id)
            # 重试时换用相邻时间窗口的验证码,补偿本地与服务器时钟偏移
            skew = {1: 0, 2: 1, 3: -1}.get(mfa_code_failures, 0)
            mfa_response = client.verify_mfa(
                mfa_id, totp_from_secret(totp, at=time.time() + 30 * skew)
            )
            log("2FA 验证通过")
            try:
                mfa_data = mfa_response.json()
            except Exception:
                mfa_data = {}
            if not isinstance(mfa_data, dict):
                mfa_data = {}
            phone_used = ""
            phone_required, reason = _phone_requirement(mfa_data)
            if phone_required is True:
                log(f"服务端要求手机验证({reason or 'add-phone'})")
                if sms is None:
                    raise RuntimeError("需要手机验证,但接码未启用或未配置")
                # OpenAI 2026-09 起校验页面导航:先进入 add-phone 页面,
                # 否则 add-phone/send 直接 409 session invalid
                nav_url = str(reason or "").strip()
                if nav_url.startswith("http"):
                    try:
                        client.navigate(nav_url)
                        log("已进入手机验证页面")
                    except Exception as exc:
                        log(f"进入手机验证页面失败({str(exc)[:100]})", "warning")
                # MFA/login retries do not grant extra paid number attempts.
                if len(phone_attempts) >= rounds:
                    raise RuntimeError(f"已达到最多 {rounds} 次取号上限")
                stage("acquiring", "正在获取号码")
                phone_attempt = {
                    "attempt": len(phone_attempts) + 1, "provider": settings.provider,
                    "country": "", "country_label": "未知", "tier": "",
                    "estimated_price": None, "price_source": "unknown",
                    "acquired": False, "result": "acquiring",
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                phone_attempts.append(phone_attempt)
                _set_sms_item(email, batch_id, attempt=len(phone_attempts))
                log(f"接码获取号码(第 {len(phone_attempts)}/{rounds} 次)")
                try:
                    activation = sms.acquire()
                except Exception:
                    phone_attempt["result"] = "acquire_failed"
                    raise
                label = activation.label or str(activation.country)
                country_label = re.split(r"\s+#|\s+\$", label, maxsplit=1)[0]
                estimate = getattr(activation, "estimated_price", None)
                phone_attempt.update(
                    acquired=True, country=str(activation.country), country_label=country_label,
                    tier=str(getattr(activation, "tier", "") or ""),
                    estimated_price=estimate, price_source="catalogue" if estimate is not None else "unknown",
                    result="pending",
                )
                try:
                    desc = activation.label or f"国家 {activation.country}"
                    log(f"已分配 {activation.phone}({desc})")
                    stage("phone", "正在提交手机号")
                    client.send_phone(activation.phone)
                    log("手机号已提交,等待短信验证码")
                    stage("waiting_sms", "等待短信验证码", wait_deadline=time.time() + settings.timeout_seconds)
                    code = sms.wait_code(activation, cancel_event=stop_event)
                    stage("verifying_sms", "正在验证短信")
                    log("收到验证码,提交验证")
                    client.submit_phone_otp(code)
                    phone_attempt["result"] = "success"
                    try:
                        sms.finish(activation)
                    except Exception:
                        # The upstream already accepted this number. A supplier
                        # acknowledgement failure must not buy another number.
                        log("手机验证已通过，供应商完成确认失败，请稍后核对供应商记录", "warning")
                    log("手机验证通过")
                    phone_used = activation.phone
                except Exception as exc:
                    last_error = str(exc)
                    cancelled = stop_event.is_set() or isinstance(exc, _SmsCancelled)
                    # Successful phone verification remains successful even if
                    # the provider's finish request subsequently fails.
                    if phone_attempt["result"] != "success":
                        phone_attempt["result"] = "cancelled" if cancelled else "failed"
                    # 号码不可用:取消激活,会话作废,由外层重新登录换号。
                    try:
                        sms.cancel(activation)
                        log("号码已取消,重新登录换号", "warning")
                    except Exception:
                        log("号码取消失败(继续换号)", "warning")
                    log(f"号码 {activation.phone} 失败: {last_error[:200]}", "warning")
                    _set_sms_item(email, batch_id, wait_deadline=None)
                    if cancelled:
                        raise _SmsCancelled("任务已停止") from exc
                    if len(phone_attempts) >= rounds:
                        raise RuntimeError(f"取号 {len(phone_attempts)}/{rounds} 次均未完成: {last_error[:160]}") from exc
                    continue
            else:
                log("无需手机验证,直接进入 workspace")
            stage("workspace", "选择工作区")
            try:
                callback = client.select_workspace(
                    workspace_name, strict_team=True, expected_workspace_id=expected_workspace_id,
                ) if strict_team else client.select_workspace(workspace_name)
            except Exception as exc:
                if (
                    sms is not None
                    and attempt < rounds
                    and "phone" in str(exc).lower()
                ):
                    # workspace 步骤回退到手机验证:同样作废会话重登。
                    last_error = str(exc)
                    log("workspace 返回手机号步骤,重新登录进入接码", "warning")
                    continue
                raise
            query = parse_qs(urlparse(callback).query)
            code = query.get("code", [""])[0]
            if not code:
                raise RuntimeError("回调缺少授权 code")
            log("交换 OAuth Token")
            stage("token", "正在获取并保存 RT")
            token = exchange_code(code, client.verifier)
            refresh = str(token.get("refresh_token") or "")
            if not refresh:
                raise RuntimeError("响应缺少 refresh_token(scope 未含 offline_access?)")
            pending_sync = _persist_sms_tokens(rec, token)
            log("RT 已写回账号库", "ok")
            relay_file, relay_error = "", ""
            try:
                relay_file = _sync_cliproxy_auth(email, token, log, expected_hash=pending_sync["base_hash"])
                if not relay_file:
                    raise RuntimeError("中转同步未返回已写入文件")
                _mark_relay_synced(rec, token)
            except Exception as exc:
                relay_error = str(exc)[:220]
                log(f"授权成功，但中转同步失败: {relay_error}；重试只同步凭据", "warning")
            return {
                "email": email,
                "status": "failed" if relay_error else "success",
                "refresh_token": refresh,
                "expires_in": token.get("expires_in"),
                "error": relay_error,
                "phone": phone_used,
                "logs": list(steps or []),
                "phone_attempts": list(phone_attempts),
                "action": "reauthorize", "authorization_status": "success",
                "relay_status": "failed" if relay_error else "synced", "relay_file": relay_file,
                "retry_action": "sync_only" if relay_error else "",
            }
        except Exception as exc:
            last_error = str(exc)
            if stop_event.is_set() or isinstance(exc, _SmsCancelled):
                raise _SmsCancelled("任务已停止") from exc
            if len(phone_attempts) >= rounds:
                raise
            if attempt < rounds and _proxy_retryable_error(exc):
                log(f"网络请求失败({last_error[:140]})，重试下一轮", "warning")
                continue
            if mfa_code_failures < _MFA_CODE_RETRIES and _mfa_code_error(exc):
                mfa_code_failures += 1
                if mfa_code_failures == 1:
                    # 等到下一个 30s TOTP 窗口,保证取到与上次不同的新码
                    wait = 31 - (int(time.time()) % 30)
                    log(
                        f"2FA 验证码被拒,等 {wait}s 换新验证码自动重试"
                        f"({mfa_code_failures}/{_MFA_CODE_RETRIES})",
                        "warning",
                    )
                    stage("mfa_retry", "等待下一次二次认证窗口")
                    if stop_event.wait(wait):
                        raise _SmsCancelled("任务已停止") from exc
                else:
                    log(
                        f"2FA 验证码被拒,换相邻时间窗口验证码自动重试"
                        f"({mfa_code_failures}/{_MFA_CODE_RETRIES})",
                        "warning",
                    )
                continue
            raise
        finally:
            try:
                client.session.close()
            except Exception:
                pass
    raise RuntimeError(f"取号 {len(phone_attempts)}/{rounds} 次，登录未完成: {last_error[:160]}")


def _run_batch(
    records: list, sms_cfg: dict, concurrency: int = 1,
    batch_id: str = "", stop_event: threading.Event | None = None,
    relay_context: dict | None = None,
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    stop_event = stop_event if stop_event is not None else _STOP
    batch_id = batch_id or str(_STATE.get("batch_id") or "")
    workers = max(1, min(int(concurrency or 1), len(records) or 1))
    context = relay_context if relay_context is not None else _relay_context()
    with _LOCK:
        options = copy.deepcopy(_STATE.get("options") or {})
        if _STATE.get("batch_id", "") == batch_id and _STATE.get("mode") == "codex":
            if not _STATE.get("items"):
                _STATE["items"] = [
                    {
                        "email": str(rec.get("email") or "").strip(), "status": "pending",
                        "stage": "queued", "message": "等待执行", "attempt": 0,
                        "max_phone_attempts": int(sms_cfg.get("max_phone_attempts") or 5),
                        "wait_deadline": None, "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    for rec in records
                ]
            _sms_counts_locked()

    def _handle(rec: dict) -> None:
        with _LOCK:
            if _STATE.get("batch_id", "") != batch_id:
                return
        email = str(rec.get("email") or "").strip()
        steps: list = []
        attempts: list = []
        result: dict = {}
        outcome = "failed"
        action = {"action": "blocked", "relay_status": "missing", "relay_file": ""}
        try:
            if stop_event.is_set():
                raise _SmsCancelled("停止时尚未执行")
            rec = _current_sms_record(rec)
            _set_sms_item(email, batch_id, status="active", stage="preparing", message="正在检查账号")
            issue = _sms_account_issue(rec)
            if issue in ("not_found", "deactivated"):
                raise RuntimeError("选中账号在库中不存在" if issue == "not_found" else "账号已停用，无法接码")
            strategy = (options.get("account_actions") or {}).get(email) or _run_strategy(options.get("strategy"), bool(options.get("force")))
            current_context = _current_relay_context(rec, context)
            action = _relay_action(rec, strategy, current_context)
            if action["action"] == "blocked":
                raise _RelayRequiresAuth(action["reason"])
            if action["action"] == "ready":
                current = current_context["by_file"][action["relay_file"]]
                outcome = "skipped"
                _log(f"{email} | 中转已有匹配完整凭据，未在线验证", batch_id=batch_id)
                steps.append({
                    "t": datetime.now().strftime("%H:%M:%S"), "level": "ok",
                    "m": "中转已有匹配完整凭据，保留中转当前 Token",
                })
                result = {
                    "email": email, "status": "success", "skipped": True,
                    "refresh_token": str(current["record"].get("refresh_token") or ""), "expires_in": None, "error": "", "phone": "",
                    "logs": list(steps), "phone_attempts": [],
                    "action": "ready", "relay_status": "ready", "relay_file": action["relay_file"],
                    "authorization_status": "reused", "retry_action": "",
                }
            elif action["action"] == "sync_only":
                _set_sms_item(email, batch_id, stage="syncing_relay", message="同步已有凭据到中转，无需接码")

                def sync_log(message, level="info"):
                    _log(f"{email} | {message}", level, batch_id=batch_id)
                    steps.append({"t": datetime.now().strftime("%H:%M:%S"), "level": level, "m": str(message)[:300]})

                filename, wrote = _sync_existing_relay(rec, current_context, sync_log)
                outcome = "synced" if wrote else "skipped"
                saved = read_relay_entry(CLIPROXY_AUTHS_DIR / filename)["record"]
                result = {
                    "email": email, "status": "success", "skipped": not wrote,
                    "refresh_token": str(saved.get("refresh_token") or ""), "expires_in": None, "error": "", "phone": "",
                    "logs": list(steps), "phone_attempts": [], "action": "sync_only",
                    "relay_status": "synced" if wrote else "ready", "relay_file": filename,
                    "authorization_status": "reused", "retry_action": "",
                }
            else:
                result = _process_one(
                    rec, sms_cfg, steps, stop_event=stop_event, batch_id=batch_id,
                    phone_attempts=attempts,
                )
                outcome = "success" if result.get("status") == "success" else "failed"
        except Exception as exc:
            outcome = "cancelled" if isinstance(exc, _SmsCancelled) or stop_event.is_set() else "failed"
            result = {
                "email": email, "status": outcome, "refresh_token": "", "expires_in": None,
                "phone": "", "logs": list(steps), "error": str(exc)[:260],
                "phone_attempts": list(attempts),
                "action": action["action"],
                "authorization_status": "failed" if action["action"] == "reauthorize" else "not_started",
                "relay_status": "requires_auth" if isinstance(exc, _RelayRequiresAuth) else "failed",
                "relay_file": action.get("relay_file") or "",
                "retry_action": "reauthorize" if isinstance(exc, _RelayRequiresAuth) or action["action"] == "reauthorize" else "sync_only",
            }
            _log(f"{email} | {'已取消' if outcome == 'cancelled' else '失败'}: {exc}",
                 "warning" if outcome == "cancelled" else "error", batch_id=batch_id)
        finally:
            result.update(batch_id=batch_id, options=copy.deepcopy(options), outcome=outcome)
            message = {
                "success": "已重新授权并同步到中转", "synced": "已有凭据已同步到中转", "skipped": "中转已有匹配凭据", "cancelled": result.get("error") or "已取消",
                "failed": result.get("error") or "执行失败",
            }[outcome]
            _set_sms_item(
                email, batch_id, status=outcome, stage=outcome, message=message,
                attempt=len(attempts), wait_deadline=None,
                **{key: result.get(key) for key in ("action", "relay_status", "relay_file", "authorization_status", "retry_action")},
            )
            with _LOCK:
                if _STATE.get("batch_id", "") == batch_id:
                    if outcome in ("success", "synced", "skipped"):
                        _STATE["success"] += 1  # Old clients count existing RTs as success.
                    elif outcome == "failed":
                        _STATE["failed"] += 1
                    _STATE["results"].append(result)
                    _STATE["done"] += 1
            _append_rt_history(email, {
                "at": datetime.now().isoformat(timespec="seconds"),
                "status": result.get("status", outcome), "outcome": outcome,
                "skipped": outcome == "skipped", "batch_id": batch_id, "options": copy.deepcopy(options),
                "rt": str(result.get("refresh_token") or "")[:600],
                "phone": str(result.get("phone") or ""), "error": result.get("error") or "",
                "logs": _clip_history_logs(result.get("logs") or steps),
                "phone_attempts": list(result.get("phone_attempts") or attempts),
                **{key: result.get(key) for key in ("action", "relay_status", "relay_file", "authorization_status", "retry_action")},
            })

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="codex-sms") as pool:
            futures = [pool.submit(_handle, rec) for rec in records]
            for future in as_completed(futures):
                future.result()
    except Exception as exc:
        _log(f"接码批次异常终止: {exc}", "error", batch_id=batch_id)
    finally:
        _finish_batch(batch_id)

# ---------------------------------------------------------------------------
# SMS provider configuration stored under config.yaml ``sms_code``


class SmsConfigUpdate(BaseModel):
    enabled: bool = False
    provider: str = Field(default="smsbower", max_length=20)
    api_key: str = Field(default="", max_length=200)
    service: str = Field(default="dr", max_length=30)
    # 国家×档位组合串(国家=档位|档位, 多国逗号分隔)远超百字符,
    # 上限放宽到 4000 以容纳大规模取号顺序
    country_order: str = Field(default="6, 4, 151", max_length=4000)
    poll_interval_ms: int = Field(default=5000, ge=1000, le=60000)
    timeout_seconds: int = Field(default=180, ge=30, le=1800)
    max_price: str = Field(default="", max_length=20)
    max_phone_attempts: int = Field(default=5, ge=1)
    firefox_api_name: str = Field(default="", max_length=120)
    firefox_api_password: str = Field(default="", max_length=120)
    firefox_api_token: str = Field(default="", max_length=200)
    firefox_docking_code: str = Field(default="", max_length=120)
    firefox_did: str = Field(default="", max_length=60)
    firefox_item_id: str = Field(default="1096", max_length=20)
    firefox_country_order: str = Field(default="usa, chl, eng", max_length=2000)


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
        metadata = {
            "provider": settings.provider, "provider_label": settings.provider_label,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            balance = check_sms_balance(settings)
            return {
                "ok": True,
                "message": f"{settings.provider_label} 余额: {balance.get('balance', '?')}",
                "balance": balance,
                **metadata,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)[:200], **metadata}

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


def _validate_import_identity(account: dict, candidate: dict) -> dict[str, str]:
    """Bind freshly returned credentials to the requested account before merging."""
    from common.session_export import parse_jwt_payload
    from common.team_workspace import session_identity

    expected_email = str(account.get("email") or "").strip().casefold()
    if not expected_email:
        raise RuntimeError("import_identity_unverified: 缺少待导入账号邮箱")
    payloads = [parse_jwt_payload(str(candidate.get(key) or "")) or {} for key in ("access_token", "id_token")]
    session = candidate.get("session")
    session = session if isinstance(session, dict) else {}
    user = session.get("user")
    user = user if isinstance(user, dict) else {}
    email_values = [user.get("email"), session.get("email"), candidate.get("email")]
    user_values = [user.get("id"), candidate.get("user_id")]
    for payload in payloads:
        profile = payload.get("https://api.openai.com/profile")
        profile = profile if isinstance(profile, dict) else {}
        auth = payload.get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, dict) else {}
        email_values.extend((profile.get("email"), payload.get("email")))
        # OAuth `sub` is a different namespace; compare only ChatGPT user IDs.
        user_values.extend((auth.get("chatgpt_user_id"), auth.get("user_id")))
    emails = {
        str(value).strip().casefold()
        for value in email_values
        if str(value or "").strip()
    }
    if not emails:
        raise RuntimeError("import_identity_unverified: 返回会话缺少可核对的邮箱，未写入账号库")
    if emails != {expected_email}:
        raise RuntimeError("import_identity_email_mismatch: 返回会话邮箱与导入账号不一致，未写入账号库")
    identity = session_identity(candidate)
    expected_user = str(account.get("user_id") or "").strip().casefold()
    users = {str(value).strip().casefold() for value in user_values if str(value or "").strip()}
    if len(users) > 1 or (expected_user and users != {expected_user}):
        raise RuntimeError("import_identity_user_mismatch: 返回会话用户与导入账号不一致，未写入账号库")
    return identity


def _persist_imported_account(
    account: dict, tokens: dict, log
) -> dict:
    """导入账号专用持久化：新账号创建，已有账号更新。

    与重新认证(reauth)的 create=False 不同，导入是新写入，
    必须允许创建记录，否则账号登录成功却不会进库。
    """
    from common.account_health import check_account
    from common.account_store import upsert_account_record
    from common.team_workspace import validate_team_session

    require_team = account.get("import_account_type") == "team"
    selected_workspace_id = str(tokens.get("selected_workspace_id") or "").strip()
    team_identity = None
    if require_team:
        if not selected_workspace_id or tokens.get("workspace_selection_verified") is not True:
            raise RuntimeError("team_workspace_unverified: 未完成团队工作区选择，未写入账号库")
        team_identity = validate_team_session(tokens, selected_workspace_id=selected_workspace_id)
        log(f"Team 导入登录套餐={team_identity['plan_type']}，开始核对测活结果")

    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("协议登录未返回 AT")
    login_identity = _validate_import_identity(account, tokens)
    refreshed = {
        **account,
        "access_token": access_token,
        "refresh_token": str(tokens.get("refresh_token") or "").strip()
        or str(account.get("refresh_token") or ""),
        "id_token": str(tokens.get("id_token") or "").strip(),
        "session_token": str(tokens.get("session_token") or "").strip(),
        "session": tokens.get("session") or account.get("session"),
    }
    if login_identity["user_id"]:
        refreshed["user_id"] = login_identity["user_id"]
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
        "selected_workspace_id",
        "selected_workspace_name",
        "workspace_selection_verified",
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
    _validate_import_identity(
        {"email": account["email"], "user_id": login_identity["user_id"]}, checked,
    )
    if require_team:
        verified = validate_team_session(
            checked,
            selected_workspace_id=selected_workspace_id,
            expected_user_id=team_identity["user_id"],
        )
        log(f"Team 导入测活套餐={checked.get('plan_type') or '?'}，最终工作区套餐={verified['plan_type']}，允许入库")
    return upsert_account_record(checked, _state().ACCOUNTS_FILE, create=True)


def _import_one(
    index: int,
    email: str,
    password: str,
    totp: str,
    workspace: str = "",
    account_type: str = "personal",
    *,
    stop_event: threading.Event | None = None,
    batch_id: str = "",
) -> dict:
    stop_event = stop_event if stop_event is not None else _STOP
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
        _log(f"[{index}] {email} | {message}", level, batch_id=batch_id)

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

    if stop_event.is_set():
        result["error"] = "任务已停止"
        log("任务已停止，账号未执行", "warning")
        result["logs"] = account_logs
        return result

    try:
        from proxy_util import normalize_proxy_url, resolve_proxy_pool
        from register.protocol_runtime import ProtocolRuntimeSettings

        cfg = _state()._read_config()
        proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
        registration = cfg.get("registration") if isinstance(cfg.get("registration"), dict) else {}
        proxies = [normalize_proxy_url(value) for value in resolve_proxy_pool(proxy_cfg)]
        proxies = [value for value in proxies if value]
        if proxy_cfg.get("enabled") and not proxies:
            raise RuntimeError("import_proxy_unavailable: 已启用代理，但没有可用代理，未直连导入")
        proxy = proxies[max(0, index - 1) % len(proxies)] if proxies else ""
        runtime = ProtocolRuntimeSettings.from_config(registration)
        runtime = runtime.with_deadline(time.monotonic() + runtime.account_timeout)
        log("协议登录启动(密码 + TOTP)")
        tokens = login_with_password_totp_one(
            index,
            email=email,
            password=password,
            totp_secret=totp,
            proxy=proxy,
            workspace_name=str(workspace or "").strip(),
            require_team_workspace=account_type == "team",
            stop_check=stop_event.is_set,
            error_sink=error_sink,
            account_log_sink=account_log_sink,
            proxy_config=proxy_cfg,
            protocol_runtime=runtime,
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
            "proxy": proxy,
        }
        # Team is a verified workspace constraint, never a fallback display label.
        if account_type == "team":
            account["import_account_type"] = "team"
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
    batch_id: str = "",
    stop_event: threading.Event | None = None,
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    stop_event = stop_event if stop_event is not None else _STOP
    batch_id = batch_id or str(_STATE.get("batch_id") or "")
    # 并发不设硬上限，实际并行数不超过待导入账号数
    workers = max(1, min(int(concurrency or 1), len(creds) or 1))
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
                    stop_event=stop_event,
                    batch_id=batch_id,
                )
                for index, (email, password, totp) in enumerate(creds, start=1)
            ]
            for future in as_completed(futures):
                result = future.result()
                with _LOCK:
                    if _STATE.get("batch_id", "") == batch_id:
                        _STATE["done"] += 1
                        _STATE["results"].append(result)
                        if result.get("status") == "success":
                            _STATE["success"] += 1
                        else:
                            _STATE["failed"] += 1
                _append_import_history(
                    {
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "batch_id": batch_id,
                        "account_type": account_type,
                        "workspace": workspace,
                        "email": str(result.get("email") or ""),
                        "status": str(result.get("status") or ""),
                        "error": str(result.get("error") or ""),
                        "logs": result.get("logs") or [],
                    }
                )
    except Exception as exc:
        _log(f"导入批次异常终止: {exc}", "error", batch_id=batch_id)
    finally:
        _finish_batch(batch_id)


class CodexOauthImportRequest(BaseModel):
    text: str = Field(default="", max_length=200000)
    # team 账号 MFA 后会进入工作区选择页，需要指定工作区名；个人账号留空。
    # 套餐(free/plus/pro/team)由登录后测活自动识别，不由导入时指定。
    account_type: str = Field(default="personal", max_length=16)
    workspace: str = Field(default="", max_length=128)
    # 同时进行协议登录的账号数(不设上限,实际并发不超过账号数)
    concurrency: int = Field(default=1, ge=1)


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
        batch_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{hashlib.sha256(f'{time.time()}-{len(creds)}'.encode()).hexdigest()[:6]}"
        stop_event = _reserve_batch("import", len(creds), batch_id)
        try:
            _rotate_import_history()
            _launch_batch(
                _run_import_batch,
                (creds, workspace, concurrency, account_type, batch_id, stop_event),
                batch_id=batch_id, stop_event=stop_event, name="codex-oauth-import",
            )
        except Exception:
            stop_event.set()
            _finish_batch(batch_id)
            raise
        type_label = "Team" if account_type == "team" else "个人"
        plan = f"Team 工作区「{workspace}」" if workspace else type_label
        _log(
            f"导入任务启动:{len(creds)} 个账号 · 协议登录(密码+TOTP) · {plan} · 并发 {min(concurrency, len(creds))}",
            batch_id=batch_id,
        )
        return {"ok": True, "total": len(creds)}

    return await asyncio.to_thread(_do)


class SmsRunOptions(BaseModel):
    provider: Literal["smsbower", "firefox"] | None = None
    country_order: str | None = Field(default=None, max_length=4000)
    max_price: str | float | None = Field(default=None)
    max_phone_attempts: int | None = Field(default=None, ge=1, le=50)
    timeout_seconds: int | None = Field(default=None, ge=30, le=1800)


class CodexOauthRunRequest(BaseModel):
    emails: list[str] = Field(default_factory=list, max_length=200)
    # force=True: 忽略账号库已有 RT 全部重跑(中转凭据失效重新登录用)
    force: bool = False
    # 同时进行接码登录的账号数(不设上限,实际并发不超过选中账号数)
    concurrency: int = Field(default=1, ge=1)
    sms_options: SmsRunOptions | None = None
    strategy: Literal["auto", "sync_only", "reauthorize"] | None = None
    account_actions: dict[str, Literal["sync_only", "reauthorize"]] = Field(default_factory=dict, max_length=200)


def _run_strategy(strategy: str | None, force: bool = False) -> str:
    return strategy or ("reauthorize" if force else "auto")


def _current_sms_record(previous: dict) -> dict:
    """Refresh an indexed account snapshot before a queued operation uses it."""
    candidates = _load_sms_batch_records({str(previous.get("email") or "").strip().lower()})
    key = account_delete_key(previous)
    matched = [rec for rec in candidates if account_delete_key(rec) == key]
    if len(matched) != 1:
        raise RuntimeError("账号已删除或身份在等待期间改变，请重新选择账号")
    current = dict(matched[0])
    old_id = session_identity(previous)
    current_id = session_identity(current)
    if old_id["user_id"] and current_id["user_id"] and old_id["user_id"] != current_id["user_id"]:
        raise RuntimeError("账号用户身份在等待期间改变，未继续操作")
    for field in ("selected_workspace_id", "selected_workspace_name", "workspace_name", "import_account_type"):
        if previous.get(field) != current.get(field):
            raise RuntimeError("账号工作区在等待期间改变，请重新选择账号")
    return current


def _normalized_account_actions(wanted: set[str], actions: dict | None, strategy: str) -> dict:
    normalized = {str(email).strip().lower(): action for email, action in (actions or {}).items()}
    if set(normalized) - wanted:
        raise HTTPException(400, "逐账号操作包含未选择的邮箱")
    if any(action not in {"sync_only", "reauthorize"} for action in normalized.values()):
        raise HTTPException(400, "未知逐账号操作")
    if strategy == "sync_only" and "reauthorize" in normalized.values():
        raise HTTPException(400, "仅同步模式不能包含重新授权操作")
    return normalized


def _relay_action(rec: dict, strategy: str, context: dict) -> dict:
    state = relay_state(rec, context)
    action = {"email": rec["email"], "action": "blocked", "reason": "", "relay_status": state["status"], "relay_file": state["file"]}
    issue = _sms_account_issue(rec)
    if issue in ("not_found", "deactivated"):
        action["reason"] = "账号不存在" if issue == "not_found" else "账号已停用"
        return action
    fresh_pending = _fresh_relay_pending(rec)
    relay_record = (context.get("by_file", {}).get(state["file"]) or {}).get("record") or {}
    invalid_same_rt = state["status"] == "invalid" and relay_record.get("refresh_token") == rec.get("refresh_token")
    if strategy != "reauthorize" and state["status"] == "ready" and not fresh_pending:
        return {**action, "action": "ready", "reason": "中转已有完整匹配凭据，未在线验证"}
    reason = "需要重新授权"
    reusable = False
    if strategy != "reauthorize":
        try:
            safe_stored_bundle(rec)
            reusable = True
        except RuntimeError as exc:
            reason = str(exc)
    known_invalid = auth_known_invalid(rec) or state["status"] == "invalid"
    identity = session_identity(rec)
    canonical = _cliproxy_auth_filename(rec["email"], identity["account_id"], identity["plan_type"])
    unsafe_target = state["status"] in ("unverified", "mismatch") and state["file"] == canonical
    if unsafe_target:
        reason = "中转目标的客户端或用户身份无法确认，需要重新授权"
    if strategy != "reauthorize" and reusable and not unsafe_target and not invalid_same_rt and (fresh_pending or not known_invalid):
        return {**action, "action": "sync_only", "reason": "补同步已授权凭据" if fresh_pending else "使用现有 Codex 凭据补齐中转，无需登录或接码"}
    if known_invalid:
        reason = "已有明确认证失效记录，需要重新授权"
    if strategy == "sync_only":
        return {**action, "reason": reason, "relay_status": "requires_auth"}
    if issue == "missing_credentials":
        return {**action, "reason": "需要重新授权，但账号缺少密码或 2FA 密钥", "relay_status": "requires_auth"}
    return {**action, "action": "reauthorize", "reason": "按所选策略重新授权" if strategy == "reauthorize" else reason}


def _effective_sms_config(cfg: dict, options: SmsRunOptions | dict | None = None) -> dict:
    """Copy only supported run settings; credentials always come from saved config."""
    section = dict(_sms_config_section(cfg))
    override = options.model_dump(exclude_none=True) if isinstance(options, SmsRunOptions) else dict(options or {})
    if override.get("provider") is not None:
        if override["provider"] not in {"smsbower", "firefox"}:
            raise HTTPException(400, "未知接码渠道")
        section["provider"] = override["provider"]
    provider = str(section.get("provider") or "smsbower")
    for key in ("max_phone_attempts", "timeout_seconds"):
        if key in override:
            section[key] = override[key]
    if "country_order" in override:
        countries = str(override["country_order"] or "").strip()
        if provider == "firefox":
            dock_country = parse_firefox_docking_code(str(section.get("firefox_docking_code") or ""))[0]
            if countries and countries != dock_country:
                raise HTTPException(400, "火狐狸国家由已保存的对接码固定，请在接码设置中更换对接码")
        else:
            if not countries or not _country_specs({"country_order": countries, "provider": provider}):
                raise HTTPException(400, "请填写有效的国家/档位顺序")
            section["country_order"] = countries
    if "max_price" in override:
        price = str(override["max_price"] if override["max_price"] is not None else "").strip()
        if price:
            try:
                number = float(price)
            except (TypeError, ValueError):
                raise HTTPException(400, "每号最高价必须为正数") from None
            if not math.isfinite(number) or number <= 0:
                raise HTTPException(400, "每号最高价必须为正数")
        if provider == "firefox" and price:
            raise HTTPException(400, "火狐狸对接码接口未确认支持限价，请先在供应商端确认价格")
        section["max_price"] = price
    if provider == "firefox":
        # Do not imply that the supplier enforces an undocumented price field.
        section["max_price"] = ""
    return section


def _safe_sms_settings(sms_cfg: dict) -> dict:
    settings = SmsSettings.from_mapping(sms_cfg)
    provider = settings.provider
    country_order = str(sms_cfg.get("country_order") or "6, 4, 151")
    if provider == "firefox":
        country_order = parse_firefox_docking_code(str(sms_cfg.get("firefox_docking_code") or ""))[0]
    return {
        "provider": provider, "provider_label": settings.provider_label,
        "enabled": settings.enabled, "configured": settings.credentials_configured,
        "country_order": country_order, "max_price": str(sms_cfg.get("max_price") or ""),
        "max_phone_attempts": int(getattr(settings, "max_phone_attempts", sms_cfg.get("max_phone_attempts") or 5)),
        "timeout_seconds": int(getattr(settings, "timeout_seconds", sms_cfg.get("timeout_seconds") or 180)),
        "price_cap_supported": provider == "smsbower",
    }


def _sms_account_issue(rec: dict) -> str:
    from common.account_store import account_is_deactivated

    if rec.get("_sms_not_found"):
        return "not_found"
    if account_is_deactivated(rec):
        return "deactivated"
    if not str(rec.get("password") or "").strip() or not str(rec.get("totp_secret") or "").strip():
        return "missing_credentials"
    return ""


def _sms_preflight(
    wanted: set[str], force: bool, sms_cfg: dict, *, strategy: str | None = None,
    account_actions: dict | None = None, relay_context: dict | None = None,
) -> tuple[dict, list]:
    if not wanted:
        raise HTTPException(400, "未选择账号")
    if len(wanted) > 200:
        raise HTTPException(400, "单批最多选择 200 个账号")
    by_email = {}
    for rec in _load_sms_batch_records(wanted):
        by_email.setdefault(str(rec.get("email") or "").strip().lower(), rec)
    picked = [dict(by_email.get(email) or {"email": email, "_sms_not_found": True}) for email in sorted(wanted)]
    settings = _safe_sms_settings(sms_cfg)
    strategy = _run_strategy(strategy, force)
    actions = _normalized_account_actions(wanted, account_actions, strategy)
    context = relay_context if relay_context is not None else _relay_context()
    payload = {
        "ok": True, "total": len(wanted), "eligible": 0, "skipped": 0,
        "missing_credentials": [], "not_found": [], "deactivated": [], "limit": 200,
        "configured": bool(settings["enabled"] and settings["configured"]),
        "settings": settings, "emails": sorted(wanted),
        "strategy": strategy, "sync_needed": 0, "relay_ready": 0, "reauthorize": 0,
        "relay_configured": bool(context["configured"]), "relay_issues": [], "action_items": [],
    }
    for rec in picked:
        email = str(rec.get("email") or "").strip().lower()
        rec["email"] = email
        issue = _sms_account_issue(rec)
        action = _relay_action(rec, actions.get(email, strategy), context)
        payload["action_items"].append(action)
        if action["relay_status"] != "ready":
            payload["relay_issues"].append({"email": email, "reason": action["reason"], "relay_status": action["relay_status"]})
        if issue in ("not_found", "deactivated"):
            payload[issue].append(email)
        elif action["action"] == "ready":
            payload["skipped"] += 1
            payload["relay_ready"] += 1
        elif action["action"] == "sync_only":
            payload["sync_needed"] += 1
            payload["eligible"] += 1
        elif action["action"] == "reauthorize":
            payload["reauthorize"] += 1
            payload["eligible"] += 1
        elif issue == "missing_credentials":
            payload["missing_credentials"].append({
                "email": email, "missing": [key for key in ("password", "totp_secret") if not str(rec.get(key) or "").strip()],
            })
    payload["sms_required_if_challenged"] = bool(payload["reauthorize"] and not payload["configured"])
    return payload, picked


@router.post("/api/codex-oauth/preflight")
async def codex_oauth_preflight(body: CodexOauthRunRequest):
    def _do():
        wanted = {str(email).strip().lower() for email in body.emails if str(email).strip()}
        sms_cfg = _effective_sms_config(_state()._read_config(), body.sms_options)
        return _sms_preflight(wanted, bool(body.force), sms_cfg, strategy=body.strategy, account_actions=body.account_actions)[0]

    return await asyncio.to_thread(_do)


def _load_sms_batch_records(wanted: set[str]) -> list[dict[str, Any]]:
    """Resolve the selected accounts without decoding the full account table.

    Uses the indexed email loader when the state module exposes it; falls back
    to the full snapshot for test doubles and older state modules.
    """
    state = _state()
    loader = getattr(state, "load_account_records_by_emails", None)
    if callable(loader):
        records = loader(wanted, state.ACCOUNTS_FILE)
    else:  # pragma: no cover - compatibility fallback for state doubles
        records = state.load_accounts(state.ACCOUNTS_FILE)
    return [
        dict(rec)
        for rec in records
        if str(rec.get("email") or "").strip().lower() in wanted
    ]


def _start_sms_batch(
    wanted: set[str], force: bool = False, concurrency: int = 1,
    sms_options: SmsRunOptions | dict | None = None,
    strategy: str | None = None, account_actions: dict | None = None,
) -> dict:
    """按选中邮箱启动接码批次。force 时忽略已有 RT 全部重跑。"""
    sms_cfg = _effective_sms_config(_state()._read_config(), sms_options)
    strategy = _run_strategy(strategy, force)
    actions = _normalized_account_actions(wanted, account_actions, strategy)
    context = _relay_context()
    preflight, picked = _sms_preflight(wanted, force, sms_cfg, strategy=strategy, account_actions=actions, relay_context=context)
    if len(preflight["not_found"]) == len(wanted):
        raise HTTPException(404, "选中账号在库中不存在")
    settings = SmsSettings.from_mapping(sms_cfg)
    batch_id = "codex-" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:16]
    stop_event = _reserve_batch("codex", len(picked), batch_id)
    workers = max(1, min(int(concurrency or 1), len(picked) or 1))
    safe = preflight["settings"]
    options = {
        "force": strategy == "reauthorize", "concurrency": workers,
        "strategy": strategy, "account_actions": actions,
        "sms_options": {key: safe[key] for key in ("provider", "country_order", "max_price", "max_phone_attempts", "timeout_seconds")},
    }
    with _LOCK:
        _STATE.update(options=options, force=options["force"], concurrency=workers)
        _STATE["items"] = [
            {
                "email": rec["email"], "status": "pending", "stage": "queued", "message": "等待执行",
                "attempt": 0, "max_phone_attempts": safe["max_phone_attempts"], "wait_deadline": None,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            for rec in picked
        ]
        _sms_counts_locked()
    _launch_batch(
        _run_batch, (picked, sms_cfg, concurrency, batch_id, stop_event, context),
        batch_id=batch_id, stop_event=stop_event, name="codex-oauth-batch",
    )
    _log(
        f"任务启动:{len(picked)} 个账号 · 接码渠道 {settings.provider_label} · "
        f"并发 {workers} · 本地直连", batch_id=batch_id,
    )
    return {"ok": True, "total": len(picked), "concurrency": workers, "batch_id": batch_id, "options": options}


@router.post("/api/codex-oauth/run")
async def codex_oauth_run(body: CodexOauthRunRequest):
    def _do():
        with _LOCK:
            if _STATE["running"]:
                raise HTTPException(409, "已有接码任务在运行")
        wanted = {str(e).strip().lower() for e in body.emails if str(e).strip()}
        if not wanted:
            raise HTTPException(400, "未选择账号")
        return _start_sms_batch(
            wanted, force=bool(body.force), concurrency=int(body.concurrency or 1), sms_options=body.sms_options,
            strategy=body.strategy, account_actions=body.account_actions,
        )

    return await asyncio.to_thread(_do)


def _historical_phone_attempts(rec: dict) -> list[dict]:
    """Conservative fallback for old log formats, including tiers without prices."""
    assign_re = re.compile(r"已分配\s+(\S+?)\(([^)]*)\)")
    fail_re = re.compile(r"号码\s+(\S+?)\s+失败")
    attempts: list[dict] = []
    by_phone: dict[str, dict] = {}
    latest = None
    for line in rec.get("logs") or []:
        if not isinstance(line, dict):
            continue
        message = str(line.get("m") or "")
        match = assign_re.search(message)
        if match:
            phone, label = match.groups()
            tier_match = re.search(r"\s+#(\d+)", label)
            price_match = re.search(r"\s+\$([0-9.]+)", label)
            try:
                estimate = float(price_match.group(1)) if price_match else None
            except ValueError:
                estimate = None
            latest = {
                "provider": "legacy", "country_label": re.split(r"\s+#|\s+\$", label, maxsplit=1)[0] or "未知",
                "tier": tier_match.group(1) if tier_match else "",
                "estimated_price": estimate, "result": "unknown", "acquired": True,
            }
            attempts.append(latest)
            by_phone[phone] = latest
        match = fail_re.search(message)
        if match:
            phone = match.group(1)
            if phone not in by_phone:
                by_phone[phone] = {
                    "provider": "legacy", "country_label": "未知", "tier": "",
                    "estimated_price": None, "result": "unknown", "acquired": True,
                }
                attempts.append(by_phone[phone])
            by_phone[phone]["result"] = "failed"
        if "手机验证通过" in message and latest:
            latest["result"] = "success"
    phone = str(rec.get("phone") or "")
    if rec.get("status") == "success" and phone:
        if phone in by_phone:
            by_phone[phone]["result"] = "success"
        else:
            attempts.append({
                "provider": "legacy", "country_label": "未知", "tier": "",
                "estimated_price": None, "result": "success", "acquired": True,
            })
    return attempts


@router.get("/api/sms/stats")
async def sms_stats():
    """Aggregate individual attempts; catalogue quotes are not billing records."""
    def _do():
        from collections import defaultdict

        try:
            history = json.loads(_RT_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            history = {}
        attempts = []
        structured = historical = 0
        for records in (history.values() if isinstance(history, dict) else []):
            if not isinstance(records, list):
                continue
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                if isinstance(rec.get("phone_attempts"), list):
                    rows = [dict(row) for row in rec["phone_attempts"] if isinstance(row, dict)]
                    structured += len(rows)
                else:
                    rows = _historical_phone_attempts(rec)
                    historical += len(rows)
                attempts.extend(rows)

        def blank():
            return {"total": 0, "ok": 0, "fail": 0, "cancelled": 0, "unknown": 0,
                    "unpriced": 0, "cost": 0.0, "cost_is_estimate": True}

        countries, tiers = defaultdict(blank), defaultdict(blank)
        summary = blank()
        for attempt in attempts:
            country = str(attempt.get("country_label") or attempt.get("country") or "未知")
            tier = str(attempt.get("tier") or "")
            provider = {"firefox": "火狐狸", "smsbower": "SMSBower", "legacy": "历史"}.get(
                str(attempt.get("provider") or ""), "未知渠道",
            )
            country_key = f"{provider} · {country}"
            tier_key = f"{country_key} #{tier}" if tier else f"{country_key} · 未指定档位"
            raw_price = attempt.get("estimated_price")
            try:
                price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                price = None
            if price is not None and (not math.isfinite(price) or price < 0):
                price = None
            result = str(attempt.get("result") or "unknown")
            for row in (summary, countries[country_key], tiers[tier_key]):
                row["total"] += 1
                if result == "success":
                    row["ok"] += 1
                elif result in ("failed", "acquire_failed"):
                    row["fail"] += 1
                elif result == "cancelled":
                    row["cancelled"] += 1
                else:
                    row["unknown"] += 1
                if price is None:
                    row["unpriced"] += 1
                else:
                    row["cost"] += price
            tiers[tier_key]["price"] = price

        def with_rate(row):
            return {
                **row, "rate": round(row["ok"] / row["total"] * 100, 1) if row["total"] else 0.0,
                "cost": round(row["cost"], 6),
            }

        return {
            "ok": True, "cost_is_estimate": True, "billing_source": "catalogue_quotes_only",
            "summary": {
                "total": summary["total"], "success": summary["ok"], "failed": summary["fail"],
                "cancelled": summary["cancelled"], "unknown": summary["unknown"], "unpriced": summary["unpriced"],
                "rate": with_rate(summary)["rate"], "structured": structured, "historical": historical,
            },
            "countries": [
                {"name": name, **with_rate(data)}
                for name, data in sorted(countries.items(), key=lambda pair: -pair[1]["total"])
            ],
            "tiers": [
                {"name": name, **with_rate(data)}
                for name, data in sorted(tiers.items(), key=lambda pair: -pair[1]["total"])
            ],
        }

    return await asyncio.to_thread(_do)


@router.get("/api/codex-oauth/status")
async def codex_oauth_status():
    with _LOCK:
        payload = copy.deepcopy(_STATE)
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
                by_email = {}
                try:
                    state = _state()
                    loader = getattr(state, "load_account_records_by_emails", None)
                    if callable(loader):
                        # Indexed lookup: only decode the requested rows.
                        accounts = loader(missing, state.ACCOUNTS_FILE)
                    else:  # pragma: no cover - compatibility fallback
                        accounts = (
                            state._load_accounts()
                            if hasattr(state, "_load_accounts")
                            else []
                        )
                except Exception:
                    accounts = []
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


@router.get("/api/codex-oauth/import-history")
async def codex_oauth_import_history(limit: int = 200):
    """Return persisted per-account import logs, newest first.

    Each entry is one imported account: batch metadata plus the full login
    log lines captured for that account, so the panel can offer a per-account
    history view even after the live batch state has been replaced.
    """

    def _do():
        cap = max(1, min(int(limit or 200), 1000))
        if not _IMPORT_HISTORY_PATH.is_file():
            return {"ok": True, "entries": []}
        try:
            lines = _IMPORT_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {"ok": True, "entries": []}
        entries: list = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
            if len(entries) >= cap:
                break
        return {"ok": True, "entries": entries}

    return await asyncio.to_thread(_do)


@router.post("/api/codex-oauth/stop")
async def codex_oauth_stop():
    with _LOCK:
        batch_id = str(_STATE.get("batch_id") or "")
        running = bool(_STATE.get("running"))
        if running:
            _STOP.set()
            _STATE["status"] = "stopping"
    if running:
        _log("已请求停止，正在结束当前请求并取消未执行账号", "warning", batch_id=batch_id)
    return {"ok": True, "batch_id": batch_id, "status": "stopping" if running else "idle"}
