"""协议层浏览器指纹体系：UA / 设备 / 显卡 / 语言 / 时区特征与指纹组合池。"""

from __future__ import annotations

import os
import random
import re
import secrets
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from common.country_identity import COUNTRY_IDENTITIES, country_code_from_proxy, country_identity

from register.protocol_context import auth_base


REGISTER_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    # --- Firefox（长线稳定频道） ---
    {"impersonate": "firefox147", "browser_family": "firefox", "major": "147", "full_version": "147.0", "weight": "5"},
    {"impersonate": "firefox144", "browser_family": "firefox", "major": "144", "full_version": "144.0", "weight": "3"},
    # --- Chrome（2026-09 版本带：真实 stable 为 151；147+ 的 TLS/H2 指纹与 146
    #     一致（curl_cffi 上游仅在指纹变化时新增目标），故 impersonate 保持
    #     chrome146、仅抬升 UA / Client Hints 版本，属上游官方认可的跳版做法） ---
    {"impersonate": "chrome146", "browser_family": "chrome", "major": "151", "full_version": "151.0.0.0", "weight": "6"},
    {"impersonate": "chrome146", "browser_family": "chrome", "major": "150", "full_version": "150.0.0.0", "weight": "4"},
    {"impersonate": "chrome146", "browser_family": "chrome", "major": "146", "full_version": "146.0.0.0", "weight": "2"},
    {"impersonate": "chrome145", "browser_family": "chrome", "major": "145", "full_version": "145.0.0.0", "weight": "1"},
    {"impersonate": "chrome142", "browser_family": "chrome", "major": "142", "full_version": "142.0.0.0", "weight": "1"},
    {"impersonate": "chrome136", "browser_family": "chrome", "major": "136", "full_version": "136.0.0.0", "weight": "1"},
)

# 每国 Chrome/Firefox 家族权重（chrome_w, firefox_w），贴近真实市占：
# 全球 Chrome 绝对主导，日本/德语区 Firefox 份额偏高。
# 本地数据佐证：注册资格率 Chrome 65% vs Firefox 47%（JP: 68% vs 51%）。
REGISTER_COUNTRY_BROWSER_WEIGHTS: dict[str, tuple[int, int]] = {
    "JP": (75, 25), "DE": (75, 25), "AT": (78, 22), "CH": (80, 20),
    "VN": (95, 5), "TH": (95, 5), "BR": (95, 5), "PH": (95, 5),
    "US": (95, 5), "GB": (90, 10), "KR": (98, 2), "SG": (95, 5),
    "AU": (95, 5), "CA": (95, 5), "IN": (95, 5), "TR": (92, 8),
    "IT": (92, 8), "ES": (92, 8), "FR": (92, 8), "NL": (92, 8),
    "SE": (92, 8), "NO": (92, 8), "DK": (92, 8), "FI": (92, 8), "BE": (92, 8),
}
DEFAULT_COUNTRY_BROWSER_WEIGHTS: tuple[int, int] = (92, 8)


def _validate_browser_profiles(
    profiles: tuple[dict[str, str], ...] = REGISTER_BROWSER_PROFILES,
) -> None:
    """Fail early when curl_cffi cannot impersonate a configured profile."""
    try:
        from curl_cffi.requests.impersonate import BrowserType
    except Exception as exc:  # pragma: no cover - environment-specific import failure
        raise RuntimeError("curl_cffi impersonation profiles are unavailable") from exc
    supported = {str(item.value) for item in BrowserType}
    missing = sorted(
        {
            str(profile.get("impersonate") or "").strip()
            for profile in profiles
            if str(profile.get("impersonate") or "").strip() not in supported
        }
    )
    if missing:
        raise RuntimeError(
            "Unsupported curl_cffi impersonation profile(s): " + ", ".join(missing)
        )




_validate_browser_profiles()



