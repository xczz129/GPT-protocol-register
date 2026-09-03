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

# SMSBower country id -> (english name, chinese name, calling code).
# The id space follows the sms-activate family shared by SMSBower; calling
# codes let the picker show the +44-style prefix before a number is bought.
SMSBOWER_COUNTRY_INFO: dict[int, tuple[str, str, str]] = {
    0: ("Russia", "俄罗斯", "+7"), 1: ("Ukraine", "乌克兰", "+380"),
    2: ("Kazakhstan", "哈萨克斯坦", "+77"), 3: ("China", "中国", "+86"),
    4: ("Philippines", "菲律宾", "+63"), 5: ("Myanmar", "缅甸", "+95"),
    6: ("Indonesia", "印度尼西亚", "+62"), 7: ("Malaysia", "马来西亚", "+60"),
    8: ("Kenya", "肯尼亚", "+254"), 9: ("Tanzania", "坦桑尼亚", "+255"),
    10: ("Vietnam", "越南", "+84"), 11: ("Kyrgyzstan", "吉尔吉斯斯坦", "+996"),
    12: ("USA", "美国", "+1"), 13: ("Israel", "以色列", "+972"),
    14: ("Hong Kong", "香港", "+852"), 15: ("Poland", "波兰", "+48"),
    16: ("England", "英国", "+44"), 17: ("Madagascar", "马达加斯加", "+261"),
    18: ("Congo DR", "刚果(金)", "+243"), 19: ("Egypt", "埃及", "+20"),
    20: ("India", "印度", "+91"), 21: ("Ireland", "爱尔兰", "+353"),
    22: ("Cambodia", "柬埔寨", "+855"), 23: ("Laos", "老挝", "+856"),
    24: ("Haiti", "海地", "+509"), 25: ("Ivory Coast", "科特迪瓦", "+225"),
    26: ("Gambia", "冈比亚", "+220"), 27: ("Serbia", "塞尔维亚", "+381"),
    28: ("Yemen", "也门", "+967"), 29: ("South Africa", "南非", "+27"),
    30: ("Romania", "罗马尼亚", "+40"), 31: ("Colombia", "哥伦比亚", "+57"),
    32: ("Estonia", "爱沙尼亚", "+372"), 33: ("Azerbaijan", "阿塞拜疆", "+994"),
    34: ("Canada", "加拿大", "+1"), 35: ("Morocco", "摩洛哥", "+212"),
    36: ("Ghana", "加纳", "+233"), 37: ("Argentina", "阿根廷", "+54"),
    38: ("Uzbekistan", "乌兹别克斯坦", "+998"), 39: ("Cameroon", "喀麦隆", "+237"),
    40: ("Chad", "乍得", "+235"), 41: ("Germany", "德国", "+49"),
    42: ("Lithuania", "立陶宛", "+370"), 43: ("Croatia", "克罗地亚", "+385"),
    44: ("Sweden", "瑞典", "+46"), 45: ("Iraq", "伊拉克", "+964"),
    46: ("Netherlands", "荷兰", "+31"), 47: ("Latvia", "拉脱维亚", "+371"),
    48: ("Austria", "奥地利", "+43"), 49: ("Belarus", "白俄罗斯", "+375"),
    50: ("Thailand", "泰国", "+66"), 51: ("Saudi Arabia", "沙特阿拉伯", "+966"),
    52: ("Mexico", "墨西哥", "+52"), 54: ("Eswatini", "斯威士兰", "+268"),
    55: ("Liberia", "利比里亚", "+231"), 56: ("Slovakia", "斯洛伐克", "+421"),
    57: ("Slovenia", "斯洛文尼亚", "+386"), 58: ("Bangladesh", "孟加拉国", "+880"),
    59: ("Senegal", "塞内加尔", "+221"), 60: ("Trinidad & Tobago", "特立尼达和多巴哥", "+1868"),
    61: ("Czech Republic", "捷克", "+420"), 62: ("Uganda", "乌干达", "+256"),
    63: ("Spain", "西班牙", "+34"), 64: ("Italy", "意大利", "+39"),
    66: ("France", "法国", "+33"), 68: ("Belgium", "比利时", "+32"),
    69: ("Bulgaria", "保加利亚", "+359"), 70: ("Hungary", "匈牙利", "+36"),
    71: ("Moldova", "摩尔多瓦", "+373"), 72: ("Switzerland", "瑞士", "+41"),
    73: ("Georgia", "格鲁吉亚", "+995"), 74: ("Armenia", "亚美尼亚", "+374"),
    76: ("Greece", "希腊", "+30"), 78: ("Mongolia", "蒙古", "+976"),
    79: ("Brazil", "巴西", "+55"), 80: ("Peru", "秘鲁", "+51"),
    81: ("Turkey", "土耳其", "+90"), 82: ("Nigeria", "尼日利亚", "+234"),
    83: ("Chile", "智利", "+56"), 84: ("Venezuela", "委内瑞拉", "+58"),
    85: ("Pakistan", "巴基斯坦", "+92"), 86: ("Ethiopia", "埃塞俄比亚", "+251"),
    87: ("Mozambique", "莫桑比克", "+258"), 88: ("Burkina Faso", "布基纳法索", "+226"),
    89: ("Togo", "多哥", "+228"), 90: ("Guinea", "几内亚", "+224"),
    91: ("Ivory", "科特迪瓦", "+225"), 92: ("Sudan", "苏丹", "+249"),
    93: ("Zambia", "赞比亚", "+260"), 94: ("Zimbabwe", "津巴布韦", "+263"),
    95: ("Mali", "马里", "+223"), 96: ("Mauritius", "毛里求斯", "+230"),
    97: ("Liberia", "利比里亚", "+231"), 98: ("Sierra Leone", "塞拉利昂", "+232"),
    99: ("Chad", "乍得", "+235"), 101: ("Guinea-Bissau", "几内亚比绍", "+245"),
    102: ("Congo", "刚果", "+242"), 103: ("Angola", "安哥拉", "+244"),
    104: ("Sri Lanka", "斯里兰卡", "+94"), 105: ("Nepal", "尼泊尔", "+977"),
    106: ("Ireland", "爱尔兰", "+353"), 107: ("Portugal", "葡萄牙", "+351"),
    108: ("Cyprus", "塞浦路斯", "+357"), 109: ("Bulgaria", "保加利亚", "+359"),
    110: ("Lithuania", "立陶宛", "+370"), 111: ("Serbia", "塞尔维亚", "+381"),
    112: ("Kosovo", "科索沃", "+383"), 113: ("Bosnia", "波黑", "+387"),
    114: ("North Macedonia", "北马其顿", "+389"), 115: ("Czech", "捷克", "+420"),
    116: ("Guatemala", "危地马拉", "+502"), 117: ("Honduras", "洪都拉斯", "+504"),
    118: ("Nicaragua", "尼加拉瓜", "+505"), 119: ("Panama", "巴拿马", "+507"),
    120: ("Bolivia", "玻利维亚", "+591"), 121: ("Martinique", "马提尼克", "+596"),
    122: ("Uruguay", "乌拉圭", "+598"), 123: ("Timor-Leste", "东帝汶", "+670"),
    124: ("Brunei", "文莱", "+673"), 125: ("Papua", "巴布亚新几内亚", "+675"),
    126: ("Fiji", "斐济", "+679"), 127: ("Niue", "纽埃", "+683"),
    128: ("Samoa", "萨摩亚", "+685"), 129: ("Maldives", "马尔代夫", "+960"),
    130: ("Lebanon", "黎巴嫩", "+961"), 131: ("Jordan", "约旦", "+962"),
    132: ("Syria", "叙利亚", "+963"), 133: ("Iraq", "伊拉克", "+964"),
    134: ("Kuwait", "科威特", "+965"), 135: ("Saudi", "沙特", "+966"),
    136: ("Yemen", "也门", "+967"), 137: ("UAE", "阿联酋", "+971"),
    138: ("Israel", "以色列", "+972"), 139: ("Bhutan", "不丹", "+975"),
    140: ("Tajikistan", "塔吉克斯坦", "+992"), 141: ("Azerbaijan", "阿塞拜疆", "+994"),
    142: ("Georgia", "格鲁吉亚", "+995"), 143: ("Kyrgyzstan", "吉尔吉斯斯坦", "+996"),
    144: ("Uzbekistan", "乌兹别克斯坦", "+998"), 145: ("Taiwan", "台湾", "+886"),
    146: ("Maldives", "马尔代夫", "+960"), 147: ("Jordan", "约旦", "+962"),
    148: ("Bosnia", "波黑", "+387"), 149: ("Albania", "阿尔巴尼亚", "+355"),
    150: ("Iceland", "冰岛", "+354"), 151: ("Indonesia", "印度尼西亚", "+62"),
    152: ("Bosnia", "波黑", "+387"), 153: ("Lithuania", "立陶宛", "+370"),
    154: ("Denmark", "丹麦", "+45"), 155: ("Norway", "挪威", "+47"),
    156: ("Finland", "芬兰", "+358"), 157: ("Greenland", "格陵兰", "+299"),
    158: ("Luxembourg", "卢森堡", "+352"), 159: ("Malta", "马耳他", "+356"),
    160: ("Monaco", "摩纳哥", "+377"), 161: ("Andorra", "安道尔", "+376"),
    162: ("Liechtenstein", "列支敦士登", "+423"), 163: ("San Marino", "圣马力诺", "+378"),
    164: ("Algeria", "阿尔及利亚", "+213"), 165: ("Tunisia", "突尼斯", "+216"),
    166: ("Libya", "利比亚", "+218"), 167: ("Gambia", "冈比亚", "+220"),
    168: ("Benin", "贝宁", "+229"), 169: ("Niger", "尼日尔", "+227"),
    170: ("Burundi", "布隆迪", "+257"), 171: ("Rwanda", "卢旺达", "+250"),
    172: ("Djibouti", "吉布提", "+253"), 173: ("Eritrea", "厄立特里亚", "+291"),
    174: ("Somalia", "索马里", "+252"), 175: ("Kenya", "肯尼亚", "+254"),
    176: ("Tanzania", "坦桑尼亚", "+255"), 177: ("Uganda", "乌干达", "+256"),
    178: ("Lesotho", "莱索托", "+266"), 179: ("Botswana", "博茨瓦纳", "+267"),
    180: ("Malawi", "马拉维", "+265"), 181: ("Comoros", "科摩罗", "+269"),
    182: ("Mauritania", "毛里塔尼亚", "+222"), 183: ("Mali", "马里", "+223"),
    184: ("Cape Verde", "佛得角", "+238"), 185: ("Gabon", "加蓬", "+241"),
    186: ("Congo", "刚果", "+242"), 187: ("Zaire", "扎伊尔", "+243"),
    188: ("Central African", "中非", "+236"), 189: ("Equatorial Guinea", "赤道几内亚", "+240"),
    190: ("South Sudan", "南苏丹", "+211"), 191: ("Afghanistan", "阿富汗", "+93"),
    192: ("Sri Lanka", "斯里兰卡", "+94"), 193: ("Iran", "伊朗", "+98"),
    194: ("Palestine", "巴勒斯坦", "+970"), 195: ("Oman", "阿曼", "+968"),
    196: ("Qatar", "卡塔尔", "+974"), 197: ("Bahrain", "巴林", "+973"),
    198: ("Kazakhstan", "哈萨克斯坦", "+77"), 199: ("Mongolia", "蒙古", "+976"),
}


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
            import json as _json

            countries = _json.loads(countries_raw) if countries_raw.startswith("{") else {}
            prices = _json.loads(prices_raw) if prices_raw.startswith("{") else {}
            out = []
            for country_id, info in (countries or {}).items():
                if not isinstance(info, dict):
                    continue
                cid = int(info.get("id") or country_id)
                eng = str(info.get("eng") or info.get("rus") or f"Country {cid}")
                chn = str(info.get("chn") or "")
                meta = SMSBOWER_COUNTRY_INFO.get(cid)
                calling = meta[2] if meta else ""
                display = chn if chn else eng
                if meta and chn:
                    display = chn
                price_info = (prices.get(str(cid)) or {}).get("dr") or {}
                count = int(price_info.get("count") or 0) if isinstance(price_info, dict) else 0
                price = str(price_info.get("cost") or "") if isinstance(price_info, dict) else ""
                out.append(
                    {"id": str(cid), "name": display, "calling": calling, "count": count, "price": price}
                )
            out.sort(key=lambda row: (-row["count"], row["name"]))
            return {"ok": True, "provider": "smsbower", "items": out}
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


