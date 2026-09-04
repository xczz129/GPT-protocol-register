"""协议层浏览器指纹体系：UA / 设备 / 显卡 / 语言 / 时区特征与指纹组合池。"""

from __future__ import annotations

import os
import random
import secrets
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from common.country_identity import COUNTRY_IDENTITIES, country_code_from_proxy, country_identity

from register.protocol_context import auth_base


REGISTER_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    # --- Firefox（与历史注册环境保持连续） ---
    {
        "impersonate": "firefox147",
        "browser_family": "firefox",
        "major": "147",
        "full_version": "147.0",
        "weight": "6",
    },
    {
        "impersonate": "firefox144",
        "browser_family": "firefox",
        "major": "144",
        "full_version": "144.0",
        "weight": "4",
    },
    {
        "impersonate": "firefox135",
        "browser_family": "firefox",
        "major": "135",
        "full_version": "135.0",
        "weight": "2",
    },
    {
        "impersonate": "firefox133",
        "browser_family": "firefox",
        "major": "133",
        "full_version": "133.0",
        "weight": "1",
    },
    # --- Chrome（贴近真实市占的稳定版分布） ---
    {
        "impersonate": "chrome142",
        "browser_family": "chrome",
        "major": "142",
        "full_version": "142.0.0.0",
        "weight": "5",
    },
    {
        "impersonate": "chrome136",
        "browser_family": "chrome",
        "major": "136",
        "full_version": "136.0.0.0",
        "weight": "4",
    },
    {
        "impersonate": "chrome131",
        "browser_family": "chrome",
        "major": "131",
        "full_version": "131.0.0.0",
        "weight": "3",
    },
    {
        "impersonate": "chrome124",
        "browser_family": "chrome",
        "major": "124",
        "full_version": "124.0.0.0",
        "weight": "1",
    },
)





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
    # 主流桌面组合（1080p 办公机到高端游戏机）
    {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "16", "device_memory": "32", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1920x1080", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    # 笔记本（含 Windows 推荐缩放）
    {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
    {"screen": "1536x864", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
    {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1366x768", "hardware_concurrency": "8", "device_memory": "4", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1707x960", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.125", "platform_version": "10.0.0"},
    {"screen": "1280x720", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1.5", "platform_version": "10.0.0"},
    # 高分辨率 / 高刷新率
    {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "2560x1440", "hardware_concurrency": "12", "device_memory": "32", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "2560x1440", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "2560x1600", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "3840x2160", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1.5", "platform_version": "10.0.0"},
    {"screen": "3840x2160", "hardware_concurrency": "20", "device_memory": "32", "device_pixel_ratio": "2", "platform_version": "10.0.0"},
    # 16:10 / 商务本
    {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
    {"screen": "1600x900", "hardware_concurrency": "6", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1440x900", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    {"screen": "1680x1050", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
)



REGISTER_JAPAN_WEBGL_TRAITS: tuple[dict[str, str], ...] = (
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
)



REGISTER_US_WEBGL_TRAITS: tuple[dict[str, str], ...] = (
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
)



REGISTER_COUNTRY_DEVICE_TRAITS: dict[str, tuple[dict[str, str], ...]] = {
    "PH": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
    ),
    "TH": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "TR": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1440x900", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "VN": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "KR": (
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
        {"screen": "1920x1200", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "2560x1440", "hardware_concurrency": "16", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
    "IN": (
        {"screen": "1366x768", "hardware_concurrency": "4", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1366x768", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
        {"screen": "1536x864", "hardware_concurrency": "8", "device_memory": "8", "device_pixel_ratio": "1.25", "platform_version": "10.0.0"},
        {"screen": "1600x900", "hardware_concurrency": "8", "device_memory": "16", "device_pixel_ratio": "1.2", "platform_version": "10.0.0"},
        {"screen": "1920x1080", "hardware_concurrency": "12", "device_memory": "16", "device_pixel_ratio": "1", "platform_version": "10.0.0"},
    ),
}



REGISTER_COUNTRY_WEBGL_TRAITS: dict[str, tuple[dict[str, str], ...]] = {
    "PH": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 560 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "TH": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "TR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "VN": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "KR": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
    "IN": (
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "Intel Inc.", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "NVIDIA Corporation", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
        {"vendor": "AMD", "renderer": "ANGLE (AMD, AMD Radeon(TM) Vega 8 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    ),
}



REGISTER_LANGUAGE_PROFILES: tuple[str, ...] = (
    "en-US,en;q=0.9",
    "en-US,en;q=0.9",
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.8",
    "en-GB,en;q=0.9",
)



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
_CHROME_SEC_CH_UA: dict[str, str] = {
    "124": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "131": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "136": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "142": '"Google Chrome";v="142", "Chromium";v="142", "Not:A-Brand";v="99"',
    "146": '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"',
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
    version = full_version if full_version.count(".") == 3 else f"{major}.0.0.0"
    grease_major = "24" if major in {"131", "146"} else "99"
    if _CHROME_SEC_CH_UA.get(major, "").startswith('"Chromium"'):
        return (
            f'"Google Chrome";v="{version}", "Chromium";v="{version}", '
            f'"Not_A Brand";v="{grease_major}.0.0.0"'
        )
    return (
        f'"Chromium";v="{version}", "Google Chrome";v="{version}", '
        f'"Not_A Brand";v="{grease_major}.0.0.0"'
    )




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
        "do_not_track": str(profile.get("do_not_track") or "1"),
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
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
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
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
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
    return next_headers




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
    return {
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
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




def _country_fingerprint_traits(proxy: str = "") -> dict[str, str]:
    """Return country-coherent browser traits for the selected egress route."""
    country = _proxy_country_code(proxy)
    if country == "JP":
        webgl = secrets.choice(REGISTER_JAPAN_WEBGL_TRAITS)
        return {
            "locale": "ja-JP",
            "timezone": "Asia/Tokyo",
            "timezone_offset_min": "-540",
            "webgl_vendor": webgl["vendor"],
            "webgl_renderer": webgl["renderer"],
        }
    if country == "US":
        webgl = secrets.choice(REGISTER_US_WEBGL_TRAITS)
        return {
            "locale": "en-US",
            "timezone": "America/New_York",
            "timezone_offset_min": str(
                _timezone_offset_for_timezone("America/New_York", fallback=300)
            ),
            "webgl_vendor": webgl["vendor"],
            "webgl_renderer": webgl["renderer"],
        }
    if country in REGISTER_COUNTRY_WEBGL_TRAITS:
        identity = country_identity(country)
        webgl = secrets.choice(REGISTER_COUNTRY_WEBGL_TRAITS[country])
        device = secrets.choice(REGISTER_COUNTRY_DEVICE_TRAITS[country])
        return {
            **device,
            "locale": identity.locale if identity else "en-US",
            "timezone": identity.timezone if identity else "UTC",
            "timezone_offset_min": str(
                _timezone_offset_for_timezone(
                    identity.timezone if identity else "UTC",
                    fallback=(identity.timezone_offsets[0] if identity else 0),
                )
            ),
            "webgl_vendor": webgl["vendor"],
            "webgl_renderer": webgl["renderer"],
        }
    # Countries with an identity but no curated hardware pool fall back to
    # the shared device traits while keeping their own locale/clock, so the
    # fingerprint stays country-coherent for every supported country.
    identity = country_identity(country)
    device = secrets.choice(REGISTER_DEVICE_TRAITS)
    webgl = secrets.choice(
        REGISTER_COUNTRY_WEBGL_TRAITS.get(country)
        or REGISTER_JAPAN_WEBGL_TRAITS
    )
    return {
        **device,
        "locale": identity.locale if identity else "en-US",
        "timezone": identity.timezone if identity else "UTC",
        "timezone_offset_min": str(
            _timezone_offset_for_timezone(
                identity.timezone if identity else "UTC",
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




def _weighted_fingerprint_profiles() -> list[dict[str, str]]:
    profiles: list[dict[str, str]] = []
    for profile in REGISTER_BROWSER_PROFILES:
        weight = max(1, int(profile.get("weight") or 1))
        profiles.extend([dict(profile) for _ in range(weight)])
    return profiles or [dict(DEFAULT_BROWSER_FINGERPRINT)]




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
        for profile in _weighted_fingerprint_profiles():
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
        chooser = random.SystemRandom()
        selected = chooser.choices(
            REGISTER_BROWSER_PROFILES,
            weights=[max(1, int(item.get("weight") or 1)) for item in REGISTER_BROWSER_PROFILES],
            k=1,
        )[0]
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
        }
    )