REGISTER_DEVICE_TRAITS: tuple[dict[str, str], ...] = (
    # 通用兜底设备池（未覆盖国家使用，组合全部换新）
    {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
    {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
    {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1366x768", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1707x960", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.125", "platform_version": "10.0.0"},
    {"screen": "1280x720", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "10.0.0"},
    {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
    {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1440x900", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
)


REGISTER_FALLBACK_WEBGL_TRAITS: tuple[dict[str, str], ...] = (
    # 中性兜底显卡池（未覆盖国家使用，不再借用日本池）
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
)


REGISTER_COUNTRY_DEVICE_TRAITS: dict[str, tuple[dict[str, str], ...]] = {
    "US": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "JP": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1707x960", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.125", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "PH": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "TH": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    ),
    "AU": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "BR": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "SG": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1600", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
    ),
    "KR": (
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "TR": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    ),
    "GB": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
    ),
    "DE": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "FR": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    ),
    "CA": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "VN": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    ),
    "IN": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    ),
    "NL": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "CH": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "SE": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "NO": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "DK": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "AT": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "FI": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "IT": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "ES": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "BE": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "IE": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "PL": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    ),
    "PT": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
    ),
    "NZ": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "13.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "13.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
}


REGISTER_COUNTRY_WEBGL_TRAITS: dict[str, tuple[dict[str, str], ...]] = {
    "US": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "JP": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Plus Graphics 655 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce MX450 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4050 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "PH": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 570 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "TH": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "AU": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "BR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "SG": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4050 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600M Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "KR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "TR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "GB": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "DE": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "FR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "CA": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "VN": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "IN": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce MX450 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "NL": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "CH": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "SE": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "NO": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "DK": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "AT": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "FI": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "IT": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "ES": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "BE": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "IE": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "PL": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "PT": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 530 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "NZ": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
}


# 兼容别名（register/protocol.py 仍按旧名字导入）
REGISTER_JAPAN_WEBGL_TRAITS: tuple[dict[str, str], ...] = REGISTER_COUNTRY_WEBGL_TRAITS["JP"]
REGISTER_US_WEBGL_TRAITS: tuple[dict[str, str], ...] = REGISTER_COUNTRY_WEBGL_TRAITS["US"]


REGISTER_LANGUAGE_PROFILES: tuple[str, ...] = (
    "en-US,en;q=0.9",
    "en-US,en;q=0.9",
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.8",
    "en-GB,en;q=0.9",
)


# 真实有头浏览器环境参数池（存储配额 / 网络信息）
REGISTER_STORAGE_QUOTA_GB: tuple[str, ...] = (
    "96", "118", "137", "154", "199", "237", "265", "316", "421", "512",
)
REGISTER_STORAGE_USAGE_MB: tuple[str, ...] = ("84", "152", "233", "311", "408", "487")
REGISTER_CONNECTION_RTT: tuple[str, ...] = ("50", "75", "100")
REGISTER_CONNECTION_DOWNLINK: tuple[str, ...] = ("10", "9.55", "8.35", "6.2", "4.85")



COUNTRY_TIMEZONE_OFFSETS: dict[str, tuple[int, ...]] = {
    country: identity.timezone_offsets
    for country, identity in COUNTRY_IDENTITIES.items()
}




def _firefox_user_agent(major: str, full_version: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"rv:{full_version} Gecko/20100101 Firefox/{full_version}"
    )




def _firefox_sec_ch_ua(major: str) -> str:
    # Firefox does not send Chromium client-hint headers.  Keep this empty so
    # callers can remove legacy sec-ch-* fields from request headers.
    return ""




def _firefox_sec_ch_ua_full_version_list(major: str, full_version: str) -> str:
    return ""


# Chrome GREASE 品牌顺序与 Not-A-Brand 变体随大版本漂移；值取自真实浏览器抓包。
# 150/151 取自 aleixrodriala/curl-impersonate 真实抓包（profiles/chrome/chrome15x_windows.json，
# consumer 发行版 5 样本，含 fingerprint_digest 溯源）。
_CHROME_SEC_CH_UA: dict[str, str] = {
    "124": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "131": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "136": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "142": '"Google Chrome";v="142", "Chromium";v="142", "Not:A-Brand";v="99"',
    "146": '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"',
    "150": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "151": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
}