def _import_one(email: str, password: str, totp: str) -> dict:
    # Import uses the panel's own protocol login (email -> password -> TOTP ->
    # chatgpt.com session), the same engine as the protocol-reauth page.  No
    # SMS and no Codex OAuth PKCE flow are involved.
    from register.protocol import login_with_password_totp_one
    from web.routes.security_ops import _persist_reauthenticated_account

    def log(message: str, level: str = "info") -> None:
        _log(f"{email} | {message}", level)

    log("协议登录启动(密码 + TOTP)")
    tokens = login_with_password_totp_one(
        1,
        email=email,
        password=password,
        totp_secret=totp,
        proxy="",
        stop_check=lambda: _STOP.is_set(),
    )
    if not tokens or not str(tokens.get("access_token") or "").strip():
        raise RuntimeError("协议登录未取得访问凭证")
    account = {
        "email": email,
        "password": password,
        "password_set": True,
        "totp_secret": totp,
        "mfa_enabled": True,
        "mfa_factor_type": "totp",
        "registration_method": "protocol",
        "source_type": "import",
        "registration_state": "imported",
        "proxy": "",
    }
    saved = _persist_reauthenticated_account(account, tokens)
    at = str(saved.get("access_token") or tokens.get("access_token") or "")
    log("协议登录成功,账号已写入账号库", "ok")
    return {
        "email": email,
        "status": "success",
        "refresh_token": at,
        "token_kind": "at",
        "expires_in": saved.get("expires_at") or None,
        "error": "",
    }


def _run_import_batch(creds: list) -> None:
    try:
        for email, password, totp in creds:
            if _STOP.is_set():
                _log("任务已停止(剩余账号跳过)", "warning")
                break
            try:
                result = _import_one(email, password, totp)
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
            args=(creds,),
            name="codex-oauth-import",
            daemon=True,
        ).start()
        _log(f"导入任务启动:{len(creds)} 个账号 · 协议登录(密码+TOTP)")
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
