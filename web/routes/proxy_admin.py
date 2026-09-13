"""Registration proxy pool configuration and connectivity test routes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes this router.
    import web_app

    return web_app


logger = logging.getLogger(__name__)


class ProxyUpdate(BaseModel):
    enabled: bool = False
    mode: str = "single_proxy"
    url: str = ""
    resource_url: str = ""
    pool_file: str = ""
    pool_text: str | None = None
    selected_country: str = ""
    country_pools: list[dict[str, Any]] | None = None
    rotate_per_task: bool = True
    clearance: dict[str, Any] | None = None


class ProxyTestRequest(BaseModel):
    proxies: list[str] = Field(default_factory=list)


def _resolve_pool_path(pool_file: str) -> Path:
    """解析代理池文件路径，并强制限制在项目 data 目录内。

    防止通过配置任意路径读取服务器上的敏感文件（如 config.yaml）。
    """
    path = Path(pool_file).expanduser()
    if not path.is_absolute():
        path = (_state().ROOT / path).resolve()
    else:
        path = path.resolve()
    try:
        path.relative_to(_state().DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "代理池文件必须位于项目 data 目录")
    return path


def _proxy_pool_preview(cfg: dict[str, Any]) -> dict[str, Any]:
    proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    pool_file = str(proxy.get("pool_file") or "").strip()
    lines: list[str] = []
    if pool_file:
        path = Path(pool_file).expanduser()
        if not path.is_absolute():
            path = _state().ROOT / path
        # Canonicalize absolute paths too. Windows may expose the temp path in
        # 8.3 form while DATA_DIR uses its long form, which would otherwise
        # fail the containment check for the same directory.
        path = path.resolve()
        try:
            # 读取前强制校验路径仍位于 data 目录，防止历史配置残留任意路径
            path.relative_to(_state().DATA_DIR.resolve())
        except ValueError:
            path = Path("")
        if path.is_file():
            try:
                lines = [
                    ln.strip()
                    for ln in path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
            except Exception:
                lines = []
    country_pools = _state().read_registration_proxy_pools()
    selected_country = str(proxy.get("selected_country") or "").upper().strip()
    selected_pool = next(
        (item for item in country_pools["items"] if item["country"] == selected_country),
        None,
    )
    selected_count = int((selected_pool or {}).get("count") or 0)
    mode = str(proxy.get("mode") or "single_proxy")
    return {
        "enabled": bool(proxy.get("enabled")),
        "mode": mode,
        "url": str(proxy.get("url") or ""),
        "resource_url": str(proxy.get("resource_url") or ""),
        "clearance": dict(proxy.get("clearance") or {}) if isinstance(proxy.get("clearance"), dict) else {},
        "pool_file": pool_file,
        "pool_count": selected_count if mode == "country_pool" else len(lines),
        "pool_unique_count": selected_count if mode == "country_pool" else len(set(lines)),
        "rotate_per_task": bool(proxy.get("rotate_per_task", True)),
        "pool_preview": lines[:20],
        "pool_text": "\n".join(lines),
        "selected_country": selected_country,
        "country_pools": country_pools,
    }


def _warm_panel_caches() -> None:
    """Prime the expensive mailbox/status snapshot after a service restart."""
    try:
        _state()._public_mail_stats(_state()._read_config())
    except Exception as error:
        # Cache warming is opportunistic; the first request can still build a
        # fresh snapshot if a provider or a partially-written data file fails.
        logger.debug("panel cache warm-up skipped: %s", error)


@router.put("/api/proxy")
async def put_proxy(body: ProxyUpdate):
    # Proxy settings are persisted independently from an active batch. A
    # request already in flight keeps its selected proxy; protocol workers
    # refresh this saved pool before the next task or retry.
    country_pools = _state().read_registration_proxy_pools()
    if body.country_pools is not None:
        try:
            country_pools = _state().write_registration_proxy_pools(body.country_pools)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    selected_country = str(body.selected_country or "").upper().strip()
    known_countries = {item["country"]: item for item in country_pools["items"]}
    if selected_country and selected_country not in known_countries:
        raise HTTPException(400, "当前国家未在主注册代理池中")
    mode = body.mode if body.mode in {"single_proxy", "pool", "country_pool"} else "single_proxy"
    if mode == "country_pool":
        if not selected_country:
            raise HTTPException(400, "请选择主注册代理国家")
        if not int(known_countries[selected_country].get("count") or 0):
            raise HTTPException(400, "所选国家没有可用代理")

    with _state()._edit_config() as cfg:
        proxy = cfg.setdefault("proxy", {})
        proxy["enabled"] = body.enabled
        proxy["mode"] = mode
        proxy["url"] = body.url or ""
        proxy["resource_url"] = body.resource_url or ""
        proxy["selected_country"] = selected_country
        proxy["rotate_per_task"] = body.rotate_per_task
        if body.clearance is not None:
            raw = body.clearance
            mode = str(raw.get("mode") or "none").strip().lower()
            proxy["clearance"] = {
                "enabled": bool(raw.get("enabled")),
                "mode": mode if mode in {"none", "flaresolverr"} else "none",
                "flaresolverr_url": str(raw.get("flaresolverr_url") or "").strip(),
                "proxy_url": str(raw.get("proxy_url") or "").strip(),
                "timeout_sec": max(1, min(300, int(raw.get("timeout_sec") or 60))),
                "refresh_interval": max(0, int(raw.get("refresh_interval") or 3600)),
            }
        pool_file = (body.pool_file or "").strip()
        # 无论是否提交 pool_text，pool_file 本身都必须位于 data 目录内
        if pool_file:
            _resolve_pool_path(pool_file)
        if body.pool_text is not None:
            if not pool_file:
                pool_file = str(_state().DATA_DIR / "proxy_pool.txt")
            path = _resolve_pool_path(pool_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.pool_text.rstrip() + "\n", encoding="utf-8")
            try:
                rel = path.relative_to(_state().ROOT)
                pool_file = str(rel)
            except ValueError:
                pool_file = str(path)
        proxy["pool_file"] = pool_file
    return {"ok": True, "proxy": _proxy_pool_preview(cfg)}


@router.post("/api/proxy/test")
async def proxy_test(body: ProxyTestRequest):
    from register.batch import resolve_proxy_pool

    cfg = _state()._read_config()
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    proxies = [str(value or "").strip() for value in body.proxies if str(value or "").strip()]
    if not proxies:
        proxies = resolve_proxy_pool(proxy_cfg)
        single = str(proxy_cfg.get("url") or "").strip()
        if not proxies and single:
            proxies = [single]
    if not proxies:
        raise HTTPException(400, "没有可测试的代理")
    targets = proxies[:100]
    _state().PROGRESS.start("proxy_test", "代理批量检测", total=len(targets), stage="检测出口连通性")
    items: list[dict[str, Any]] = []
    try:
        tasks = [asyncio.create_task(asyncio.to_thread(_state().probe_proxy, proxy)) for proxy in targets]
        for future in asyncio.as_completed(tasks):
            items.append(await future)
            _state().PROGRESS.update(
                "proxy_test",
                done=len(items),
                total=len(targets),
                stage=f"已检测 {len(items)} / {len(targets)}",
            )
    except Exception as exc:
        _state().PROGRESS.finish("proxy_test", ok=False, message=str(exc))
        raise
    alive = sum(1 for item in items if item.get("ok"))
    _state().PROGRESS.finish("proxy_test", message=f"可用 {alive} / {len(items)}")
    return {
        "ok": True,
        "count": len(items),
        "alive": alive,
        "items": items,
    }