def _chrome_user_agent(major: str, full_version: str) -> str:
    # Chrome 121+ 从 UA 中移除次版本号，固定四段 0 填充（如 131.0.0.0）。
    version = full_version if full_version.count(".") == 3 else f"{major}.0.0.0"
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _chrome_sec_ch_ua(major: str) -> str:
    return _CHROME_SEC_CH_UA.get(major) or f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not_A Brand";v="24"'


def _chrome_sec_ch_ua_full_version_list(major: str, full_version: str) -> str:
    # 真实浏览器中 full-version-list 与 sec-ch-ua 的品牌顺序、GREASE 品牌名完全一致，
    # 仅版本号为完整 build 号（如 136.0.7103.114）；两者互相矛盾是强机器人信号
    base = _CHROME_SEC_CH_UA.get(major) or _chrome_sec_ch_ua(major)

    def _replace(match: "re.Match[str]") -> str:
        brand = match.group(1)
        old_major = match.group(2)
        # 注意保留品牌两侧的引号：正则匹配会吃掉开头的 "，替换串必须补回，
        # 否则首品牌丢失开引号（真实浏览器不可能产生的畸形值）。
        if "not" in brand.lower():
            # GREASE 品牌版本同样展开为完整形式（如 99.0.0.0）
            return f'"{brand}";v="{old_major}.0.0.0"'
        return f'"{brand}";v="{version}"'

    if full_version.count(".") == 3 and not full_version.endswith(".0.0.0"):
        version = full_version
    else:
        # 生成随机 build 号，避免与 UA 冻结版本雷同
        version = (
            f"{major}.0.{random.randint(1000, 7999)}.{random.randint(10, 400)}"
        )
    return re.sub(r'"([^"]+)";v="(\d+)"', _replace, base)




def _complete_browser_fingerprint(profile: dict[str, str]) -> dict[str, str]:
    major = str(profile.get("major") or "147").strip()
    family = str(profile.get("browser_family") or "firefox").strip().lower()
    if family not in {"firefox", "chrome"}:
        family = "firefox"
    if family == "chrome":
        default_ua = _chrome_user_agent(major, str(profile.get("full_version") or ""))
        default_sec_ch_ua = _chrome_sec_ch_ua(major)
        default_version_list = _chrome_sec_ch_ua_full_version_list(
            major, str(profile.get("full_version") or "")
        )
        default_vendor = "Google Inc."
    else:
        default_ua = _firefox_user_agent(major, str(profile.get("full_version") or f"{major}.0"))
        default_sec_ch_ua = _firefox_sec_ch_ua(major)
        default_version_list = _firefox_sec_ch_ua_full_version_list(major, str(profile.get("full_version") or ""))
        default_vendor = ""
    full_version = str(profile.get("full_version") or f"{major}.0.0.0").strip()
    completed = {
        **profile,
        "browser_family": family,
        "major": major,
        "full_version": full_version,
        "user_agent": str(profile.get("user_agent") or default_ua),
        "sec_ch_ua": str(profile.get("sec_ch_ua") or default_sec_ch_ua),
        "sec_ch_ua_full_version_list": str(
            profile.get("sec_ch_ua_full_version_list") or default_version_list
        ),
        "navigator_vendor": str(profile.get("navigator_vendor") or default_vendor),
        "accept_language": str(profile.get("accept_language") or "en-US,en;q=0.9"),
        "platform_version": str(profile.get("platform_version") or "10.0.0"),
        "impersonate": str(profile.get("impersonate") or "firefox147"),
        # 补充的真实环境参数（sentinel payload 与设备信息存档共用）
        "platform": str(profile.get("platform") or "Win32"),
        "max_touch_points": str(profile.get("max_touch_points") or "0"),
        "color_depth": str(profile.get("color_depth") or "24"),
        "audio_sample_rate": str(profile.get("audio_sample_rate") or "48000"),
        "audio_channel_count": str(profile.get("audio_channel_count") or "2"),
        # 真实浏览器默认 navigator.doNotTrack = null（Chrome/Firefox 均需用户手动开启）
        "do_not_track": str(profile.get("do_not_track") or ""),
        "storage_quota_gb": str(profile.get("storage_quota_gb") or "256"),
        "storage_usage_mb": str(profile.get("storage_usage_mb") or "152"),
        "connection_rtt": str(profile.get("connection_rtt") or "50"),
        "connection_downlink": str(profile.get("connection_downlink") or "10"),
    }
    if family == "chrome":
        # 真实 Chrome 报告 navigator.plugins（内置 PDF 查看器三件套）
        completed["pdf_viewer_enabled"] = str(profile.get("pdf_viewer_enabled") or "true")
        completed["plugin_count"] = "5"
    return completed




