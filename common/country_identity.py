# -*- coding: utf-8 -*-
"""Shared locale and clock identities for proxy-country fingerprinting."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CountryIdentity:
    locale: str
    accept_language: str
    timezone: str
    timezone_offsets: tuple[int, ...]


COUNTRY_IDENTITIES: dict[str, CountryIdentity] = {
    "US": CountryIdentity("en-US", "en-US,en;q=0.9", "America/New_York", (240, 300)),
    "JP": CountryIdentity("ja-JP", "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.6", "Asia/Tokyo", (-540,)),
    "PH": CountryIdentity("en-PH", "en-PH,en;q=0.9,fil;q=0.8", "Asia/Manila", (-480,)),
    "TH": CountryIdentity("th-TH", "th-TH,th;q=0.9,en-US;q=0.7,en;q=0.6", "Asia/Bangkok", (-420,)),
    "AU": CountryIdentity("en-AU", "en-AU,en;q=0.9", "Australia/Sydney", (-600, -660)),
    "BR": CountryIdentity("pt-BR", "pt-BR,pt;q=0.9,en-US;q=0.7,en;q=0.6", "America/Sao_Paulo", (180, 240)),
    "SG": CountryIdentity("en-SG", "en-SG,en;q=0.9,zh-SG;q=0.7", "Asia/Singapore", (-480,)),
    "KR": CountryIdentity("ko-KR", "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6", "Asia/Seoul", (-540,)),
    "TR": CountryIdentity("tr-TR", "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Istanbul", (-180,)),
    "GB": CountryIdentity("en-GB", "en-GB,en;q=0.9,en-US;q=0.8", "Europe/London", (0, -60)),
    "DE": CountryIdentity("de-DE", "de-DE,de;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Berlin", (-60, -120)),
    "FR": CountryIdentity("fr-FR", "fr-FR,fr;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Paris", (-60, -120)),
    "CA": CountryIdentity("en-CA", "en-CA,en;q=0.9,fr-CA;q=0.7", "America/Toronto", (240, 300, 360, 420)),
    "VN": CountryIdentity("vi-VN", "vi-VN,vi;q=0.9,en-US;q=0.7,en;q=0.6", "Asia/Ho_Chi_Minh", (-420,)),
    "IN": CountryIdentity("en-IN", "en-IN,en;q=0.9,hi-IN;q=0.8,hi;q=0.7", "Asia/Kolkata", (-330,)),
    "IT": CountryIdentity("it-IT", "it-IT,it;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Rome", (-60, -120)),
    "ES": CountryIdentity("es-ES", "es-ES,es;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Madrid", (-60, -120)),
    "NL": CountryIdentity("nl-NL", "nl-NL,nl;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Amsterdam", (-60, -120)),
    "CH": CountryIdentity("de-CH", "de-CH,de;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Zurich", (-60, -120)),
    "SE": CountryIdentity("sv-SE", "sv-SE,sv;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Stockholm", (-60, -120)),
    "NO": CountryIdentity("nb-NO", "nb-NO,no;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Oslo", (-60, -120)),
    "DK": CountryIdentity("da-DK", "da-DK,da;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Copenhagen", (-60, -120)),
    "FI": CountryIdentity("fi-FI", "fi-FI,fi;q=0.9,sv-FI;q=0.8,en-US;q=0.7,en;q=0.6", "Europe/Helsinki", (-120, -180)),
    "AT": CountryIdentity("de-AT", "de-AT,de;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Vienna", (-60, -120)),
    "BE": CountryIdentity("nl-BE", "nl-BE,nl;q=0.9,fr-BE;q=0.8,en-US;q=0.7,en;q=0.6", "Europe/Brussels", (-60, -120)),
    "IE": CountryIdentity("en-IE", "en-IE,en;q=0.9", "Europe/Dublin", (0, -60)),
    "PT": CountryIdentity("pt-PT", "pt-PT,pt;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Lisbon", (0, -60)),
    "PL": CountryIdentity("pl-PL", "pl-PL,pl;q=0.9,en-US;q=0.7,en;q=0.6", "Europe/Warsaw", (-60, -120)),
    "NZ": CountryIdentity("en-NZ", "en-NZ,en;q=0.9", "Pacific/Auckland", (-720, -780)),
}


def normalize_country_code(value: object) -> str:
    code = str(value or "").strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else ""


def country_code_from_proxy(proxy: object) -> str:
    value = str(proxy or "").upper()
    match = re.search(r"(?:REGION-|[_-]G-)([A-Z]{2})(?:[_-]|$)", value)
    return normalize_country_code(match.group(1)) if match else ""


def country_identity(country_code: object) -> CountryIdentity | None:
    return COUNTRY_IDENTITIES.get(normalize_country_code(country_code))
