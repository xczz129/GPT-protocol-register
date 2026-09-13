# -*- coding: utf-8 -*-
"""Load YAML/JSON config for GPT registration."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from common.country_identity import country_identity, normalize_country_code

ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG: dict[str, Any] = {
    "mail": {
        "providers": [
            {
                "type": "outlook_token",
                "enable": True,
                "label": "Outlook Pool",
                # 默认 imap：个人 Outlook refresh_token 常无 Graph Mail.Read（AADSTS70000）
                "mode": "imap",
                "imap_host": "outlook.office365.com",
                "message_limit": 10,
                "mailboxes": "",
            },
            {
                "type": "icloud_api",
                "enable": True,
                "label": "iCloud Pool",
                # Hide My Email 取码 API（https://icloud.xbovo.online）
                "api_base": "https://icloud.xbovo.online",
                # 每行：email----api_key
                "mailboxes": "",
            },
            {
                "type": "smsbower_mail",
                "enable": False,
                "register_child": True,
                "label": "SMSBower Mail",
                # Mail API origin; endpoint paths are appended by the provider.
                "api_base": "https://smsbower.page",
                "api_key": "",
                # OpenAI / ChatGPT mail service code.
                "service": "dr",
                # Purchase attempts follow this order when the first domain is out of stock.
                "domains": ["gmail.com", "icloud.com"],
                # Empty means that the provider's current price is accepted.
                "max_price": "",
                "capacity": 0,
                "mailboxes": "",
            },
            {
                "type": "maddy_webui",
                "enable": False,
                "label": "Maddy 邮箱",
                "api_base": "https://120.53.232.195/api",
                "api_key": "",
                "default_domain": "unjome.com",
                "domains": ["unjome.com"],
                "blocked_domains": ["xb1219.com"],
                "local_length": 12,
                "capacity": 500,
                "mailboxes": "",
            },
            {
                "type": "fastmail_api",
                "enable": False,
                "label": "Fastmail Pool",
                # mailcode-api 服务地址（Fastmail 别名池）
                "api_base": "http://127.0.0.1:8686",
                # 管理密钥：管理端点（acquire/池状态）使用 X-API-Key
                "admin_key": "",
                # 主账号列表，每行一个（密码保存在 mailcode-api 服务内）
                "accounts": "",
                "capacity": 615,
                "mailboxes": "",
            },
        ],
        "request_timeout": 30,
        "wait_timeout": 90,
        "smsbower_wait_timeout": 180,
        "wait_interval": 3,
        "icloud_resync_interval": 15,
        "resend_after": 45,
    },
    "proxy": {
        "enabled": False,
        "mode": "single_proxy",
        "url": "",
        "resource_url": "",
        "pool_file": "",
        "selected_country": "",
        "rotate_per_task": True,
        "clearance": {
            "enabled": False,
            "mode": "none",
            "flaresolverr_url": "",
            "proxy_url": "",
            "timeout_sec": 60,
            "refresh_interval": 3600,
        },
    },
    "registration": {
        "engine": "protocol",
        "threads": 10,
        "browser_protocol_security_after_register": True,
        "protocol_otp_timeout": 180,
        "protocol_otp_poll_chunk": 60,
        "protocol_tls_verify": True,
        "protocol_ca_bundle": "",
        "protocol_request_timeout": 30,
        "protocol_oauth_timeout": 60,
        "protocol_sentinel_timeout": 30,
        "protocol_sentinel_worker_timeout": 90,
        "protocol_sentinel_sdk_cache_ttl": 300,
        "protocol_sentinel_behavior_duration_ms": 4200,
        "protocol_sentinel_worker_persistent": True,
        "protocol_sentinel_worker_pool_size": 4,
        "protocol_verify_me": True,
        "protocol_enable_totp_after_register": True,
        "protocol_account_timeout": 600,
        "protocol_login_otp_timeout": 180,
        "protocol_login_poll_chunk": 60,
        "post_register_health_check": False,
        "total": 1,
        "timeout": 480,
        "max_transient_retries": 2,
    },
    "cache": {
        "enabled": True,
        "redis_url": "redis://127.0.0.1:6379/0",
        "prefix": "gpt-register-fp",
        "socket_timeout": 0.35,
        "status_ttl": 3,
        "accounts_ttl": 300,
        "mailboxes_ttl": 120,
        "operations_ttl": 5,
        "config_ttl": 60,
        "tasks_ttl": 60,
        "browsers_ttl": 300,
        "local_max_entries": 32,
        "local_max_bytes": 67108864,
    },
    "fingerprint": {
        # local = free（本机 Chrome + stealth），无需付费账号
        # camoufox = free 反检测 Firefox（需 python3 -m camoufox fetch）
        # obscura = free 轻量 Rust headless + CDP（bin/obscura，低内存）
        "browser": "local",
        "camoufox": {
            "headless": True,
            "os": "windows",
            # auto = 跟随 proxy.selected_country；也可手工指定 en-US 等 locale。
            "locale": "auto",
            "geoip": True,
            "humanize": 0.15,
        },
    },
    # chatgpt2api（basketikun）— 注册成功后 POST /api/accounts 自动上传普通网页号
    "chatgpt2api": {
        "enabled": False,
        "url": "https://chatgpt2api.xbovo.online",
        "key": "",
        "auto_upload": True,
    },
    "logging": {
        "level": "INFO",
    },
    "maintenance": {
        "enabled": False,
        "interval_hours": 12,
        "workers": 10,
        "auto_delete_invalid": False,
        "webhook_url": "",
        "notify_on_complete": False,
        "notify_on_pool_low": False,
        "pool_low_threshold": 3,
    },
    "maddy_auto_register": {
        "enabled": False,
        "interval_hours": 1,
        "per_domain": 20,
        "concurrency": 3,
        "engine": "protocol",
        "keep_on_fail": False,
        "refresh_domains": True,
        "group_id": "",
    },
    # 本地面板安全（默认仅本机；token 空则启动时自动生成）
    "web": {
        "host": "127.0.0.1",
        "port": 8787,
        # 面板登录账号（password 明文写在 config.yaml，请妥善保管）
        "username": "admin",
        "password": "",
        # 会话签名密钥（留空自动生成；一般无需手填）
        "token": "",
        # 只有来自这些反向代理地址的转发头才会参与 IP 限流识别
        "trusted_proxies": ["127.0.0.1", "::1"],
    },
}


def _deep_merge(base: dict, overlay: dict) -> None:
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required for .yaml config. "
                "pip install pyyaml  or use config.json"
            ) from exc
        # PyYAML ships a C-backed safe loader in normal deployments.  Keep
        # the pure-Python loader as a fallback for minimal environments while
        # avoiding ~100 ms of CPU work on every cold config read.
        safe_loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        data = yaml.load(text, Loader=safe_loader) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be object: {path}")
    return data


def apply_fingerprint_env(cfg: dict[str, Any]) -> None:
    """Sync the selected free fingerprint browser into the process environment."""
    fp = cfg.get("fingerprint") if isinstance(cfg.get("fingerprint"), dict) else {}
    browser = str(fp.get("browser") or "local").strip().lower() or "local"
    aliases = {
        "chrome": "local", "stealth": "local", "playwright": "local",
        "chromium": "local", "free": "local", "cfx": "camoufox",
        "fox": "camoufox", "obs": "obscura", "obscura-browser": "obscura",
    }
    browser = aliases.get(browser, browser)
    if browser not in {"local", "camoufox", "obscura"}:
        browser = "local"
    # The force flag owns only the browser provider; country identity must
    # still refresh when the proxy country changes in the panel.
    force_browser = os.environ.get("FINGERPRINT_BROWSER_FORCE", "").strip().lower() in {
        "1", "true", "yes",
    }
    if not force_browser:
        os.environ["FINGERPRINT_BROWSER"] = browser
    cfx = fp.get("camoufox") if isinstance(fp.get("camoufox"), dict) else {}
    proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    country = normalize_country_code(proxy.get("selected_country"))
    identity = country_identity(country)
    configured_locale = str(cfx.get("locale") or "").strip()
    auto_locale = configured_locale.lower() in {"", "auto"}
    locale = identity.locale if auto_locale and identity else ("" if auto_locale else configured_locale)
    os.environ["CAMOUFOX_HEADLESS"] = "1" if cfx.get("headless", True) else "0"
    os.environ["CAMOUFOX_OS"] = str(cfx.get("os") or "windows").strip()
    os.environ["CAMOUFOX_LOCALE"] = locale
    os.environ["CAMOUFOX_GEOIP"] = "1" if cfx.get("geoip", True) else "0"
    os.environ["CAMOUFOX_HUMANIZE"] = str(cfx.get("humanize", 0.15))
    os.environ["REGISTER_PROXY_COUNTRY"] = country
    os.environ["REGISTER_ACCEPT_LANGUAGE"] = identity.accept_language if identity else ""
    os.environ["LOCAL_LOCALE"] = locale or "en-US"
    os.environ["LOCAL_TZ"] = identity.timezone if identity else "UTC"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config. File values win over process env for fingerprint.browser.

    Exception: FINGERPRINT_BROWSER_FORCE=1 keeps env as source of truth.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    candidates: list[Path] = []
    explicit_missing: list[Path] = []
    if path:
        p = Path(path)
        candidates.append(p)
        if not p.is_file():
            explicit_missing.append(p)
    else:
        env_path = os.environ.get("GPT_REGISTER_CONFIG", "").strip()
        if env_path:
            p = Path(env_path)
            candidates.append(p)
            if not p.is_file():
                explicit_missing.append(p)
        candidates.extend(
            [
                ROOT / "config.yaml",
                ROOT / "config.yml",
                ROOT / "config.json",
            ]
        )
    # 显式指定的配置路径（-c 参数 / 环境变量）不存在时直接报错：
    # 静默回退默认配置（空邮箱池、禁代理）会把故障延迟暴露且极难排查
    if explicit_missing:
        missing_list = "、".join(str(p) for p in explicit_missing)
        raise FileNotFoundError(f"指定的配置文件不存在: {missing_list}")

    loaded_from: Path | None = None
    for p in candidates:
        if p.is_file():
            file_cfg = _load_yaml_or_json(p)
            # providers is a list — file list replaces default list entirely
            # (deep_merge would not replace lists correctly for partial providers)
            if (
                isinstance(file_cfg.get("mail"), dict)
                and isinstance(file_cfg["mail"].get("providers"), list)
            ):
                cfg.setdefault("mail", {})["providers"] = file_cfg["mail"]["providers"]
            _deep_merge(cfg, file_cfg)
            loaded_from = p
            break

    # Env can still set browser only if no file value and env present, or FORCE
    force = os.environ.get("FINGERPRINT_BROWSER_FORCE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    env_browser = os.environ.get("FINGERPRINT_BROWSER", "").strip().lower()
    if force and env_browser:
        cfg.setdefault("fingerprint", {})["browser"] = env_browser
    elif env_browser and loaded_from is None:
        cfg.setdefault("fingerprint", {})["browser"] = env_browser

    # Optional env override for chatgpt2api (CLI / docker)
    env_c2a_url = os.environ.get("CHATGPT2API_URL", "").strip()
    env_c2a_key = os.environ.get("CHATGPT2API_KEY", "").strip()
    if env_c2a_url or env_c2a_key:
        c2a = cfg.setdefault("chatgpt2api", {})
        if env_c2a_url:
            c2a["url"] = env_c2a_url
        if env_c2a_key:
            c2a["key"] = env_c2a_key

    apply_fingerprint_env(cfg)
    if loaded_from is not None:
        cfg["_loaded_from"] = str(loaded_from)
    return cfg


def write_example_config(dest: str | Path) -> None:
    """Write a starter config.yaml (from example file or DEFAULT_CONFIG)."""
    dest_path = Path(dest)
    example = ROOT / "config.example.yaml"
    if example.is_file():
        dest_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        return
    try:
        import yaml  # type: ignore

        text = yaml.safe_dump(
            {k: v for k, v in DEFAULT_CONFIG.items() if not str(k).startswith("_")},
            allow_unicode=True,
            sort_keys=False,
        )
    except Exception:
        text = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n"
    dest_path.write_text(text, encoding="utf-8")