DEFAULT_BROWSER_FINGERPRINT = _complete_browser_fingerprint(REGISTER_BROWSER_PROFILES[0])


user_agent = DEFAULT_BROWSER_FINGERPRINT["user_agent"]


sec_ch_ua = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua"]


sec_ch_ua_full_version_list = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua_full_version_list"]



common_headers = {
    "accept": "application/json",
    # Chrome 123+/Firefox 126+ 均发送 zstd；此前缺 zstd 会覆盖 curl_cffi 的正确默认值
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}



navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}




def _browser_fingerprint(fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    return _complete_browser_fingerprint(fingerprint or DEFAULT_BROWSER_FINGERPRINT)




def _header_fingerprint(headers: dict[str, str], fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    fp = _browser_fingerprint(fingerprint)
    next_headers = dict(headers)
    next_headers["user-agent"] = fp["user_agent"]
    if fp["sec_ch_ua"]:
        next_headers["sec-ch-ua"] = fp["sec_ch_ua"]
    else:
        for key in tuple(next_headers):
            if key.lower().startswith("sec-ch-"):
                next_headers.pop(key, None)
    if "sec-ch-ua-full-version-list" in next_headers:
        if fp["sec_ch_ua_full_version_list"]:
            next_headers["sec-ch-ua-full-version-list"] = fp["sec_ch_ua_full_version_list"]
        else:
            next_headers.pop("sec-ch-ua-full-version-list", None)
    if "sec-ch-ua-platform-version" in next_headers:
        next_headers["sec-ch-ua-platform-version"] = f'"{fp["platform_version"]}"'
    if "accept-language" in next_headers:
        next_headers["accept-language"] = fp["accept_language"]
    # --- 家族差异头 ---
    # 真实 Chrome 从不发送 Sec-GPC（Firefox 120+ 默认发送）；两家默认均不发 DNT。
    if fp.get("browser_family") == "firefox":
        next_headers["sec-gpc"] = "1"
    else:
        next_headers.pop("sec-gpc", None)
    next_headers.pop("dnt", None)
    # --- 抑制 curl_cffi C 层 navigate 默认头泄漏到 fetch 请求 ---
    # curl_cffi 的 impersonate 默认头集是导航型（Sec-Fetch-User / Upgrade-Insecure-Requests），
    # 真实浏览器的 fetch(cors) 请求不会携带；值为 None 时 curl_cffi 会生成
    # "key:" 行，libcurl 语义为显式禁用该头（"" 只会发送空值头）。
    if str(next_headers.get("sec-fetch-mode") or "").lower() != "navigate":
        next_headers.setdefault("sec-fetch-user", None)  # type: ignore[dict-item]
        next_headers.setdefault("upgrade-insecure-requests", None)  # type: ignore[dict-item]
    return next_headers




def _sec_fetch_site_value(referer: str = "", target_url: str = "") -> str:
    """按导航来源计算真实的 sec-fetch-site 值。

    无来源 = none（地址栏直开/首跳）；跨站跳转（chatgpt.com → auth.openai.com）
    = cross-site；同源 = same-origin；同注册域不同主机 = same-site。
    """

    def _host(url: str) -> str:
        try:
            from urllib.parse import urlsplit
            return (urlsplit(str(url)).hostname or "").lower()
        except ValueError:
            return ""

    def _site(url: str) -> str:
        host = _host(url)
        if not host:
            return ""
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    if not _host(referer):
        return "none"
    if not _host(target_url):
        return "same-origin"
    if _host(referer) == _host(target_url):
        return "same-origin"
    if _site(referer) == _site(target_url):
        return "same-site"
    return "cross-site"




def _extract_firefox_version_from_user_agent(value: str) -> tuple[str, str]:
    ua = str(value or "")
    for marker in ("Firefox/",):
        if marker not in ua:
            continue
        tail = ua.split(marker, 1)[1]
        version = tail.split(" ", 1)[0].strip()
        major = version.split(".", 1)[0].strip()
        if major.isdigit():
            return major, version or f"{major}.0.0.0"
    return "", ""


def _extract_chrome_version_from_user_agent(value: str) -> tuple[str, str]:
    ua = str(value or "")
    if "Firefox/" in ua or "Chrome/" not in ua:
        return "", ""
    tail = ua.split("Chrome/", 1)[1]
    version = tail.split(" ", 1)[0].strip()
    major = version.split(".", 1)[0].strip()
    if major.isdigit():
        return major, version or f"{major}.0.0.0"
    return "", ""




def _fingerprint_with_user_agent(fingerprint: dict[str, str] | None, value: str) -> dict[str, str]:
    ua = str(value or "").strip()
    if not ua:
        return _browser_fingerprint(fingerprint)
    fp = _browser_fingerprint(fingerprint)
    impersonate_by_major = {
        str(item.get("major") or ""): item
        for item in REGISTER_BROWSER_PROFILES
    }
    # Clearance endpoints occasionally report a UA from a different engine.
    # Keep the protocol session's own family instead of mixing TLS and HTTP
    # fingerprints, and reject versions absent from curl_cffi.
    if fp.get("browser_family") == "chrome":
        major, full_version = _extract_chrome_version_from_user_agent(ua)
        if not major or major not in impersonate_by_major:
            return fp
        profile = impersonate_by_major[major]
        if str(profile.get("browser_family")) != "chrome":
            return fp
        full_version = full_version or f"{major}.0.0.0"
        return {
            **fp,
            "major": major,
            "full_version": full_version,
            "impersonate": str(profile.get("impersonate") or fp["impersonate"]),
            "user_agent": ua,
            "sec_ch_ua": _chrome_sec_ch_ua(major),
            "sec_ch_ua_full_version_list": _chrome_sec_ch_ua_full_version_list(major, full_version),
        }
    major, full_version = _extract_firefox_version_from_user_agent(ua)
    if not major or major not in impersonate_by_major:
        return fp
    profile = impersonate_by_major[major]
    if str(profile.get("browser_family") or "firefox") != "firefox":
        return fp
    full_version = full_version or f"{major}.0"
    return {
        **fp,
        "major": major,
        "full_version": full_version,
        "impersonate": str(profile.get("impersonate") or fp["impersonate"]),
        "user_agent": ua,
        "sec_ch_ua": _firefox_sec_ch_ua(major),
        "sec_ch_ua_full_version_list": _firefox_sec_ch_ua_full_version_list(major, full_version),
    }




def _client_hint_headers(fp: dict[str, str]) -> dict[str, str]:
    """Return Chromium client hints only when the selected profile uses them."""
    if not fp.get("sec_ch_ua"):
        return {}
    # 平台头必须与 UA 一致：FlareSolverr 清障对齐后的 Chromium 运行在
    # Linux 容器里（UA 含 "X11; Linux x86_64"），"Linux UA + Windows CH"
    # 是强机器人信号。默认档位 UA 均为 Windows，行为不变。
    ua = str(fp.get("user_agent") or "")
    if "X11; Linux" in ua or "Linux x86_64" in ua:
        platform = '"Linux"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        platform = '"macOS"'
    else:
        platform = '"Windows"'
    return {
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
    }




def _proxy_country_code(proxy: str = "") -> str:
    # A country embedded in proxy credentials describes the actual pool and
    # takes precedence over the panel selection exported by config_loader.
    return (
        country_code_from_proxy(proxy)
        or str(os.environ.get("REGISTER_PROXY_COUNTRY") or "").strip().upper()
        or "JP"
    )




def _language_profiles_for_proxy(proxy: str = "") -> tuple[str, ...]:
    identity = country_identity(_proxy_country_code(proxy))
    if identity:
        return (identity.accept_language,)
    return REGISTER_LANGUAGE_PROFILES




# 硬件档位相关规则：真实设备中屏幕/显卡/核心数强相关，独立采样会出现
# "1366x768 笔记本屏 + RTX 3060 台式卡"这类不存在的组合（此前 US 出口错搭率 30%）。
_IGPU_MARKERS: tuple[str, ...] = (
    "HD Graphics", "UHD Graphics", "Iris", "Radeon(TM) Graphics", "Intel(R) UHD",
)
_ENTRY_DISCRETE: tuple[str, ...] = ("GTX 1050", "GTX 1650")
_LOW_SCREENS: frozenset[str] = frozenset(
    {"1366x768", "1280x720", "1536x864", "1600x900", "1707x960"}
)
_HIGH_SCREENS: frozenset[str] = frozenset({"2560x1440", "2560x1600"})
# 较新的核显（可驱动 2K 屏 / 中端任务）
_MODERN_IGPU: tuple[str, ...] = ("UHD Graphics 770", "Iris(R) Xe", "Radeon(TM) Graphics")


def _webgl_is_igpu(renderer: str) -> bool:
    return any(marker in str(renderer) for marker in _IGPU_MARKERS)


def _hardware_compatible(device: dict[str, str], webgl: dict[str, str]) -> bool:
    """设备与显卡档位一致性校验。"""
    screen = str(device.get("screen") or "")
    renderer = str(webgl.get("renderer") or "")
    try:
        cores = int(str(device.get("hardware_concurrency") or "8") or 8)
    except ValueError:
        cores = 8
    igpu = _webgl_is_igpu(renderer)
    if screen in _LOW_SCREENS:
        # 入门屏（轻薄本/老台式）：核显或入门独显，不配中高端独显
        if not igpu and not any(m in renderer for m in _ENTRY_DISCRETE):
            return False
    if screen in _HIGH_SCREENS:
        # 2K 屏：排除上古核显（HD 530/620/630、UHD 620/630 等）
        if igpu and not any(m in renderer for m in _MODERN_IGPU):
            return False
    if not igpu and not any(m in renderer for m in _ENTRY_DISCRETE) and cores < 6:
        # 中高端独显整机 CPU 至少 6 核（Ryzen 5 / i5 起步；入门卡配 4 核 i3 属真实存在）
        return False
    return True


# 多时区大国：按真实人口权重采样时区（此前全部锁死在 identity.timezone 第一城市，
# 例如 US 全部 America/New_York，与真实人口分布不符）。
COUNTRY_TIMEZONE_CHOICES: dict[str, tuple[tuple[str, int], ...]] = {
    "US": (
        ("America/New_York", 35), ("America/Chicago", 25),
        ("America/Denver", 10), ("America/Los_Angeles", 25), ("America/Phoenix", 5),
    ),
    "CA": (
        ("America/Toronto", 60), ("America/Vancouver", 25),
        ("America/Winnipeg", 10), ("America/Halifax", 5),
    ),
    "BR": (
        ("America/Sao_Paulo", 55), ("America/Bahia", 10), ("America/Fortaleza", 10),
        ("America/Recife", 10), ("America/Cuiaba", 10), ("America/Manaus", 5),
    ),
    "AU": (
        ("Australia/Sydney", 55), ("Australia/Melbourne", 35),
        ("Australia/Brisbane", 7), ("Australia/Perth", 3),
    ),
}

# 2026-09 真实 Win10/Win11 分布（Win10 已于 2025-10 EOL）：老硬件更多停留在 Win10。
# (win10, win11, win11_25H2) 权重；平台版本在采样时统一归一，不再依赖各国池子手工配比。
# 25H2(15.0.0) 对应本仓资格率 76% 的最优档。
_PLATFORM_VERSIONS: tuple[tuple[str, int], ...] = (
    ("10.0.0", 30), ("13.0.0", 45), ("15.0.0", 25),
)
_PLATFORM_VERSIONS_LEGACY: tuple[tuple[str, int], ...] = (
    ("10.0.0", 55), ("13.0.0", 35), ("15.0.0", 10),
)
# 入门屏机器内存：真实 4GB 仍占一定比例（尤其 VN/BR 老本）
_LOW_SCREEN_MEMORY_4_RATIO = 0.35


def _pick_weighted(choices: tuple[tuple[str, int], ...]) -> str:
    values, weights = zip(*choices)
    return random.SystemRandom().choices(values, weights=weights, k=1)[0]


def _pick_timezone(country: str, identity) -> str:
    choices = COUNTRY_TIMEZONE_CHOICES.get(country)
    if choices:
        return _pick_weighted(choices)
    return identity.timezone if identity else "UTC"


def _normalize_device_traits(device: dict[str, str]) -> dict[str, str]:
    """平台版本与内存的市占归一化（作用于全部国家池，单点维护）。"""
    normalized = dict(device)
    screen = str(normalized.get("screen") or "")
    legacy = screen in _LOW_SCREENS
    normalized["platform_version"] = _pick_weighted(
        _PLATFORM_VERSIONS_LEGACY if legacy else _PLATFORM_VERSIONS
    )
    if legacy and normalized.get("device_memory") == "8":
        if random.SystemRandom().random() < _LOW_SCREEN_MEMORY_4_RATIO:
            normalized["device_memory"] = "4"
    return normalized



def _country_fingerprint_traits(proxy: str = "") -> dict[str, str]:
    """Return country-coherent browser traits for the selected egress route.

    24 个支持国家各自拥有独立的设备池与显卡池：屏幕/核心/内存、WebGL、
    locale、时区全部与出口国家保持一致；未收录国家回落到中性通用池，
    不再借用其他国家（日本）的显卡特征。设备与显卡按硬件档位规则
    相关采样（低端屏不配高性能独显、2K 屏不配上古核显）。
    """
    country = _proxy_country_code(proxy)
    identity = country_identity(country)
    device_pool = REGISTER_COUNTRY_DEVICE_TRAITS.get(country) or REGISTER_DEVICE_TRAITS
    webgl_pool = REGISTER_COUNTRY_WEBGL_TRAITS.get(country) or REGISTER_FALLBACK_WEBGL_TRAITS
    device: dict[str, str] | None = None
    webgl: dict[str, str] | None = None
    for _attempt in range(8):
        candidate = secrets.choice(device_pool)
        allowed = [w for w in webgl_pool if _hardware_compatible(candidate, w)]
        if allowed:
            device = candidate
            webgl = secrets.choice(allowed)
            break
    if device is None or webgl is None:
        device = secrets.choice(device_pool)
        webgl = secrets.choice(webgl_pool)
    device = _normalize_device_traits(device)
    timezone = _pick_timezone(country, identity)
    return {
        **device,
        "locale": identity.locale if identity else "en-US",
        "timezone": timezone,
        "timezone_offset_min": str(
            _timezone_offset_for_timezone(
                timezone,
                fallback=(identity.timezone_offsets[0] if identity else 0),
            )
        ),
        "webgl_vendor": webgl["vendor"],
        "webgl_renderer": webgl["renderer"],
    }


def _timezone_offset_for_timezone(timezone_name: str, *, fallback: int = 0) -> int:
    """Return JavaScript-style timezone offset minutes for a concrete zone."""
    try:
        offset = datetime.now(ZoneInfo(str(timezone_name))).utcoffset()
    except Exception:
        offset = None
    if offset is None:
        return int(fallback)
    return -int(offset.total_seconds() / 60)




def _timezone_offset_for_proxy(proxy: str = "") -> int:
    country = _proxy_country_code(proxy)
    choices = COUNTRY_TIMEZONE_OFFSETS.get(country)
    if not choices:
        return 0
    return int(secrets.choice(choices))




def _weighted_fingerprint_profiles(proxy: str = "") -> list[dict[str, str]]:
    """按国家家族权重展开 profile 列表（组合池与兜底抽样共用）。

    家族内再按 profile 自带 weight 等比展开到家族配额，
    使 Chrome/Firefox 比例贴近各国真实市占。
    """
    country = _proxy_country_code(proxy)
    chrome_w, firefox_w = REGISTER_COUNTRY_BROWSER_WEIGHTS.get(
        country, DEFAULT_COUNTRY_BROWSER_WEIGHTS
    )
    expanded: list[dict[str, str]] = []
    for family, quota in (("chrome", chrome_w), ("firefox", firefox_w)):
        family_profiles = [
            dict(p)
            for p in REGISTER_BROWSER_PROFILES
            if str(p.get("browser_family") or "firefox") == family
        ]
        if not family_profiles:
            continue
        total = sum(max(1, int(p.get("weight") or 1)) for p in family_profiles)
        for p in family_profiles:
            weight = max(1, int(p.get("weight") or 1))
            count = max(1, round(quota * weight / total))
            expanded.extend([dict(p) for _ in range(count)])
    return expanded or [dict(DEFAULT_BROWSER_FINGERPRINT)]




def _fingerprint_pool_key(proxy: str = "") -> str:
    country = _proxy_country_code(proxy) or "LOCAL"
    return f"register-fingerprint:{country}"




def _claim_fingerprint_combo(proxy: str = "") -> dict[str, str] | None:
    """Return an under-used browser/profile tuple for the current process.

    Random pure sampling can accidentally repeat the same high-frequency tuple
    for a short batch.  This small per-process shuffled pool keeps variation
    high without changing the public concurrency model or touching backend
    queues.
    """
    state = getattr(_claim_fingerprint_combo, "_state", None)
    if not isinstance(state, dict):
        state = {}
        setattr(_claim_fingerprint_combo, "_state", state)
    key = _fingerprint_pool_key(proxy)
    pool = state.get(key)
    if not pool:
        combos: list[dict[str, str]] = []
        for profile in _weighted_fingerprint_profiles(proxy):
            for trait in REGISTER_DEVICE_TRAITS:
                for language in _language_profiles_for_proxy(proxy):
                    combos.append({**profile, **trait, "accept_language": language})
        random.SystemRandom().shuffle(combos)
        state[key] = combos
        pool = combos
    try:
        return dict(pool.pop())
    except Exception:
        return None




def _make_browser_fingerprint(proxy: str = "") -> dict[str, str]:
    profile = _claim_fingerprint_combo(proxy)
    if not profile:
        # 兜底：从按国家加权展开的列表中等概率抽取（权重已编码在展开次数中）
        selected = random.SystemRandom().choice(_weighted_fingerprint_profiles(proxy))
        traits = secrets.choice(REGISTER_DEVICE_TRAITS)
        profile = {
            **selected,
            **traits,
            "accept_language": secrets.choice(_language_profiles_for_proxy(proxy)),
        }
    country_traits = _country_fingerprint_traits(proxy)
    return _complete_browser_fingerprint(
        {
            **profile,
            **country_traits,
            "profile_id": secrets.token_hex(6),
            "sentinel_sid": str(uuid.uuid4()),
            "storage_quota_gb": secrets.choice(REGISTER_STORAGE_QUOTA_GB),
            "storage_usage_mb": secrets.choice(REGISTER_STORAGE_USAGE_MB),
            "connection_rtt": secrets.choice(REGISTER_CONNECTION_RTT),
            "connection_downlink": secrets.choice(REGISTER_CONNECTION_DOWNLINK),
        }
    )
