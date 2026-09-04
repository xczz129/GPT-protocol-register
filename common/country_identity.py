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
