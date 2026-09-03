"""Trial eligibility and account health check routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from web.routes.shared_models import BulkDeleteRequest

router = APIRouter()
router_health = APIRouter()


def _state() -> Any:
    # Delayed import is intentional: web_app includes these routers.
    import web_app

    return web_app


class AccountHealthRequest(BulkDeleteRequest):
    workers: int = Field(10, ge=1, le=20)


class AccountTrialEligibilityRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=1000)
    all_accounts: bool = False
    force: bool = False
    concurrency: int = Field(default=5, ge=1, le=5)


class TokenTrialEligibilityRequest(BaseModel):
    access_token: str = ""
    use_configured_proxy: bool = True


class TokenTrialEligibilityBatchRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    use_configured_proxy: bool = True
    workers: int = Field(default=4, ge=1, le=8)


class TrialProxyCountryItem(BaseModel):
    country: str = Field(min_length=2, max_length=2)
    label: str = Field(default="", max_length=40)
    text: str = Field(default="", max_length=2_000_000)


class TrialProxyConfigRequest(BaseModel):
    selected_country: str = Field(default="", max_length=2)
    countries: list[TrialProxyCountryItem] = Field(default_factory=list, max_length=100)


@router.post("/api/accounts/trial-eligibility")
async def accounts_trial_eligibility(body: AccountTrialEligibilityRequest):
    """Queue direct Plus/free-trial checks for selected or all accounts."""
    records = await asyncio.to_thread(_state()._load_accounts)
    wanted = {str(key or "").strip() for key in body.keys if str(key or "").strip()}
    selected = [
        record for record in records
        if (body.all_accounts or _state()._account_key(record) in wanted)
        and str(record.get("plan_type") or "free").strip().lower() == "free"
        and not _state().account_is_deactivated(record)
    ]
    if not selected:
        raise HTTPException(400, "没有可检测的 Free 账号")
    effective_concurrency, resource_mode = _state()._trial_eligibility_resource_budget(
        body.concurrency
    )
    try:
        job = await asyncio.to_thread(
            _state().TRIAL_ELIGIBILITY_JOBS.start,
            selected,
            concurrency=effective_concurrency,
            attempts=8,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {
        "ok": True,
        **job,
        "requested": len(
            [
                record
                for record in records
                if (body.all_accounts or _state()._account_key(record) in wanted)
                and str(record.get("plan_type") or "free").strip().lower() == "free"
            ]
        ),
        "skipped_known_deactivated": max(
            0,
            len(
                [
                    record
                    for record in records
                    if (body.all_accounts or _state()._account_key(record) in wanted)
                    and str(record.get("plan_type") or "free").strip().lower() == "free"
                    and _state().account_is_deactivated(record)
                ]
            ),
        ),
        "requested_concurrency": body.concurrency,
        "effective_concurrency": effective_concurrency,
        "resource_mode": resource_mode,
    }


@router.get("/api/accounts/trial-eligibility/progress")
async def accounts_trial_eligibility_progress():
    return {"ok": True, "job": await asyncio.to_thread(_state().TRIAL_ELIGIBILITY_JOBS.active)}


@router.get("/api/accounts/trial-eligibility/{job_id}")
async def accounts_trial_eligibility_job(job_id: str):
    job = await asyncio.to_thread(_state().TRIAL_ELIGIBILITY_JOBS.snapshot, job_id)
    if job is None:
        raise HTTPException(404, "优惠检测任务不存在或已清理")
    return {"ok": True, **job}


@router.post("/api/accounts/trial-eligibility/{job_id}/stop")
async def accounts_trial_eligibility_stop(job_id: str):
    job = await asyncio.to_thread(_state().TRIAL_ELIGIBILITY_JOBS.stop, job_id)
    if job is None:
        raise HTTPException(404, "优惠检测任务不存在或已清理")
    return {"ok": True, **job}


async def _token_trial_eligibility_core(token: str, use_configured_proxy: bool) -> dict[str, Any]:
    """Single-token check shared by the single and batch endpoints."""
    country = ""
    if use_configured_proxy:
        country, first_proxy = await asyncio.to_thread(_state().selected_trial_proxy)
        if first_proxy:
            _, remaining_proxies = await asyncio.to_thread(
                _state().selected_trial_proxies,
                limit=4,
                exclude={first_proxy},
            )
            attempts = [first_proxy, *remaining_proxies]
        else:
            attempts = [""]
    else:
        attempts = [""]

    result: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt, proxy in enumerate(attempts, 1):
        try:
            result = await asyncio.to_thread(
                _state().check_trial_eligibility,
                {"access_token": token},
                proxy=proxy,
                direct=not bool(proxy),
            )
            break
        except _state().InvalidAccessTokenError as error:
            raise HTTPException(400, "Access Token 已失效或无效") from error
        except Exception as error:
            last_error = error
            if not _state().is_trial_probe_retryable(error) or attempt >= len(attempts):
                break
    if result is None:
        error = last_error or _state().TrialEligibilityError("资格检测失败")
        if isinstance(error, _state().TrialEligibilityError):
            raise HTTPException(502, f"资格接口检测失败: {str(error)[:240]}") from error
        raise HTTPException(502, f"资格检测请求失败: {str(error)[:240]}") from error

    campaigns = result.get("eligible_promo_campaigns")
    campaigns = campaigns if isinstance(campaigns, dict) else {}
    active_campaigns = {
        str(name): value
        for name, value in campaigns.items()
        if value
    }
    return {
        "ok": True,
        **result,
        "eligible_promo_campaigns": active_campaigns,
        "has_promotion": bool(active_campaigns),
        "promotion_count": len(active_campaigns),
        "promotion_keys": list(active_campaigns),
        "proxy_country": country if attempts and attempts[0] else "",
    }


def _normalize_access_token(raw: str) -> str:
    token = str(raw or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


@router.post("/api/trial-eligibility/check-token")
async def token_trial_eligibility(body: TokenTrialEligibilityRequest):
    """Check one supplied AT without persisting or returning the credential."""
    token = _normalize_access_token(body.access_token)
    if len(token) < 20 or len(token) > 20_000:
        raise HTTPException(400, "请输入完整的 Access Token")
    return await _token_trial_eligibility_core(token, body.use_configured_proxy)


@router.post("/api/trial-eligibility/check-tokens")
async def token_trial_eligibility_batch(body: TokenTrialEligibilityBatchRequest):
    """Batch-check supplied ATs with bounded concurrency."""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in body.access_tokens:
        token = _normalize_access_token(raw)
        if len(token) < 20 or len(token) > 20_000 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    if not tokens:
        raise HTTPException(400, "请输入至少一个有效的 Access Token")
    if len(tokens) > 200:
        raise HTTPException(400, "单次最多批量检测 200 个 Access Token")

    workers = max(1, min(int(body.workers), 8))
    semaphore = asyncio.Semaphore(workers)

    async def run_one(index: int, token: str) -> dict[str, Any]:
        tail = token[-10:]
        async with semaphore:
            try:
                payload = await _token_trial_eligibility_core(token, body.use_configured_proxy)
            except HTTPException as error:
                detail = str(error.detail) if error.detail is not None else "检测失败"
                return {"index": index, "token_tail": tail, "ok": False, "error": detail[:240]}
            except Exception as error:
                return {"index": index, "token_tail": tail, "ok": False, "error": str(error)[:240]}
            return {"index": index, "token_tail": tail, **payload}

    results = list(await asyncio.gather(*(run_one(i, token) for i, token in enumerate(tokens))))
    eligible = sum(1 for item in results if item.get("ok") and item.get("eligible"))
    plus_active = sum(1 for item in results if item.get("ok") and item.get("has_active_subscription"))
    banned = sum(1 for item in results if item.get("ok") and item.get("is_deactivated"))
    failed = sum(1 for item in results if not item.get("ok"))
    return {
        "ok": True,
        "total": len(results),
        "eligible": eligible,
        "plus_active": plus_active,
        "banned": banned,
        "failed": failed,
        "workers": workers,
        "results": results,
    }


@router.get("/api/trial-proxy-pool")
async def trial_proxy_pool():
    return {"ok": True, **await asyncio.to_thread(_state().read_trial_proxy_config)}


@router.put("/api/trial-proxy-pool")
async def trial_proxy_pool_save(body: TrialProxyConfigRequest):
    try:
        config = await asyncio.to_thread(
            _state().write_trial_proxy_config,
            body.selected_country,
            [item.model_dump() for item in body.countries],
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, **config}


@router_health.post("/api/accounts/health")
async def accounts_health(body: AccountHealthRequest):
    from common.account_health import check_accounts

    if not body.delete_all and not body.keys:
        raise HTTPException(400, "请选择要测活的账号，或明确使用全部测活")
    records = await asyncio.to_thread(_state()._load_accounts)
    keys = [] if body.delete_all else body.keys
    probe_records, skipped_records = _state().select_health_check_records(records, keys=keys)
    requested = len(probe_records) + len(skipped_records)
    actual_total = len(probe_records)
    if not requested:
        raise HTTPException(404, "没有匹配到可测活的账号记录")
    if _state()._account_health_progress_snapshot(include_updates=False).get("running"):
        raise HTTPException(409, "账号批量测活正在运行，请等待当前任务完成")
    _state()._start_account_health_progress(
        actual_total,
        body.workers,
        requested=requested,
        skipped_known_deactivated=len(skipped_records),
    )
    try:
        result = await asyncio.to_thread(
            check_accounts,
            records=records,
            keys=keys,
            workers=body.workers,
            item_callback=_state()._update_account_health_progress,
            worker_limit=_state()._account_health_worker_limit,
            proxy_candidates=_state()._current_health_proxy_pool(),
        )
    except asyncio.CancelledError:
        _state()._finish_account_health_progress(message="测活请求已取消")
        raise
    except Exception as error:
        _state()._finish_account_health_progress(message=str(error))
        raise
    try:
        replacement = await asyncio.to_thread(
            _state()._replace_cdks_after_health,
            records,
            result["items"],
        )
    except Exception as error:
        _state()._finish_account_health_progress(message=str(error))
        raise
    _state()._finish_account_health_progress(
        cdk_replaced=replacement["replaced"],
        cdk_unresolved=replacement["unresolved"],
        result=result,
    )
    # The UI refreshes its paginated account view after completion. Returning
    # every checked row here duplicates that response and can be several MB for
    # an all-account run, so keep row payloads for small targeted checks only.
    include_rows = requested <= _state()._ACCOUNT_HEALTH_INLINE_UPDATE_LIMIT
    response = {
        "ok": True,
        "requested": result["requested"],
        "actual_total": result["actual_total"],
        "checked": result["checked"],
        "usable": result["usable"],
        "unavailable": result["unavailable"],
        "skipped_known_deactivated": result["skipped_known_deactivated"],
        "deactivated": result["deactivated"],
        "invalid_other": result["invalid_other"],
        "alive": result["alive"],
        "limited": result["limited"],
        "invalid": result["invalid"],
        "reauth_required": result["reauth_required"],
        "error": result["error"],
        "cdk_replaced": replacement["replaced"],
        "cdk_unresolved": replacement["unresolved"],
        "items": (
            [_state().account_public_view(item) for item in result["items"]]
            if include_rows
            else []
        ),
        "skipped_items": (
            [
                _state().account_public_view(item)
                for item in result.get("skipped_items", [])
            ]
            if include_rows
            else []
        ),
        "items_omitted": not include_rows,
    }
    return response


@router_health.get("/api/accounts/health/progress")
async def accounts_health_progress(include_updates: bool = True):
    return {
        "ok": True,
        **_state()._account_health_progress_snapshot(include_updates=include_updates),
    }
