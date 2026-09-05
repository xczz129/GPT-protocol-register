"""Pure-HTTP SMS provider clients used by the local import queue.

The provider deliberately lives behind this module so the OAuth worker only
needs three operations: reserve a number, wait for a code, and finish/cancel
the activation.  SMSBower and Firefox/火狐狸 implement the same small
interface.  No browser automation or provider SDK is required.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SMSBOWER_API = "https://smsbower.page/stubs/handler_api.php"
SMSBOWER_UA = "codex-oauth-local/1.0"
FIREFOX_API = "https://www.firefox.fun/yhapi.ashx"
FIREFOX_UA = "codex-oauth-local/1.0"
SMS_PROVIDERS = ("smsbower", "firefox")

# Firefox.fun's country table (``PagCountry``) uses its own short country
# identifiers rather than ISO country codes.  ``Country_Area`` in that table
# is the international calling code.  Keep the table local so phone
# normalisation still works when the provider response omits field 5, which
# happens for a few number pools.
#
# The table was synced from https://firefox.fun/api/init.ashx (act=PagCountry)
# on 2026-08-29.  Values are strings because they are sent as URL/query
# components and may be one, two, or three digits.
FIREFOX_COUNTRY_CALLING_CODES: dict[str, str] = {
    "can": "1",
    "dom": "1",
    "pri": "1",
    "usa": "1",
    "rus": "7",
    "kaz": "7",
    "egy": "20",
    "zaf": "27",
    "grc": "30",
    "nld": "31",
    "bel": "32",
    "fra": "33",
    "esp": "34",
    "ita": "39",
    "rou": "40",
    "che": "41",
    "aut": "43",
    "eng": "44",
    "dnk": "45",
    "swe": "46",
    "pol": "48",
    "deu": "49",
    "per": "51",
    "mex": "52",
    "cub": "53",
    "arg": "54",
    "bra": "55",
    "chl": "56",
    "col": "57",
    "ven": "58",
    "mys": "60",
    "aus": "61",
    "idn": "62",
    "phl": "63",
    "nzl": "64",
    "sgp": "65",
    "tha": "66",
    "jpn": "81",
    "kor": "82",
    "vnm": "84",
    "tur": "90",
    "ind": "91",
    "pak": "92",
    "afg": "93",
    "lka": "94",
    "mmr": "95",
    "irn": "98",
    "ssd": "211",
    "mar": "212",
    "dza": "213",
    "tun": "216",
    "lby": "218",
    "gmb": "220",
    "sen": "221",
    "mrt": "222",
    "mli": "223",
    "gin": "224",
    "civ": "225",
    "bfa": "226",
    "tgo": "228",
    "mus": "230",
    "lbr": "231",
    "sle": "232",
    "gha": "233",
    "nga": "234",
    "tcd": "235",
    "caf": "236",
    "cmr": "237",
    "cpv": "238",
    "gab": "241",
    "cog": "242",
    "cod": "243",
    "ago": "244",
    "gnb": "245",
    "sdn": "249",
    "eth": "251",
    "som": "252",
    "ken": "254",
    "tza": "255",
    "uga": "256",
    "bdi": "257",
    "moz": "258",
    "zmb": "260",
    "mdg": "261",
    "zwe": "263",
    "mwi": "265",
    "lso": "266",
    "bwa": "267",
    "com": "269",
    "prt": "351",
    "irl": "353",
    "cyp": "357",
    "bgr": "359",
    "ltu": "370",
    "lva": "371",
    "est": "372",
    "arm": "374",
    "blr": "375",
    "mco": "377",
    "ukr": "380",
    "srb": "381",
    "xkk": "383",
    "hrv": "385",
    "svn": "386",
    "bih": "387",
    "mkd": "389",
    "cze": "420",
    "gtm": "502",
    "hnd": "504",
    "nic": "505",
    "pan": "507",
    "bol": "591",
    "mtq": "596",
    "ury": "598",
    "tls": "670",
    "brn": "673",
    "png": "675",
    "fji": "679",
    "niu": "683",
    "wsm": "685",
    "hkg": "852",
    "mac": "853",
    "khm": "855",
    "lao": "856",
    "bgd": "880",
    "twn": "886",
    "mdv": "960",
    "lbn": "961",
    "jor": "962",
    "syr": "963",
    "irq": "964",
    "kwt": "965",
    "sau": "966",
    "yem": "967",
    "are": "971",
    "isr": "972",
    "btn": "975",
    "mng": "976",
    "npl": "977",
    "tjk": "992",
    "aze": "994",
    "geo": "995",
    "kgz": "996",
    "uzb": "998",
}

# English slugs exposed by ``PagCountry.Country_Title``.  Keeping these
# alongside the provider IDs makes the normalizer useful when an operator
# pastes a country name from the Firefox UI (not just ``eng``/``usa``).  The
# values are deliberately compact because the API itself uses these slugs in
# its documentation and examples.
FIREFOX_COUNTRY_NAMES: dict[str, str] = {
    "can": "canada",
    "dom": "dominican",
    "pri": "puertorico",
    "usa": "usa",
    "rus": "russia",
    "kaz": "kazakhstan",
    "egy": "egypt",
    "zaf": "southafrica",
    "grc": "greece",
    "nld": "netherlands",
    "bel": "belgium",
    "fra": "france",
    "esp": "spain",
    "ita": "italy",
    "rou": "romania",
    "che": "switzerland",
    "aut": "austria",
    "eng": "england",
    "dnk": "denmark",
    "swe": "sweden",
    "pol": "poland",
    "deu": "germany",
    "per": "peru",
    "mex": "mexico",
    "cub": "cuba",
    "arg": "argentina",
    "bra": "brazil",
    "chl": "chile",
    "col": "colombia",
    "ven": "venezuela",
    "mys": "malaysia",
    "aus": "australia",
    "idn": "indonesia",
    "phl": "philippines",
    "nzl": "newzealand",
    "sgp": "singapore",
    "tha": "thailand",
    "jpn": "japan",
    "kor": "southkorea",
    "vnm": "vietnam",
    "tur": "turkey",
    "ind": "india",
    "pak": "pakistan",
    "afg": "afghanistan",
    "lka": "srilanka",
    "mmr": "myanmar",
    "irn": "iran",
    "ssd": "southsudan",
    "mar": "morocco",
    "dza": "algeria",
    "tun": "tunisia",
    "lby": "libyan",
    "gmb": "gambia",
    "sen": "senegal",
    "mrt": "mauritania",
    "mli": "mali",
    "gin": "guinea",
    "civ": "ivory",
    "bfa": "burkinafaso",
    "tgo": "togo",
    "mus": "mauritius",
    "lbr": "liberia",
    "sle": "sierraleone",
    "gha": "ghana",
    "nga": "nigeria",
    "tcd": "chad",
    "caf": "caf",
    "cmr": "cameroon",
    "cpv": "capeverde",
    "gab": "gabon",
    "cog": "congo",
    "cod": "zaire",
    "ago": "angola",
    "gnb": "guineabissau",
    "sdn": "sudan",
    "eth": "ethiopia",
    "som": "somalia",
    "ken": "kenya",
    "tza": "tanzania",
    "uga": "uganda",
    "bdi": "burundi",
    "moz": "mozambique",
    "zmb": "zambia",
    "mdg": "madagascar",
    "zwe": "zimbabwe",
    "mwi": "malawi",
    "lso": "lesotho",
    "bwa": "botswana",
    "com": "comoros",
    "prt": "portugal",
    "irl": "ireland",
    "cyp": "cyprus",
    "bgr": "bulgaria",
    "ltu": "lithuania",
    "lva": "latvia",
    "est": "estonia",
    "arm": "armenia",
    "blr": "belarus",
    "mco": "monaco",
    "ukr": "ukraine",
    "srb": "serbia",
    "xkk": "kosovo",
    "hrv": "croatia",
    "svn": "slovenia",
    "bih": "bosnia",
    "mkd": "northmacedonia",
    "cze": "czech",
    "gtm": "guatemala",
    "hnd": "honduras",
    "nic": "nicaragua",
    "pan": "panama",
    "bol": "bolivia",
    "mtq": "martinique",
    "ury": "uruguay",
    "tls": "timorleste",
    "brn": "brunei",
    "png": "papua",
    "fji": "fiji",
    "niu": "niue",
    "wsm": "samoa",
    "hkg": "hongkong",
    "mac": "macao",
    "khm": "cambodia",
    "lao": "laos",
    "bgd": "bangladesh",
    "twn": "taiwan",
    "mdv": "maldives",
    "lbn": "lebanon",
    "jor": "jordan",
    "syr": "syrian",
    "irq": "iraq",
    "kwt": "kuwait",
    "sau": "saudiarabia",
    "yem": "yemen",
    "are": "uae",
    "isr": "israel",
    "btn": "bhutan",
    "mng": "mongolia",
    "npl": "nepal",
    "tjk": "tajikistan",
    "aze": "azerbaijan",
    "geo": "georgia",
    "kgz": "kyrgyzstan",
    "uzb": "uzbekistan",
}

# Common aliases are accepted in the web form and in a docking code.  The
# provider itself still receives the canonical Firefox identifier above.
FIREFOX_COUNTRY_ALIASES: dict[str, str] = {
    "gb": "eng",
    "gbr": "eng",
    "uk": "eng",
    "unitedkingdom": "eng",
    "england": "eng",
    "us": "usa",
    "usa": "usa",
    "unitedstates": "usa",
    "unitedstatesofamerica": "usa",
    "ca": "can",
    "canada": "can",
    "cl": "chl",
    "chile": "chl",
    "au": "aus",
    "australia": "aus",
    "de": "deu",
    "germany": "deu",
    "fr": "fra",
    "france": "fra",
    "es": "esp",
    "spain": "esp",
    "it": "ita",
    "italy": "ita",
    "nl": "nld",
    "netherlands": "nld",
    "be": "bel",
    "belgium": "bel",
    "at": "aut",
    "austria": "aut",
    "dk": "dnk",
    "denmark": "dnk",
    "se": "swe",
    "sweden": "swe",
    "pl": "pol",
    "poland": "pol",
    "br": "bra",
    "brazil": "bra",
    "ar": "arg",
    "argentina": "arg",
    "mx": "mex",
    "mexico": "mex",
    "jp": "jpn",
    "japan": "jpn",
    "kr": "kor",
    "southkorea": "kor",
    "in": "ind",
    "india": "ind",
    "id": "idn",
    "indonesia": "idn",
    "ph": "phl",
    "philippines": "phl",
    "nz": "nzl",
    "newzealand": "nzl",
    "sg": "sgp",
    "singapore": "sgp",
    "th": "tha",
    "thailand": "tha",
    "vn": "vnm",
    "vietnam": "vnm",
    "tr": "tur",
    "turkey": "tur",
    "my": "mys",
    "malaysia": "mys",
    "hk": "hkg",
    "hongkong": "hkg",
    "mo": "mac",
    "macao": "mac",
    "tw": "twn",
    "taiwan": "twn",
    "za": "zaf",
    "southafrica": "zaf",
    "eg": "egy",
    "egypt": "egy",
    "ng": "nga",
    "nigeria": "nga",
    "ua": "ukr",
    "ukraine": "ukr",
    "cz": "cze",
    "czech": "cze",
}

# Add every English title from the provider table while preserving the more
# specific aliases above (for example ``uk`` and ``unitedkingdom``).  This is
# intentionally done at import time from local data; no country-table request
# is needed on the OAuth worker's critical path.
for _country_id, _country_name in FIREFOX_COUNTRY_NAMES.items():
    _compact_name = re.sub(r"[\s._-]+", "", _country_name.strip().lower())
    if _compact_name:
        FIREFOX_COUNTRY_ALIASES.setdefault(_compact_name, _country_id)


def normalize_firefox_country(value: Any) -> str:
    """Return a canonical Firefox.fun country identifier.

    Firefox accepts identifiers such as ``eng`` and ``usa``.  Operators often
    paste ISO-2 names (``GB``/``US``) or a display name, so normalize those
    forms before constructing a request.  Unknown alphabetic identifiers are
    retained in lowercase to keep the client forward-compatible with a new
    provider country added after this table was synced.
    """
    raw = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not raw:
        return ""
    compact = re.sub(r"[\s._-]+", "", raw)
    direct = FIREFOX_COUNTRY_ALIASES.get(compact)
    if direct:
        return direct
    if compact in FIREFOX_COUNTRY_CALLING_CODES:
        return compact

    # ``PagCountry`` normally returns the short Country_ID, but some helper
    # endpoints/UI fields pass the display title instead (for example
    # ``+44/英格兰/england``).  Walk each slash/pipe-delimited component and
    # prefer a recognizable code/name.  Keeping this fallback here means the
    # request builder and phone normalizer share exactly the same rules.
    for component in re.split(r"[/|,;]+", raw):
        candidate = re.sub(r"[\s._-]+", "", component.strip().lower())
        if not candidate:
            continue
        alias = FIREFOX_COUNTRY_ALIASES.get(candidate)
        if alias:
            return alias
        if candidate in FIREFOX_COUNTRY_CALLING_CODES:
            return candidate
    return compact


def firefox_country_calling_code(value: Any) -> str:
    """Resolve a Firefox country code to its international calling code.

    ``value`` may be a canonical code, a common alias, or an already supplied
    numeric calling code.  An empty string means the local table has no entry;
    callers can then use the provider's response field as a fallback.
    """
    normalized = normalize_firefox_country(value)
    if normalized in FIREFOX_COUNTRY_CALLING_CODES:
        return FIREFOX_COUNTRY_CALLING_CODES[normalized]
    # Accept an already supplied international prefix in the common ``+44``
    # and ``0044`` forms.  This is only used for phone normalization; country
    # parameters sent to Firefox remain the canonical Country_ID strings.
    digits = re.sub(r"\D", "", normalized)
    if digits:
        digits = digits.lstrip("0")
    if digits and 1 <= len(digits) <= 3:
        return digits
    return ""


def parse_firefox_docking_code(value: Any) -> tuple[str, str, str]:
    """Parse a Firefox docking code into ``country``, ``iid`` and ``did``.

    The platform displays codes in the form ``eng-1096-DEMO01``.  The first
    component is its country code, the second is the project/item ID, and the
    final component is the developer/docking ID sent as the ``did`` parameter
    to ``getPhone``.  Invalid input returns empty components so callers can
    fall back to the explicit advanced fields.
    """
    raw = str(value or "").strip().replace("－", "-").replace("–", "-")
    parts = raw.split("-", 2)
    if len(parts) != 3:
        return "", "", ""
    country, item_id, did = (part.strip() for part in parts)
    country = normalize_firefox_country(country)
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", country):
        return "", "", ""
    if not re.fullmatch(r"\d+", item_id):
        return "", "", ""
    if not re.fullmatch(r"[a-zA-Z0-9_]+", did):
        return "", "", ""
    return country, item_id, did


def _mask_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    return value[:3] + "…" + value[-4:] if len(value) > 8 else "••••"


class SmsError(RuntimeError):
    """Provider error suitable for a job log."""


@dataclass(frozen=True)
class SmsSettings:
    enabled: bool = False
    api_key: str = ""
    service: str = "dr"
    country_order: tuple[int, ...] = (6, 4, 151)
    poll_interval_ms: int = 5000
    timeout_seconds: int = 180
    max_price: str = ""
    max_phone_attempts: int = 5
    # The fields below are appended so existing positional SmsSettings(...)
    # calls remain compatible with the SMSBower-only version.
    provider: str = "smsbower"
    firefox_api_name: str = ""
    firefox_api_password: str = ""
    firefox_item_id: str = "1096"
    firefox_country_order: tuple[str, ...] = ("usa", "chl", "eng")
    firefox_api_url: str = FIREFOX_API
    firefox_docking_code: str = ""
    firefox_did: str = ""
    firefox_api_token: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> "SmsSettings":
        if not isinstance(value, dict):
            value = {}
        provider = str(value.get("provider") or "smsbower").strip().lower()
        if provider in {"firefox.fun", "firefox_sms", "huohuli", "火狐狸"}:
            provider = "firefox"
        if provider not in SMS_PROVIDERS:
            provider = "smsbower"
        raw_smsbower_key = value.get("api_key")
        if not raw_smsbower_key and provider != "firefox":
            # Keep the legacy SMSBower ``token`` alias; for Firefox the same
            # field is its already-issued API token and is parsed separately.
            raw_smsbower_key = value.get("token")
        raw_countries = value.get("country_order", (6, 4, 151))
        if isinstance(raw_countries, str):
            raw_countries = re.split(r"[,，\s]+", raw_countries.strip())
        countries: list[int] = []
        if isinstance(raw_countries, (list, tuple)):
            for item in raw_countries:
                # Panel entries can carry per-country annotations
                # (``6=1170|3237`` tier priority, ``4:0.2`` price ceiling);
                # only the leading country id matters for this fallback.
                text = re.split(r"[=|:@]", str(item).strip(), 1)[0].strip()
                try:
                    number = int(text)
                except (TypeError, ValueError):
                    continue
                if number > 0 and number not in countries:
                    countries.append(number)
        if not countries:
            countries = [6, 4, 151]
        docking_code = str(
            value.get("firefox_docking_code")
            or value.get("docking_code")
            or value.get("Docking_Code")
            or value.get("dock_code")
            or ""
        ).strip()
        dock_country, dock_item_id, dock_did = parse_firefox_docking_code(docking_code)
        raw_firefox_countries = value.get("firefox_country_order")
        if raw_firefox_countries is None:
            raw_firefox_countries = (dock_country,) if dock_country else ("usa", "chl", "eng")
        if isinstance(raw_firefox_countries, str):
            raw_firefox_countries = re.split(r"[,，\s]+", raw_firefox_countries.strip())
        firefox_countries: list[str] = []
        if isinstance(raw_firefox_countries, (list, tuple)):
            for item in raw_firefox_countries:
                country = normalize_firefox_country(item)
                if country and country not in firefox_countries:
                    firefox_countries.append(country)
        if not firefox_countries:
            firefox_countries = ["usa", "chl", "eng"]
        try:
            poll = max(1000, min(60000, int(value.get("poll_interval_ms", 5000))))
        except (TypeError, ValueError):
            poll = 5000
        try:
            timeout = max(30, min(1800, int(value.get("timeout_seconds", 180))))
        except (TypeError, ValueError):
            timeout = 180
        try:
            attempts = max(1, int(value.get("max_phone_attempts", 5)))
        except (TypeError, ValueError):
            attempts = 5
        raw_enabled = value.get("enabled", False)
        if isinstance(raw_enabled, str):
            enabled = raw_enabled.strip().lower() in {"1", "true", "yes", "on", "启用"}
        else:
            enabled = bool(raw_enabled)
        firefox_api_url = str(value.get("firefox_api_url") or FIREFOX_API).strip() or FIREFOX_API
        if not firefox_api_url.startswith(("http://", "https://")):
            firefox_api_url = FIREFOX_API
        return cls(
            enabled=enabled,
            api_key=str(raw_smsbower_key or "").strip(),
            service=str(value.get("service") or "dr").strip() or "dr",
            country_order=tuple(countries),
            poll_interval_ms=poll,
            timeout_seconds=timeout,
            max_price=str(value.get("max_price") or "").strip(),
            max_phone_attempts=attempts,
            provider=provider,
            firefox_api_name=str(
                value.get("firefox_api_name")
                or value.get("firefox_username")
                or value.get("api_name")
                or value.get("ApiName")
                or ""
            ).strip(),
            firefox_api_password=str(
                value.get("firefox_api_password")
                or value.get("firefox_password")
                or value.get("api_password")
                or value.get("PassWord")
                or ""
            ).strip(),
            firefox_item_id=str(value.get("firefox_item_id") or value.get("item_id") or dock_item_id or "1096").strip() or "1096",
            firefox_country_order=tuple(firefox_countries),
            firefox_api_url=firefox_api_url,
            firefox_docking_code=docking_code,
            firefox_did=str(value.get("firefox_did") or value.get("did") or dock_did or "").strip(),
            firefox_api_token=str(
                value.get("firefox_api_token")
                or value.get("firefox_token")
                or (value.get("token") if provider == "firefox" else "")
                or ""
            ).strip(),
        )

    @property
    def credentials_configured(self) -> bool:
        """Whether the selected provider has the credentials it needs."""
        if self.provider == "firefox":
            has_auth = bool(self.firefox_api_token or (self.firefox_api_name and self.firefox_api_password))
            # 火狐狸只走对接码(对接码一次锁定国家+项目),不再回退国家列表。
            dock = parse_firefox_docking_code(self.firefox_docking_code)
            return has_auth and bool(dock[0] and dock[1] and dock[2])
        return bool(self.api_key)

    @property
    def provider_label(self) -> str:
        return "火狐狸" if self.provider == "firefox" else "SMSBower"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "api_key": self.api_key,
            "service": self.service,
            "country_order": list(self.country_order),
            "poll_interval_ms": self.poll_interval_ms,
            "timeout_seconds": self.timeout_seconds,
            "max_price": self.max_price,
            "max_phone_attempts": self.max_phone_attempts,
            "provider": self.provider,
            "firefox_api_name": self.firefox_api_name,
            "firefox_api_password": self.firefox_api_password,
            "firefox_item_id": self.firefox_item_id,
            "firefox_country_order": list(self.firefox_country_order),
            "firefox_api_url": self.firefox_api_url,
            "firefox_docking_code": self.firefox_docking_code,
            "firefox_did": self.firefox_did,
            "firefox_api_token": self.firefox_api_token,
        }

    def public_mapping(self) -> dict[str, Any]:
        masked = _mask_secret(self.api_key)
        configured = self.credentials_configured
        if self.enabled and configured:
            state = "active"
            state_label = f"{self.provider_label} 自动接码已启用"
        elif configured:
            state = "disabled"
            state_label = f"{self.provider_label} 已配置但未启用"
        else:
            state = "unconfigured"
            state_label = f"未配置 {self.provider_label} 凭证"
        firefox_name = self.firefox_api_name
        return {
            "enabled": self.enabled,
            "configured": configured,
            "state": state,
            "state_label": state_label,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "service": self.service,
            "country_order": list(self.country_order),
            "poll_interval_ms": self.poll_interval_ms,
            "timeout_seconds": self.timeout_seconds,
            "max_price": self.max_price,
            "max_phone_attempts": self.max_phone_attempts,
            "api_key_masked": masked,
            "has_api_key": bool(self.api_key),
            "firefox_api_name": firefox_name,
            "has_firefox_credentials": bool(self.firefox_api_name and self.firefox_api_password),
            "firefox_item_id": self.firefox_item_id,
            "firefox_country_order": list(self.firefox_country_order),
            "firefox_api_url": self.firefox_api_url,
            "firefox_docking_code": self.firefox_docking_code,
            "firefox_did": self.firefox_did,
            "firefox_api_token_masked": _mask_secret(self.firefox_api_token),
            "has_firefox_token": bool(self.firefox_api_token),
        }


@dataclass
class SmsActivation:
    activation_id: str
    phone: str
    country: int | str
    api_key: str
    finished: bool = False
    # 面板日志展示用:国家名称/档位价格(如 "印度尼西亚 #1170 $0.107")。
    label: str = ""


class SmsBowerClient:
    """SMSBower ``handler_api.php`` protocol wrapper."""

    def __init__(self, settings: SmsSettings, *, timeout: float = 30.0):
        self.settings = settings
        self.timeout = max(3.0, float(timeout))

    def _request(self, action: str, **params: Any) -> str:
        if not self.settings.api_key:
            raise SmsError("未配置 SMSBower API key")
        query: dict[str, str] = {
            "api_key": self.settings.api_key,
            "action": action,
        }
        query.update({key: str(value) for key, value in params.items() if value is not None and str(value) != ""})
        request = Request(
            SMSBOWER_API + "?" + urlencode(query),
            headers={"User-Agent": SMSBOWER_UA, "Accept": "text/plain, */*"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:220]
            raise SmsError(f"SMSBower HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise SmsError(f"SMSBower 连接失败: {getattr(exc, 'reason', exc)}") from exc
        if not body:
            raise SmsError("SMSBower 返回空响应")
        if body.startswith(("BAD_", "NO_", "ERROR", "WRONG_", "EXPIRED")):
            raise SmsError(f"SMSBower {body[:180]}")
        return body

    def balance(self) -> str:
        return self._request("getBalance")

    def acquire(self) -> SmsActivation:
        last_error = "没有可用号码"
        for country in self.settings.country_order:
            params: dict[str, Any] = {
                "service": self.settings.service,
                "country": country,
            }
            if self.settings.max_price:
                params["maxPrice"] = self.settings.max_price
            try:
                body = self._request("getNumber", **params)
            except SmsError as exc:
                last_error = str(exc)
                continue
            if not body.startswith("ACCESS_NUMBER:"):
                last_error = f"SMSBower 未分配号码: {body[:120]}"
                continue
            parts = body.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                last_error = f"SMSBower 响应格式异常: {body[:120]}"
                continue
            return SmsActivation(parts[1], parts[2], int(country), self.settings.api_key)
        raise SmsError(last_error)

    def wait_code(self, activation: SmsActivation, *, cancel_event: Any = None) -> str:
        """Wait for a code without closing the activation.

        The caller must invoke :meth:`finish` only after the upstream service
        accepts the code; this leaves a failed OAuth validation eligible for a
        retry and lets timeout/cancellation use :meth:`cancel`.
        """
        deadline = time.monotonic() + self.settings.timeout_seconds
        interval = self.settings.poll_interval_ms / 1000.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self.cancel(activation)
                raise SmsError("任务已取消")
            body = self._request("getStatus", id=activation.activation_id)
            if body.startswith("STATUS_OK:"):
                code = body.split(":", 1)[1].strip()
                if not code:
                    raise SmsError("SMSBower 返回空验证码")
                # Do not finish the activation yet.  The upstream OAuth
                # endpoint still has to accept the code.  Marking it as
                # finished here makes a bad/expired code impossible to retry
                # and can consume a number before it was actually used.
                return code
            if body.startswith("STATUS_CANCEL") or body.startswith("STATUS_FINISH"):
                raise SmsError(f"SMSBower 激活已结束: {body[:120]}")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY are normal polling states.
            if cancel_event is not None:
                cancel_event.wait(interval)
            else:
                time.sleep(interval)
        self.cancel(activation)
        raise SmsError("等待短信验证码超时")

    def finish(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            self._request("setStatus", id=activation.activation_id, status="6")
        except SmsError:
            # The code has already been obtained; a provider acknowledgement
            # failure should not make the OAuth result look unsuccessful.
            pass
        finally:
            activation.finished = True

    def cancel(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            self._request("setStatus", id=activation.activation_id, status="8")
        except SmsError:
            pass
        finally:
            activation.finished = True


class FirefoxSmsClient:
    """火狐狸 ``yhapi.ashx`` protocol wrapper.

    The platform uses a long-lived API token obtained from ``login``.  A
    successful ``getPhone`` response carries a ``pkey``; that pkey is the
    activation identifier used by ``getPhoneCode``, ``apiReturn`` and
    ``setRel``.  The public API is deliberately shaped like
    :class:`SmsBowerClient` so the OAuth queue can switch providers without
    changing its state machine.
    """

    def __init__(self, settings: SmsSettings, *, timeout: float = 30.0):
        self.settings = settings
        self.timeout = max(3.0, float(timeout))
        # The web UI accepts the persistent ``我的密钥(token)`` directly, so
        # a login request is skipped when it is already present.  ApiName and
        # password remain a fallback for older configurations.
        self._token = settings.firefox_api_token

    def _request(self, action: str, **params: Any) -> str:
        query: dict[str, str] = {"act": action}
        query.update({key: str(value) for key, value in params.items() if value is not None and str(value) != ""})
        request = Request(
            self.settings.firefox_api_url + "?" + urlencode(query),
            headers={"User-Agent": FIREFOX_UA, "Accept": "text/plain, application/json, */*"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:220]
            raise SmsError(f"火狐狸 HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise SmsError(f"火狐狸连接失败: {getattr(exc, 'reason', exc)}") from exc
        if not body:
            raise SmsError("火狐狸返回空响应")
        return body

    @staticmethod
    def _failure(body: str) -> str:
        return body.split("|", 1)[1].strip() if "|" in body else body.strip()

    @staticmethod
    def _normalize_phone(value: Any, prefix: Any = "", country: Any = "") -> str:
        """Convert Firefox's phone field to the E.164 form OpenAI expects.

        ``getPhone`` returns two separate fields for many countries: the
        number in ``parts[7]`` can be a local number while ``parts[4]`` is the
        country calling code.  Passing that local value directly to
        ``/api/accounts/add-phone/send`` produces ``invalid_phone_number``
        (the exact 400 seen in the task log).  Some inventories instead return
        a number that already includes the calling code, or an ``00`` prefix;
        all of those forms are handled here without duplicating the prefix.
        If field 5 is empty, ``country`` is resolved through the provider's
        country table, for example ``eng -> 44`` and ``usa -> 1``.
        """
        raw = str(value or "").strip()
        # Keep only the number's meaningful characters.  Provider responses
        # occasionally include spaces, parentheses or a display hyphen.
        phone = re.sub(r"[^0-9+]", "", raw)
        if phone.startswith("00"):
            phone = "+" + phone[2:]
        elif not phone.startswith("+"):
            digits = re.sub(r"\D", "", phone).lstrip("0")
            country_prefix = re.sub(r"\D", "", str(prefix or "")).lstrip("0")
            if not country_prefix:
                country_prefix = firefox_country_calling_code(country)
            if not country_prefix:
                raise SmsError(
                    f"火狐狸国家代码 {normalize_firefox_country(country) or '未知'} 缺少对应国际区号"
                )
            if country_prefix and digits.startswith(country_prefix):
                phone = "+" + digits
            elif country_prefix:
                phone = "+" + country_prefix + digits
            else:
                phone = "+" + digits
        # E.164 permits at most 15 digits after the leading plus.  OpenAI's
        # endpoint also rejects very short values, so fail before making a
        # billable/visible OAuth request and let the caller rotate the number.
        if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
            raise SmsError("火狐狸返回的手机号格式无效")
        return phone

    def _login(self, *, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        if not self.settings.firefox_api_name or not self.settings.firefox_api_password:
            raise SmsError("未配置火狐狸 ApiName/PassWord")
        body = self._request(
            "login",
            ApiName=self.settings.firefox_api_name,
            PassWord=self.settings.firefox_api_password,
        )
        if not body.startswith("1|"):
            raise SmsError(f"火狐狸登录失败：{self._failure(body)[:120]}")
        token = body.split("|", 1)[1].strip()
        if not token:
            raise SmsError("火狐狸登录返回空 token")
        self._token = token
        return token

    def _authorized_request(self, action: str, **params: Any) -> str:
        """Call an authenticated action and retry once on an expired token."""
        for attempt in range(2):
            token = self._login(force=attempt > 0)
            body = self._request(action, token=token, **params)
            if not body.startswith("0|-2"):
                return body
            self._token = ""
        raise SmsError("火狐狸 token 已失效")

    def balance(self) -> str:
        return self._authorized_request("myInfo")

    def acquire(self) -> SmsActivation:
        last_error = "没有可用号码"
        dock_country, dock_item_id, dock_did = parse_firefox_docking_code(self.settings.firefox_docking_code)
        if dock_country and dock_item_id and dock_did:
            countries = (dock_country,)
            item_id = dock_item_id
            did = dock_did
        else:
            countries = self.settings.firefox_country_order or ("",)
            item_id = self.settings.firefox_item_id
            did = self.settings.firefox_did
        # The provider explicitly asks callers to log in once at startup;
        # cache the token before trying the configured country sequence so a
        # bad credential is not retried once per country.
        self._login()
        for country in countries:
            country = normalize_firefox_country(country)
            params: dict[str, Any] = {
                "iid": item_id,
                "country": country,
                "dock": "0",
                "otpmode": "sms",
            }
            if did:
                params["did"] = did
            if self.settings.max_price:
                params["maxPrice"] = self.settings.max_price
            try:
                body = self._authorized_request("getPhone", **params)
            except SmsError as exc:
                last_error = str(exc)
                continue
            if not body.startswith("1|"):
                last_error = f"火狐狸未分配号码：{self._failure(body)[:120]}"
                continue
            parts = body.split("|")
            # 1|pkey|提取时间|国家代码|国家区号|归属地|端口号|手机号|对接码
            if len(parts) < 8 or not parts[1].strip() or not parts[7].strip():
                last_error = f"火狐狸响应格式异常：{body[:160]}"
                continue
            # The documented response is
            # ``1|pkey|time|country|country_calling_code|area|port|phone|...``.
            # ``phone`` is not guaranteed to include the calling code; always
            # normalize with the separate prefix before handing it to OAuth.
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

    def wait_code(self, activation: SmsActivation, *, cancel_event: Any = None) -> str:
        """Poll ``getPhoneCode`` until a code arrives or the activation ends."""
        deadline = time.monotonic() + self.settings.timeout_seconds
        interval = self.settings.poll_interval_ms / 1000.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self.cancel(activation)
                raise SmsError("任务已取消")
            body = self._authorized_request("getPhoneCode", pkey=activation.activation_id)
            if body.startswith("1|"):
                parts = body.split("|", 2)
                code = parts[1].strip() if len(parts) > 1 else ""
                if not code:
                    raise SmsError("火狐狸返回空验证码")
                return code
            failure = self._failure(body)
            if failure == "-3" or "等待验证码" in failure:
                if cancel_event is not None:
                    cancel_event.wait(interval)
                else:
                    time.sleep(interval)
                continue
            raise SmsError(f"火狐狸获取验证码失败：{failure[:120]}")
        self.cancel(activation)
        raise SmsError("等待火狐狸短信验证码超时")

    def finish(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            # Firefox charges/settles a successful number through apiReturn;
            # setRel is only for numbers that did not receive a code.
            body = self._authorized_request("apiReturn", pkey=activation.activation_id, remark="0")
            if not body.startswith("1|"):
                raise SmsError(f"火狐狸成功回执失败：{self._failure(body)[:120]}")
        except SmsError:
            # The OAuth code was already accepted; do not turn a provider
            # acknowledgement glitch into a failed account import.
            pass
        finally:
            activation.finished = True

    def cancel(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            self._authorized_request("setRel", pkey=activation.activation_id)
        except SmsError:
            pass
        finally:
            activation.finished = True


def parse_balance_response(provider: str, body: Any) -> dict[str, str]:
    """Normalize a provider balance response without exposing credentials.

    SMSBower returns ``ACCESS_BALANCE:<amount>`` while Firefox returns
    ``1|<balance>|<level>|<points>``.  The dashboard only needs the useful
    fields, so the original response is deliberately not returned to the
    browser.
    """
    raw = str(body or "").strip()
    if provider == "firefox":
        parts = raw.split("|")
        if parts and parts[0] == "1" and len(parts) >= 2 and parts[1].strip():
            result = {"balance": parts[1].strip()}
            if len(parts) >= 3 and parts[2].strip():
                result["level"] = parts[2].strip()
            if len(parts) >= 4 and parts[3].strip():
                result["points"] = parts[3].strip()
            return result
        failure = parts[1].strip() if len(parts) > 1 else "响应格式异常"
        failure = {
            "-1": "Token 不存在",
            "-2": "token 已失效，请重新登录",
            "-3": "余额接口每 60 秒只能查询一次，请稍后再试",
        }.get(failure, failure)
        raise SmsError(f"火狐狸余额查询失败：{failure[:120]}")
    prefix = "ACCESS_BALANCE:"
    if raw.startswith(prefix) and raw[len(prefix):].strip():
        return {"balance": raw[len(prefix):].strip()}
    raise SmsError(f"SMSBower 余额查询失败：{raw[:120] or '响应为空'}")


def check_sms_balance(settings: SmsSettings, *, timeout: float = 30.0) -> dict[str, str]:
    """Check the selected provider using the server-side private settings."""
    if not settings.credentials_configured:
        raise SmsError(f"未配置 {settings.provider_label} 凭证")
    if settings.provider == "firefox":
        client: Any = FirefoxSmsClient(settings, timeout=timeout)
    else:
        client = SmsBowerClient(settings, timeout=timeout)
    return parse_balance_response(settings.provider, client.balance())


def load_sms_settings(path: str | Any) -> SmsSettings:
    from pathlib import Path

    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SmsError(f"读取接码配置失败: {exc}") from exc
    return SmsSettings.from_mapping(data)


def save_sms_settings(path: str | Any, settings: SmsSettings) -> None:
    from pathlib import Path
    import os
    import tempfile

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".smsbower-", suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings.as_mapping(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
