# -*- coding: utf-8 -*-
"""Single-account ChatGPT registration flow."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback as _tb
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.browser import open_and_connect, teardown, react_fill, _write_browser_diagnostic
from common.cookies import save_platform_cookies
from common.session_export import (
    build_account_record,
    fetch_chatgpt_session,
    fetch_platform_refresh_tokens,
    save_chatgpt_tokens,
)
from common.chatgpt2api import upload_session
from common.mail_registration_metrics import begin_attempt
from config import CHATGPT_CF_NODES
from mail import (
    create_mailbox,
    mark_mailbox_code_boundary,
    mark_mailbox_result,
    mark_smsbower_otp_accepted,
    release_mailbox,
    wait_for_code,
)
from proxy_util import is_proxy_transport_error, proxy_brief
from register.log import (
    _log,
    _TASK_IDX,
    _TASK_EMAIL,
    _ensure_print_prefix,
    _otp_channel_label,
)
from register.sessions import (
    JobCancelled,
    _default_stop_check,
    _register_session,
    _unregister_session,
)
from register.registration_outcome import RegisteredEmailDetected, requires_fresh_registration
from register.util import (
    ROOT as PROJECT_ROOT,
    PLATFORM,
    SIGNUP_URL,
    KEY_COOKIES,
    REGISTER_TIMEOUT,
    rand_password,
    _append_account,
)
from register.page import (
    _page_url,
    _is_login_url,
    _is_navigation_race,
    login_email_query,
    _is_about_you_url,
    _is_verify_url,
    _is_existing_account_password_url,
    _is_auth_error_url,
    auth_error_code_from_url,
    dump_state,
    safe_body_text,
    safe_count,
    wait_for_any,
    is_chatgpt_main,
    click_exact,
    click_any_exact,
    switch_password_to_email_code,
    install_fresh_registration_guard,
    check_fresh_registration_page,
    click_submit_or_enter,
    dismiss_cookie_banner,
    fill_email_verified,
    submit_email_continue,
    submit_email_browser_ui,
    start_nextauth_email_flow,
    detect_challenge,
    _is_cf_blocked,
    _click_turnstile,
    _switch_cf_node,
    _click_resend_code,
    EMAIL_INPUT_SELECTOR,
    OTP_INPUT_SELECTOR,
    fill_otp_verified,
)
from register.onboarding import handle_onboarding, is_identity_verification_page
import logging

logger = logging.getLogger(__name__)


def _otp_resend_accepted(status: int | None) -> bool:
    """Only a 2xx response means the server issued a fresh OTP."""
    try:
        value = int(status or 0)
    except (TypeError, ValueError):
        return False
    return 200 <= value < 300


@dataclass(frozen=True)
class OtpSubmitResult:
    advanced: bool = False
    accepted: bool = False
    status: int = 0
    continue_url: str = ""

    @property
    def should_resend(self) -> bool:
        return not self.accepted and not self.advanced


def _find_otp_continue_url(value: Any, depth: int = 0) -> str:
    """Find the auth continuation in current and nested OTP response payloads."""
    if depth > 5:
        return ""
    if isinstance(value, dict):
        for key in (
            "continue_url",
            "continueUrl",
            "redirect_url",
            "redirectUrl",
            "url",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            candidate = _find_otp_continue_url(child, depth + 1)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for child in value:
            candidate = _find_otp_continue_url(child, depth + 1)
            if candidate:
                return candidate
    return ""


def _trusted_otp_continue_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        candidate = urljoin("https://auth.openai.com/", raw)
        parsed = urlparse(candidate)
    except Exception:
        return ""
    if parsed.scheme != "https" or parsed.hostname not in {
        "auth.openai.com",
        "chatgpt.com",
    }:
        return ""
    return candidate


def _otp_confirmation_present(body_text: str) -> bool:
    """Recognize the terminal email-confirmation page across supported locales."""
    text = " ".join(str(body_text or "").lower().split())
    return any(
        marker in text
        for marker in (
            "email has been verified",
            "email is already verified",
            "email address is already verified",
            "already been verified",
            "メールが確認されました",
            "確認済み",
            "邮箱已验证",
            "郵箱已驗證",
        )
    )


async def _read_otp_response(response) -> tuple[int, dict[str, Any]]:
    try:
        status = int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    try:
        payload = await asyncio.wait_for(response.json(), timeout=3)
    except Exception:
        payload = {}
        try:
            raw = await asyncio.wait_for(response.text(), timeout=2)
            decoded = json.loads(raw or "{}")
            if isinstance(decoded, dict):
                payload = decoded
        except Exception:
            logger.debug("_read_otp_response: ignoring exception", exc_info=True)
    return status, payload if isinstance(payload, dict) else {}


async def _recover_after_accepted_otp(page, continue_url: str = "") -> bool:
    target = _trusted_otp_continue_url(continue_url)
    if not target:
        target = "https://auth.openai.com/about-you"
        _log("[4] OTP 已被接受但页面未跳转，使用当前会话恢复资料页")
    else:
        _log(f"[4] OTP 已被接受，跟随服务端继续地址 · {urlparse(target).path}")
    try:
        await page.goto(target, timeout=20000, wait_until="commit")
    except Exception as error:
        if not _is_navigation_race(error):
            _log(f"[4] OTP 后续导航异常: {str(error).splitlines()[0][:100]}")
    await wait_for_any(
        page,
        selectors=['input[name="age"]', 'input[name="name"]', '#prompt-textarea'],
        url_substrings=["about-you", "chatgpt.com/", "callback"],
        timeout=8,
        poll=0.25,
    )
    current = await _page_url(page)
    return (
        not _is_verify_url(current)
        or _is_about_you_url(current)
        or await is_chatgpt_main(page)
        or await safe_count(page, 'input[name="age"], input[name="name"]') > 0
    )


async def _poll_auto_sent_code_before_resend(
    page,
    mail_config: dict[str, Any],
    mailbox: dict[str, Any],
    stop_check,
    *,
    allow_http_fallback: bool = True,
) -> tuple[str | None, int]:
    """Use the OTP automatically sent on route entry before requesting another."""
    normal_wait = max(10.0, float(mail_config.get("wait_timeout") or 90))
    first_wait_timeout = max(30.0, min(60.0, normal_wait))
    first_wait = dict(
        mail_config,
        wait_timeout=first_wait_timeout,
        wait_chunk=min(
            first_wait_timeout,
            max(30.0, float(mail_config.get("wait_chunk") or 60)),
        ),
        request_timeout=max(15.0, float(mail_config.get("request_timeout") or 30)),
    )
    try:
        code = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: wait_for_code(first_wait, mailbox, stop_check=stop_check),
        )
    except JobCancelled:
        raise
    except Exception as error:
        _log(f"[4] 首封验证码读取失败，转为补发: {str(error)[:100]}")
        code = None
    if callable(stop_check) and stop_check():
        raise JobCancelled("stop requested")
    if code:
        return str(code), 200

    mark_mailbox_code_boundary(mailbox, grace_seconds=0)
    return None, await _click_resend_code(
        page, allow_http_fallback=allow_http_fallback
    )


async def _submit_verification_code(
    page, code: str, timeout: float = 12, *, mailbox: dict[str, Any] | None = None
) -> OtpSubmitResult:
    """Submit one OTP while preserving acceptance separately from SPA routing."""
    loop = asyncio.get_running_loop()
    response_future = loop.create_future()
    acceptance_recorded = False

    def record_acceptance() -> None:
        nonlocal acceptance_recorded
        if mailbox is not None and not acceptance_recorded:
            mark_smsbower_otp_accepted(mailbox)
            acceptance_recorded = True

    def capture_otp_response(response) -> None:
        try:
            url = str(response.url or "")
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "auth.openai.com"
                or parsed.path.rstrip("/") != "/api/accounts/email-otp/validate"
                or response_future.done()
            ):
                return
            response_future.set_result(response)
            if 200 <= int(response.status) < 300:
                # Record consumption before the next page wait can be
                # cancelled or fail while the accepted OTP is navigating.
                record_acceptance()
        except Exception:
            logger.debug("capture_otp_response: ignoring exception", exc_info=True)

    try:
        page.on("response", capture_otp_response)
    except Exception:
        logger.debug("_submit_verification_code: ignoring exception", exc_info=True)
    try:
        await dismiss_cookie_banner(page)
        if not await fill_otp_verified(page, code, tries=2):
            _log("[4] 验证码填写未生效（已重试）")

        # Some OTP forms submit as soon as the final digit is typed.
        await wait_for_any(
            page,
            selectors=['input[name="age"]', 'input[name="name"]', '#prompt-textarea'],
            url_substrings=["about-you", "chatgpt.com/", "callback"],
            timeout=2,
        )
        current = await _page_url(page)
        if _is_verify_url(current) and not _is_about_you_url(current):
            if not await click_any_exact(
                page,
                [
                    "Continue", "続行", "Verify", "確認", "确认", "继续",
                    "Submit", "次へ", "Teruskan", "Sahkan",
                ],
            ):
                await click_submit_or_enter(page, OTP_INPUT_SELECTOR)
            await wait_for_any(
                page,
                selectors=['input[name="age"]', 'input[name="name"]', '#prompt-textarea'],
                url_substrings=["about-you", "chatgpt.com/", "callback"],
                timeout=max(1, timeout - 2),
            )

        response = response_future.result() if response_future.done() else None
        if response is None:
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(response_future), timeout=1.5
                )
            except Exception:
                response = None
        status, payload = await _read_otp_response(response) if response else (0, {})
        accepted = 200 <= status < 300
        if accepted:
            record_acceptance()
        continue_url = _trusted_otp_continue_url(_find_otp_continue_url(payload))
        if status:
            result_text = "已接受" if accepted else "未通过"
            _log(f"[4] 验证码校验接口{result_text} · HTTP {status}")

        current = await _page_url(page)
        advanced = (
            not _is_verify_url(current)
            or _is_about_you_url(current)
            or await is_chatgpt_main(page)
            or await safe_count(page, 'input[name="age"], input[name="name"]') > 0
        )
        if not advanced:
            confirmation_body = await safe_body_text(page, limit=1600, timeout=2)
            confirmed_on_page = _otp_confirmation_present(confirmation_body)
            if accepted or confirmed_on_page:
                record_acceptance()
                if confirmed_on_page and not accepted:
                    _log("[4] 页面确认邮箱已验证，按已接受状态恢复后续流程")
                advanced = await _recover_after_accepted_otp(page, continue_url)
                accepted = True
        if not advanced and not status:
            _log("[4] 未观察到验证码校验接口响应，页面仍停留在验证码步骤")
        return OtpSubmitResult(
            advanced=advanced,
            accepted=accepted,
            status=status,
            continue_url=continue_url,
        )
    finally:
        if not response_future.done():
            response_future.cancel()
        try:
            page.remove_listener("response", capture_otp_response)
        except Exception:
            logger.debug("_submit_verification_code: ignoring exception", exc_info=True)


async def register_one(
    index: int,
    total: int,
    p,
    *,
    mail_config: dict[str, Any],
    proxy: str = "",
    fixed_email: str | None = None,
    provider_type: str | None = None,
    timeout: int = REGISTER_TIMEOUT,
    keep_on_fail: bool = False,
    stop_check=None,
    task_event=None,
    c2a_config: dict[str, Any] | None = None,
    group_id: str = "",
    hybrid_mode: bool = False,
    persist_result: bool = True,
    defer_success_event: bool = False,
    mailbox_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Register one ChatGPT account via fingerprint browser."""
    token = _TASK_IDX.set(index)
    email_token = _TASK_EMAIL.set("")
    overall_start = time.time()
    start = time.time()
    mailbox: dict[str, Any] | None = None
    email = ""
    password = rand_password()
    password_submitted = False
    bb = pid = None
    browser = ctx = page = None
    success = False
    diagnostic_written = False
    last_error = ""
    _stop = stop_check if callable(stop_check) else _default_stop_check
    _ensure_print_prefix()

    def emit(status: str | None = None, **fields: Any) -> None:
        if callable(task_event):
            try:
                task_event(index, status, **fields)
            except Exception:
                logger.debug("emit: ignoring exception", exc_info=True)

    def check_timeout():
        if _stop():
            raise JobCancelled("stop requested")
        if time.time() - start > timeout:
            raise TimeoutError(f"timeout {timeout}s")

    def check_stop():
        if _stop():
            raise JobCancelled("stop requested")

    def _mask_email(value: str) -> str:
        raw = str(value or "")
        if "@" not in raw:
            return raw[:2] + "***" if raw else ""
        name, domain = raw.split("@", 1)
        return f"{name[:2]}***@{domain}"

    def _redact_runtime_text(value: str) -> str:
        text = str(value or "")
        for secret in (email, password):
            if secret:
                text = text.replace(secret, "***")
        return text

    async def _capture_registration_diagnostic(reason: str, *, force: bool = False) -> None:
        nonlocal diagnostic_written
        if diagnostic_written and not force:
            return
        if page is None:
            return
        diagnostic_written = True
        body_snippet = ""
        try:
            body_snippet = _redact_runtime_text(
                await safe_body_text(page, limit=1200, timeout=2)
            )
        except Exception as error:
            body_snippet = f"<body unavailable: {type(error).__name__}>"
        try:
            path = await _write_browser_diagnostic(
                "registration_failure",
                provider=getattr(bb, "provider_name", "") if bb else "",
                profile_id=str(pid or ""),
                proxy=proxy,
                elapsed=time.time() - start if start else None,
                error=last_error or reason,
                page=page,
                screenshot=True,
                extra={
                    "index": index,
                    "total": total,
                    "reason": reason,
                    "email": _mask_email(email),
                    "body_snippet": body_snippet[:1200],
                },
            )
            if path:
                _log(f"[诊断] 浏览器失败现场已保存: {path}")
        except Exception:
            logger.debug("_capture_registration_diagnostic: ignoring exception", exc_info=True)

    try:
        emit("queued", step="等待分配邮箱", proxy=proxy_brief(proxy))
        check_stop()
        mailbox = (
            mailbox_override
            if mailbox_override is not None
            else create_mailbox(mail_config, username=fixed_email, provider_type=provider_type)
        )
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        if requires_fresh_registration(mailbox):
            begin_attempt(mailbox)
        _TASK_EMAIL.set(email)
        prov = str(mailbox.get("provider") or provider_type or "?")
        emit(
            "running",
            email=email,
            provider=prov,
            proxy=proxy_brief(proxy),
            step=f"已分配邮箱 {email}",
        )
        _log(f"开始 #{index}/{total} · 类型={prov} · 代理={proxy_brief(proxy)}")
        check_stop()

        name = f"chatgpt_{time.strftime('%m%d_%H%M%S')}_{index}"
        bb, pid, browser, ctx, page = await open_and_connect(
            name=name, p=p, proxy=proxy or None
        )
        _register_session(bb, pid)
        emit("running", pid=str(pid), step=f"浏览器已创建 {pid}")
        await ctx.clear_cookies()
        await install_fresh_registration_guard(ctx, mailbox)
        # Browser startup is infrastructure time; the registration timeout starts
        # when the page flow can actually begin.
        start = time.time()

        emit("running", step="打开注册页")
        _log("[1] 打开注册页…")
        goto_ok = False
        for attempt in range(2):
            try:
                # commit: first byte is enough; full load finishes while we wait for email input
                await page.goto(
                    SIGNUP_URL, timeout=20000, wait_until="commit"
                )
                goto_ok = True
                break
            except Exception as e:
                # Navigation often "times out" after URL is already on login
                try:
                    cur = (getattr(page, "url", "") or "").lower()
                    if "auth/login" in cur or "chatgpt.com" in cur:
                        goto_ok = True
                        _log(f"打开注册页超时但页面已到达: {cur[:80]}")
                        break
                except Exception:
                    logger.debug("register_one: ignoring exception", exc_info=True)
                _log(f"打开注册页重试 {attempt + 1}/2: {str(e)[:70]}")
                await asyncio.sleep(0.5)
        if not goto_ok:
            last_error = "goto_failed"
            _log("打开注册页失败（已重试）")
            return None
        # Wait for email field (SPA may still hydrate after commit)
        email_ready = await wait_for_any(
            page,
            selectors=[EMAIL_INPUT_SELECTOR],
            timeout=15,
        )
        if not email_ready:
            # one soft reload if shell loaded but input missing
            try:
                cur = (getattr(page, "url", "") or "").lower()
                if "auth/login" in cur:
                    await page.reload(timeout=15000, wait_until="commit")
                    await wait_for_any(
                        page,
                        selectors=[EMAIL_INPUT_SELECTOR],
                        timeout=10,
                    )
            except Exception:
                logger.debug("register_one: ignoring exception", exc_info=True)
        check_stop()
        await dump_state(page, "加载后")

        async def _try_pass_turnstile(rounds=2, wait=1.5):
            for _ in range(rounds):
                if not await _is_cf_blocked(page):
                    return True
                await _click_turnstile(page)
                await asyncio.sleep(wait)
            return not await _is_cf_blocked(page)

        if await _is_cf_blocked(page):
            _log("[CF] 检测到 Cloudflare 验证，尝试点击…")
            if await _try_pass_turnstile():
                _log("[CF] Turnstile 已通过")
            else:
                passed = False
                for cf_try in range(max(1, len(CHATGPT_CF_NODES) or 1)):
                    # 同步 requests 会阻塞事件循环最长 13s（8s PUT + 5s DELETE），
                    # 期间所有并发任务的 OTP 临界区全部停摆，必须下放线程
                    node = await asyncio.to_thread(_switch_cf_node)
                    if not node:
                        break
                    _log(f"[CF] 切换节点 -> {node} ({cf_try + 1})")
                    await asyncio.sleep(1.5)
                    try:
                        await page.goto(
                            SIGNUP_URL, timeout=20000, wait_until="commit"
                        )
                    except Exception:
                        logger.debug("register_one: ignoring exception", exc_info=True)
                    await wait_for_any(
                        page,
                        selectors=[EMAIL_INPUT_SELECTOR],
                        timeout=8,
                    )
                    if await _try_pass_turnstile(rounds=2):
                        _log(f"[CF] 节点 {node} 已通过")
                        passed = True
                        break
                if not passed and await _is_cf_blocked(page):
                    last_error = "cf_blocked"
                    _log("[CF] 仍被拦截，放弃本次")
                    await dump_state(page, "CF拦截", verbose=True)
                    return None

        await dismiss_cookie_banner(page)

        check_stop()
        emit("running", step="填写邮箱")
        _log("[2] 填写邮箱…")
        email_stage_start = time.monotonic()
        if await safe_count(page, EMAIL_INPUT_SELECTOR, timeout=0.8) == 0:
            _log("[2] 邮箱输入框尚未进入 DOM，等待页面 hydration…")
            await wait_for_any(page, selectors=[EMAIL_INPUT_SELECTOR], timeout=6)
        if await safe_count(page, EMAIL_INPUT_SELECTOR, timeout=0.8) == 0:
            cur = await _page_url(page)
            if _is_login_url(cur):
                _log("[2] 登录页仍无邮箱输入框，软刷新后再检测一次…")
                try:
                    await page.reload(timeout=15000, wait_until="commit")
                except Exception:
                    logger.debug("register_one: ignoring exception", exc_info=True)
                await wait_for_any(page, selectors=[EMAIL_INPUT_SELECTOR], timeout=10)
        if await safe_count(page, EMAIL_INPUT_SELECTOR, timeout=1.0) == 0:
            cur = (await _page_url(page)).strip().lower()
            if not cur or cur == "about:blank" or _is_login_url(cur):
                reason = "落入空白页" if not cur or cur == "about:blank" else "登录页 DOM 未就绪"
                _log(f"[2] 软刷新后{reason}，重新打开登录地址恢复当前窗口…")
                await asyncio.sleep(1.0)
                try:
                    await page.goto(SIGNUP_URL, timeout=25000, wait_until="commit")
                except Exception as error:
                    if not _is_navigation_race(error):
                        _log(f"[2] 空白页恢复导航异常: {str(error)[:90]}")
                await wait_for_any(
                    page,
                    selectors=[EMAIL_INPUT_SELECTOR],
                    timeout=15,
                )
        if await safe_count(page, EMAIL_INPUT_SELECTOR, timeout=1.0) == 0:
            # A page caught between Turnstile and SPA hydration can keep the
            # same URL while its main frame is repeatedly replaced.  Reuse the
            # context cookies in one clean tab before declaring the account bad.
            fresh_page = None
            try:
                _log("[2] 当前标签页持续缺少登录表单，创建干净标签页做最后恢复…")
                fresh_page = await ctx.new_page()
                await fresh_page.goto(SIGNUP_URL, timeout=25000, wait_until="commit")
                await wait_for_any(
                    fresh_page,
                    selectors=[EMAIL_INPUT_SELECTOR],
                    timeout=15,
                )
                if await safe_count(fresh_page, EMAIL_INPUT_SELECTOR, timeout=2.0) > 0:
                    old_page = page
                    page = fresh_page
                    fresh_page = None
                    try:
                        await old_page.close()
                    except Exception:
                        logger.debug("register_one: ignoring exception", exc_info=True)
                    _log("[2] 干净标签页已恢复邮箱输入框")
            except Exception as error:
                if not _is_navigation_race(error):
                    _log(f"[2] 干净标签页恢复异常: {str(error)[:90]}")
            finally:
                if fresh_page is not None:
                    try:
                        await fresh_page.close()
                    except Exception:
                        logger.debug("register_one: ignoring exception", exc_info=True)
        if await safe_count(page, EMAIL_INPUT_SELECTOR, timeout=3.0) == 0:
            _log("[2] 快速检测未发现邮箱输入框，读取页面状态后最终复核…")
            await dump_state(page, "无邮箱输入框", verbose=True)
            if await safe_count(page, EMAIL_INPUT_SELECTOR, timeout=3.0) > 0:
                _log("[2] 最终复核已发现邮箱输入框，继续当前会话")
            else:
                last_error = "no_email_input"
                _log("未找到邮箱输入框")
                shot = PROJECT_ROOT / "screenshots" / f"chatgpt_noemail_{index}.png"
                try:
                    await page.screenshot(path=str(shot))
                except Exception:
                    logger.debug("register_one: ignoring exception", exc_info=True)
                return None

        channel = _otp_channel_label(
            str(mailbox.get("provider") if mailbox else prov)
        )
        _log(
            f"[2.5] 验证码将通过邮箱池读取"
            f"（provider={mailbox.get('provider') if mailbox else '?'} · {channel}）"
        )

        _NEXT_SELS = [
            'input[name="code"]',
            'input[inputmode="numeric"]',
            'input[type="password"]',
            'input[name="age"]',
            'input[autocomplete="one-time-code"]',
        ]
        _NEXT_URLS = [
            "email-verification",
            "about-you",
            "password",
            "auth/create-account",
            "auth/username_password",
            "oauth",
            "auth.openai.com",
            "verify",
        ]

        auth_trace: list[str] = []

        def _capture_auth_response(response) -> None:
            try:
                url = str(response.url or "")
                if (
                    "/api/auth/" not in url
                    and "/api/accounts/" not in url
                    and "auth.openai.com/api/" not in url
                ):
                    return
                item = f"HTTP {int(response.status)} {url[:180]}"
                if not auth_trace or auth_trace[-1] != item:
                    auth_trace.append(item)
                    del auth_trace[:-12]
                if any(
                    marker in url
                    for marker in (
                        "/api/accounts/user/register",
                        "/api/accounts/passwordless/send-otp",
                        "/api/accounts/email-otp/send",
                        "/api/accounts/email-otp/resend",
                    )
                ):
                    _log(f"[auth] {item}")
            except Exception:
                logger.debug("_capture_auth_response: ignoring exception", exc_info=True)

        def _capture_auth_request(request) -> None:
            try:
                url = str(request.url or "")
                if (
                    "/api/auth/" not in url
                    and "/api/accounts/" not in url
                    and "auth.openai.com/api/" not in url
                ):
                    return
                item = f"REQUEST {str(request.method or 'GET')} {url[:170]}"
                if not auth_trace or auth_trace[-1] != item:
                    auth_trace.append(item)
                    del auth_trace[:-12]
            except Exception:
                logger.debug("_capture_auth_request: ignoring exception", exc_info=True)

        def _capture_auth_failure(request) -> None:
            try:
                url = str(request.url or "")
                if "/api/auth/" not in url and "auth.openai.com" not in url:
                    return
                failure = str(request.failure or "request failed")
                auth_trace.append(f"FAILED {url[:150]} · {failure[:100]}")
                del auth_trace[:-12]
            except Exception:
                logger.debug("_capture_auth_failure: ignoring exception", exc_info=True)

        try:
            page.on("request", _capture_auth_request)
            page.on("response", _capture_auth_response)
            page.on("requestfailed", _capture_auth_failure)
        except Exception:
            logger.debug("register_one: ignoring exception", exc_info=True)

        async def _left_email_step() -> bool:
            """True when we left the plain email-entry login screen."""
            u = await _page_url(page)
            if _is_auth_error_url(u):
                return False
            if _is_verify_url(u) or _is_about_you_url(u):
                return True
            if any(k in u for k in (
                "password", "create-account", "username_password",
                "oauth", "auth.openai.com", "email-verification",
            )):
                return True
            if await safe_count(page, ", ".join(_NEXT_SELS), timeout=0.4) > 0:
                return True
            if _is_login_url(u):
                return False
            return await is_chatgpt_main(page)

        # Browser mode stays on trusted UI events end to end. Hybrid mode may
        # use the same-session NextAuth fallback below.
        advanced = False
        email_stage_limit = 40 if hybrid_mode else 50
        email_stage_deadline = time.monotonic() + email_stage_limit
        if hybrid_mode:
            emit("running", step="[混合] 协议提交邮箱")
            _log("[2] [混合] 使用浏览器会话协议提交邮箱…")
            await fill_email_verified(page, email, tries=2)
            hybrid_status = await start_nextauth_email_flow(
                page, email, password_first=requires_fresh_registration(mailbox),
            )
            if hybrid_status == "advanced":
                advanced = True
                await wait_for_any(
                    page,
                    selectors=_NEXT_SELS,
                    url_substrings=_NEXT_URLS,
                    timeout=10,
                    poll=0.3,
                )
                _log("[2] [混合] 协议提交完成，浏览器已进入认证流程")
            else:
                _log(f"[2] [混合] 协议提交状态={hybrid_status}，回退浏览器 Continue")
        submit_attempts = 2 if hybrid_mode else 3
        for sub_try in range(submit_attempts):
            if advanced or await _left_email_step():
                advanced = True
                break

            remaining = email_stage_deadline - time.monotonic()
            if remaining <= 0:
                _log(f"[2] 邮箱提交达到 {email_stage_limit}s 上限，停止重复点击")
                break

            try:
                status = await asyncio.wait_for(
                    (
                        submit_email_continue(page, email, retry_click=True)
                        if hybrid_mode
                        else submit_email_browser_ui(page, email)
                    ),
                    timeout=max(
                        1.0,
                        min(22.0 if not hybrid_mode else 14.0, remaining),
                    ),
                )
            except asyncio.TimeoutError:
                status = "timeout"
                _log(
                    f"[2] 邮箱提交调用超时 · "
                    f"try={sub_try + 1}/{submit_attempts}"
                )
            if status == "advanced":
                advanced = True
                _log(
                    f"[2] 已进入下一步 (submit) · "
                    f"try={sub_try + 1}/{submit_attempts}"
                )
                break
            if str(status).startswith("auth_error"):
                _log(f"[2] 邮箱提交进入认证错误页 · {status}")
                break
            if status == "no_el":
                _log("[2] 邮箱输入框已消失，等待页面跳转…")
            elif status in {"fill_fail", "error"}:
                _log(f"[2] 邮箱提交状态={status}，回退 fill…")
                if hybrid_mode:
                    await fill_email_verified(page, email, tries=1)
                    try:
                        await click_exact(page, "Continue", timeout=1500)
                    except Exception:
                        await click_submit_or_enter(
                            page, EMAIL_INPUT_SELECTOR
                        )
            elif status == "timeout":
                _log(
                    f"[2] 邮箱提交动作未完成 (timeout) · "
                    f"try={sub_try + 1}/{submit_attempts}"
                )
            elif status == "submitted":
                _log("[2] [全浏览器] 已捕获 signin POST，等待授权跳转")
                emit("running", step="[全浏览器] signin 已提交")
            elif status == "routed":
                _log("[2] [全浏览器] 邮箱已写入登录路由，继续当前会话")
            elif status == "no_auth_request":
                _log("[2] [全浏览器] 受信任点击未触发 signin POST")
            elif status == "form_unstable":
                _log("[2] [全浏览器] 登录表单仍在重建，未进入稳定状态")
            elif status == "email_state_lost":
                _log("[2] [全浏览器] 页面重渲染导致邮箱状态丢失")
            else:
                _log(
                    f"[2] 已提交邮箱 ({status}) · "
                    f"try={sub_try + 1}/{submit_attempts}"
                )

            auth_observed = any(
                "authorize" in item or "/api/auth/signin/" in item
                for item in auth_trace
            )
            if not hybrid_mode and (status == "submitted" or auth_observed):
                _log("[2] [全浏览器] 认证请求已发出，仅等待授权页面推进…")
                advanced = await wait_for_any(
                    page,
                    selectors=_NEXT_SELS,
                    url_substrings=_NEXT_URLS,
                    timeout=max(
                        1.0,
                        min(15.0, email_stage_deadline - time.monotonic()),
                    ),
                    poll=0.25,
                )
                if advanced or await _left_email_step():
                    advanced = True
                break

            if (
                not hybrid_mode
                and status in {"no_auth_request", "routed"}
                and sub_try + 1 < submit_attempts
            ):
                _log("[2] [全浏览器] 当前表单继续下一次受信任提交…")
                await asyncio.sleep(0.6)
                continue

            current_form_recoverable = {"no_el", "fill_fail", "form_unstable"}
            if (
                not hybrid_mode
                and status in current_form_recoverable
                and sub_try + 1 < submit_attempts
            ):
                _log("[2] [全浏览器] 等待当前表单稳定后继续，不刷新页面…")
                await asyncio.sleep(0.8)
                continue

            browser_recoverable = {"email_state_lost"}
            if (
                not hybrid_mode
                and status in browser_recoverable
                and sub_try + 1 < submit_attempts
            ):
                _log("[2] [全浏览器] 刷新登录表单后重新提交一次…")
                try:
                    await page.goto(SIGNUP_URL, timeout=15000, wait_until="commit")
                except Exception as error:
                    if not _is_navigation_race(error):
                        _log(f"[2] 登录表单刷新较慢: {str(error)[:100]}")
                await wait_for_any(
                    page,
                    selectors=[EMAIL_INPUT_SELECTOR],
                    timeout=8,
                    poll=0.25,
                )
                continue

            # Give the SPA a chance to consume the trusted browser click.
            remaining = email_stage_deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_s = min(4, remaining)
            advanced = await wait_for_any(
                page,
                selectors=_NEXT_SELS,
                url_substrings=_NEXT_URLS,
                timeout=wait_s,
                poll=0.35,
            )
            if advanced or await _left_email_step():
                advanced = True
                break

            if not hybrid_mode and any(
                "authorize" in item or "/api/auth/signin/" in item
                for item in auth_trace
            ):
                _log("[2] [全浏览器] 已观察到授权请求，等待当前请求完成…")
                emit("running", step="[全浏览器] 授权请求处理中")
                advanced = await wait_for_any(
                    page,
                    selectors=_NEXT_SELS,
                    url_substrings=_NEXT_URLS,
                    timeout=max(
                        1.0,
                        min(12.0, email_stage_deadline - time.monotonic()),
                    ),
                    poll=0.25,
                )
                if advanced or await _left_email_step():
                    advanced = True
                    break

            if hybrid_mode and status == "timeout" and sub_try == 0:
                check_stop()
                _log("[2] 首次 Continue 超时，立即使用当前浏览器会话请求…")
                emit("running", step="Continue 超时，浏览器会话请求接管")
                early_fallback = await start_nextauth_email_flow(
                    page, email, password_first=requires_fresh_registration(mailbox),
                )
                if early_fallback == "advanced" or await _left_email_step():
                    advanced = True
                    _log("[2] 浏览器会话请求已接管并进入认证流程")
                    await wait_for_any(
                        page,
                        selectors=_NEXT_SELS,
                        url_substrings=_NEXT_URLS,
                        timeout=10,
                        poll=0.3,
                    )
                    break
                _log(f"[2] 浏览器会话请求未推进 · 状态={early_fallback}")
                if str(early_fallback).startswith("auth_error"):
                    break

            cur_u = await _page_url(page)
            if _is_login_url(cur_u):
                if hybrid_mode:
                    _log(f"[2] 仍在登录页，准备最后一次提交… · {cur_u[:80]}")
                else:
                    _log(f"[2] [全浏览器] UI 提交结束但仍在登录页 · {cur_u[:80]}")
                if auth_trace:
                    _log(f"[2] auth trace: {' | '.join(auth_trace[-4:])}")
                continue

            _log(
                f"[2] 仍在登录页，重试提交 "
                f"({sub_try + 1}/{submit_attempts})… · url={cur_u[:90]}"
            )
            await asyncio.sleep(0.15)

        email_url_before_fallback = await _page_url(page)
        if (
            not advanced
            and not _is_auth_error_url(email_url_before_fallback)
            and not await _left_email_step()
        ):
            check_stop()
            if hybrid_mode:
                _log("[2] UI Continue 未推进，使用 HAR NextAuth 请求链兜底…")
                emit("running", step="NextAuth 请求链兜底")
                fallback_status = await start_nextauth_email_flow(
                    page, email, password_first=requires_fresh_registration(mailbox),
                )
                if fallback_status == "advanced":
                    advanced = True
                    _log("[2] NextAuth 兜底已进入 authorize")
                    await wait_for_any(
                        page,
                        selectors=_NEXT_SELS,
                        url_substrings=_NEXT_URLS,
                        timeout=10,
                        poll=0.3,
                    )
                elif await _left_email_step():
                    advanced = True
                    _log("[2] NextAuth 兜底期间页面已自行进入下一步")
                else:
                    _log(f"[2] NextAuth 兜底状态={fallback_status}")
            else:
                _log("[2] [全浏览器] Continue 已提交，等待页面完成授权跳转…")
                emit("running", step="[全浏览器] 等待授权跳转")
                advanced = await wait_for_any(
                    page,
                    selectors=_NEXT_SELS,
                    url_substrings=_NEXT_URLS,
                    timeout=max(1.0, min(4.0, email_stage_deadline - time.monotonic())),
                    poll=0.25,
                )
                if not advanced:
                    advanced = await _left_email_step()
                if not advanced:
                    _log("[2] [全浏览器] UI 跳转未落地，由当前浏览器会话接管认证请求…")
                    emit("running", step="[全浏览器] 当前会话接管认证请求")
                    fallback_status = await start_nextauth_email_flow(
                        page, email, password_first=requires_fresh_registration(mailbox),
                    )
                    if fallback_status == "advanced" or await _left_email_step():
                        advanced = True
                        _log("[2] [全浏览器] 当前会话接管成功，已进入认证流程")
                    else:
                        _log(f"[2] [全浏览器] 当前会话接管状态={fallback_status}")

        check_timeout()
        stage_cost = time.monotonic() - email_stage_start
        if advanced:
            _log(f"[2] 邮箱阶段完成 · {stage_cost:.1f}s")
        else:
            _log(f"[2] 邮箱阶段未推进 · {stage_cost:.1f}s")
        await dump_state(page, "填邮箱后")

        cur_after_email = await _page_url(page)
        auth_error_code = auth_error_code_from_url(cur_after_email)
        if _is_auth_error_url(cur_after_email):
            auth_error_code = auth_error_code or "unknown"
            last_error = f"auth_{auth_error_code}"
            _log(f"[2] 认证服务拒绝当前会话 · {auth_error_code}")
            emit(
                "failed",
                step=f"认证服务错误: {auth_error_code}",
                error=last_error,
            )
            return None
        if not advanced:
            advanced = await _left_email_step()
        still_on_login = (
            not advanced
            and _is_login_url(cur_after_email)
            and await safe_count(
                page,
                'input[name="code"], input[inputmode="numeric"], '
                'input[type="password"], input[name="age"]',
                timeout=0.8,
            ) == 0
        )
        if still_on_login and login_email_query(cur_after_email) == email:
            _log("[2] 最后等待 SPA 跳转…")
            advanced = await wait_for_any(
                page,
                selectors=_NEXT_SELS,
                url_substrings=_NEXT_URLS,
                timeout=8,
                poll=0.4,
            )
            if advanced or await _left_email_step():
                still_on_login = False
        if still_on_login and not advanced:
            # The authorize request can already have returned 302 while the
            # navigation event is still queued. A final short DOM/URL check
            # prevents closing a window that has just reached the OTP page.
            _log("[2] 认证请求已发出，最终复核页面跳转…")
            moved = await wait_for_any(
                page,
                selectors=_NEXT_SELS,
                url_substrings=_NEXT_URLS,
                timeout=5,
                poll=0.2,
            )
            cur_after_email = await _page_url(page)
            if moved or await _left_email_step():
                advanced = True
                still_on_login = False
                _log(f"[2] 最终复核已进入下一步 · {cur_after_email[:90]}")
        if still_on_login and not advanced:
            last_error = "email_submit_stuck"
            _log("[2] 多次 Continue 后仍在登录页，放弃（不会进入验证码/资料）")
            if auth_trace:
                _log(f"[2] auth trace: {' | '.join(auth_trace[-8:])}")
            else:
                _log("[2] auth trace: 未观察到认证 API 请求")
            emit("failed", step="邮箱提交失败，仍在登录页", error=last_error)
            await dump_state(page, "邮箱提交卡住", verbose=True)
            return None

        body_l = (await safe_body_text(page, 500)).lower()
        cur_for_req = await _page_url(page)
        still_login_with_email = (
            _is_login_url(cur_for_req)
            and await safe_count(
                page, EMAIL_INPUT_SELECTOR, timeout=0.8
            ) > 0
            and await safe_count(
                page,
                'input[name="code"], input[inputmode="numeric"]',
                timeout=0.8,
            ) == 0
        )
        if still_login_with_email and any(
            k in body_l
            for k in ["必須", "必填", "is required", "email is required", "enter a valid"]
        ):
            _log("[2] 仍在登录页（邮箱必填），再填一次…")
            await dismiss_cookie_banner(page)
            await fill_email_verified(page, email, tries=1)
            if not await click_any_exact(
                page, ["Continue", "続行", "继续", "繼續", "Teruskan"]
            ):
                await click_submit_or_enter(page, EMAIL_INPUT_SELECTOR)
            await wait_for_any(
                page,
                selectors=['input[name="code"]', 'input[type="password"]'],
                url_substrings=["email-verification", "about-you", "password"],
                timeout=8,
            )
            await dump_state(page, "重填邮箱后")

        if await detect_challenge(page):
            _log("[!] authorize 出现 Cloudflare 验证，尝试点击并等待…")
            if await _try_pass_turnstile(rounds=4, wait=1.5):
                _log("[CF] authorize Turnstile 已通过")
            else:
                _log("[CF] authorize Turnstile 暂未通过，继续等待页面自动处理")
            await wait_for_any(
                page,
                selectors=_NEXT_SELS,
                url_substrings=["email-verification", "about-you"],
                timeout=8,
            )

        password_input_count = await safe_count(
            page,
            'input[type="password"], input[name="password"], input[name="new-password"]',
        )
        await check_fresh_registration_page(page, mailbox, require_signup=password_input_count > 0)
        if password_input_count > 0:
            password_url = (await _page_url(page)).lower()
            existing_account_login = _is_existing_account_password_url(password_url)
            if existing_account_login:
                _log("[3] 服务端识别为已有账号，切换邮箱验证码登录…")
                emit("running", step="已有账号，切换邮箱验证码登录")
                if mailbox is not None:
                    # Exclude messages generated by an earlier login attempt.
                    # The passwordless action below starts a new OTP transaction.
                    mark_mailbox_code_boundary(mailbox, grace_seconds=0)
                switched = await switch_password_to_email_code(page)
                if switched:
                    await wait_for_any(
                        page,
                        selectors=['input[name="code"]', 'input[inputmode="numeric"]'],
                        url_substrings=["email-verification", "verify"],
                        timeout=15,
                    )
                cur_after_password = await _page_url(page)
                if "password" in cur_after_password:
                    last_error = "passwordless_switch_failed"
                    _log("[3] 已有账号仍停留密码页，邮箱验证码入口未推进")
                    emit(
                        "failed",
                        step="已有账号未进入邮箱验证码登录",
                        error=last_error,
                    )
                    await dump_state(page, "密码页切换失败", verbose=True)
                    return None
                _log("[3] 已进入邮箱验证码登录")
            else:
                _log("[3] 新账号填写注册密码…")
                await react_fill(
                    page,
                    'input[type="password"]',
                    password,
                    tries=2,
                    delay=8,
                    settle=0.1,
                    verbose=False,
                )
                await asyncio.sleep(0.2)
                if not await click_exact(page, "Continue", timeout=3000):
                    await click_submit_or_enter(page, 'input[type="password"]')
                await wait_for_any(
                    page,
                    selectors=['input[name="code"]', 'input[name="age"]'],
                    url_substrings=["email-verification", "about-you"],
                    timeout=20,
                )
                cur_after_password = await _page_url(page)
                await check_fresh_registration_page(page, mailbox)
                if "password" in cur_after_password:
                    _log("[3] 注册密码页仍未推进，再提交一次…")
                    await click_submit_or_enter(page, 'input[type="password"]')
                    await wait_for_any(
                        page,
                        selectors=['input[name="code"]', 'input[name="age"]'],
                        url_substrings=["email-verification", "about-you"],
                        timeout=15,
                    )
                    cur_after_password = await _page_url(page)
                    await check_fresh_registration_page(page, mailbox)
                # 以“密码输入框已消失”判定提交成功：表单被服务端接受后跳转，
                # 密码框不再渲染；不能看 URL 子串（query 参数带 password 字样会误判，
                # 导致落库空密码、服务端真实密码永久丢失）
                password_submitted = (
                    await safe_count(
                        page,
                        'input[type="password"], input[name="password"], '
                        'input[name="new-password"]',
                        timeout=0.8,
                    )
                    == 0
                )
                if password_submitted:
                    _log("[3] 注册密码已由服务端页面确认")
            await dump_state(page, "填密码后")
        await check_fresh_registration_page(page, mailbox)
        if requires_fresh_registration(mailbox) and not mailbox.get("_registration_identity_created"):
            raise RuntimeError("registration_identity_not_confirmed")
        check_timeout()

        body_text = (await safe_body_text(page)).lower()
        cur_url = await _page_url(page)
        need_code = (
            await safe_count(
                page,
                'input[inputmode="numeric"], input[name="code"], '
                'input[autocomplete="one-time-code"]',
            ) > 0
            or "verify" in cur_url
            or "email-verification" in cur_url
            or "check your" in body_text
            or ("enter" in body_text and "code" in body_text)
        )
        if await is_chatgpt_main(page) or "about-you" in cur_url:
            need_code = False

        if not need_code and _is_login_url(cur_url) and not await is_chatgpt_main(page):
            # login?email= may still be mid-redirect — give SPA a short window
            if login_email_query(cur_url) == email:
                _log("[2] login?email= 尚未到验证码页，再等 8s…")
                moved = await wait_for_any(
                    page,
                    selectors=[
                        'input[name="code"]',
                        'input[inputmode="numeric"]',
                        'input[type="password"]',
                        'input[name="age"]',
                    ],
                    url_substrings=[
                        "email-verification", "about-you", "password",
                        "auth.openai.com", "create-account",
                    ],
                    timeout=8,
                    poll=0.25,
                )
                cur_url = await _page_url(page)
                body_text = (await safe_body_text(page)).lower()
                need_code = (
                    moved
                    or await safe_count(
                        page,
                        'input[inputmode="numeric"], input[name="code"], '
                        'input[autocomplete="one-time-code"]',
                    ) > 0
                    or "verify" in cur_url
                    or "email-verification" in cur_url
                    or "check your" in body_text
                    or ("enter" in body_text and "code" in body_text)
                )
            if (
                not need_code
                and _is_login_url(cur_url)
                and await safe_count(page, 'input[name="age"]') == 0
                and await safe_count(page, 'input[type="password"]') == 0
            ):
                last_error = "stuck_on_login"
                _log(f"[2] 未进入验证码页且仍在登录 · url={cur_url[:80]}")
                emit("failed", step="未进入验证码页", error=last_error)
                return None

        if need_code:
            # Entering the verification route normally sends an OTP already.
            # Poll that first message briefly before creating a duplicate.
            _log(f"[4] 先通过 {channel} 检查自动发送的验证码…")
            emit("running", step=f"检查首封验证码 ({channel})")
            code, send_status = await _poll_auto_sent_code_before_resend(
                page,
                mail_config,
                mailbox,
                _stop,
                allow_http_fallback=hybrid_mode,
            )
            otp_send_confirmed = bool(code) or 200 <= send_status < 300
            if code:
                _log("[4] 已收到自动发送的验证码，跳过补发")
                emit("running", step="已收到首封验证码")
            elif 200 <= send_status < 300:
                _log(f"[4] 首次验证码发送已由服务端确认 · HTTP {send_status}")
                emit("running", step="验证码发送已确认")
            elif send_status == 429:
                _log("[4] 首次补发处于冷却，继续等待当前会话邮件")
                emit("running", step="验证码发送冷却中")
            else:
                _log("[4] 未确认首次发送请求，继续轮询并在下一轮重发")
                emit("running", step="验证码发送未确认")
            for code_try in range(3) if not code else ():
                # OTP 等待链路最长可达数分钟，必须受注册总超时约束
                check_timeout()
                try:
                    if page.is_closed():
                        _log("[4] 页面已关闭，停止等待验证码")
                        break
                except Exception:
                    logger.debug("register_one: ignoring exception", exc_info=True)
                if code_try == 0:
                    _log(f"[4] 正在通过 {channel} 等待邮箱验证码…")
                    emit("running", step=f"等待邮箱验证码 ({channel})")
                else:
                    _log(f"[4] 验证码重试 {code_try}/2：重发并继续等待…")
                    emit("running", step=f"验证码重试 {code_try}/2")
                    if mailbox is not None:
                        mark_mailbox_code_boundary(mailbox, grace_seconds=0)
                    resend_status = await _click_resend_code(
                        page, allow_http_fallback=hybrid_mode
                    )
                    if 200 <= resend_status < 300:
                        otp_send_confirmed = True
                    if not resend_status:
                        try:
                            await page.goto(
                                SIGNUP_URL,
                                timeout=25000,
                                wait_until="commit",
                            )
                            await wait_for_any(
                                page, selectors=[EMAIL_INPUT_SELECTOR], timeout=8
                            )
                            await dismiss_cookie_banner(page)
                            await fill_email_verified(page, email)
                            if not await click_any_exact(
                                page,
                                [
                                    "Continue", "続行", "继续", "繼續",
                                    "Next", "下一步", "Teruskan",
                                ],
                            ):
                                await click_submit_or_enter(
                                    page, EMAIL_INPUT_SELECTOR
                                )
                            await wait_for_any(
                                page,
                                selectors=[
                                    'input[name="code"]',
                                    'input[type="password"]',
                                ],
                                url_substrings=["email-verification"],
                                timeout=12,
                            )
                            pw = page.locator('input[type="password"]')
                            recovery_url = await _page_url(page)
                            if await pw.count() > 0 and _is_existing_account_password_url(
                                (recovery_url or "").lower()
                            ):
                                # 落到已有账号的登录密码页：不能填本轮注册密码
                                # （错误密码提交会触发账号锁定/风控），直接放弃恢复
                                _log(
                                    "[4] 恢复流程落到已有账号密码页，跳过填码避免误提交"
                                )
                            elif await pw.count() > 0:
                                await react_fill(
                                    page,
                                    'input[type="password"]',
                                    password,
                                    tries=2,
                                    delay=8,
                                    settle=0.1,
                                    verbose=False,
                                )
                                await asyncio.sleep(0.3)
                                if not await click_exact(
                                    page, "Continue", timeout=3000
                                ):
                                    await click_submit_or_enter(
                                        page, 'input[type="password"]'
                                    )
                                await wait_for_any(
                                    page,
                                    selectors=['input[name="code"]'],
                                    url_substrings=["email-verification"],
                                    timeout=10,
                                )
                                recovery_password_url = await _page_url(page)
                                # 与主流程一致：仅以密码框消失判定提交成功
                                password_submitted = (
                                    await safe_count(
                                        page,
                                        'input[type="password"], input[name="password"], '
                                        'input[name="new-password"]',
                                        timeout=0.8,
                                    )
                                    == 0
                                )
                        except Exception as e:
                            _log(f"[4] 重新进入页面重发失败: {str(e)[:80]}")
                    await asyncio.sleep(1)

                try:
                    check_stop()
                    normal_wait = float(mail_config.get("wait_timeout") or 90)
                    attempt_wait = max(30.0, min(normal_wait, 60.0))
                    wait_config = dict(
                        mail_config,
                        wait_timeout=attempt_wait,
                        wait_chunk=min(
                            60.0,
                            max(30.0, float(mail_config.get("wait_chunk") or 60)),
                        ),
                        request_timeout=max(
                            15.0,
                            float(mail_config.get("request_timeout") or 30),
                        ),
                    )
                    code = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: wait_for_code(
                            wait_config, mailbox, stop_check=_stop
                        ),
                    )
                except JobCancelled:
                    raise
                except Exception as e:
                    _log(f"[4] 取验证码出错: {str(e)[:100]}")
                    code = None
                check_stop()
                if code:
                    break

            if code:
                # 验证码脱敏：聚合日志可能被用于撞库窗口内重放，只显示前 2 位
                _log(f"已拿到验证码: {str(code)[:2]}****")
                emit("running", step="已拿到验证码")
                if hybrid_mode:
                    # Keep the SPA in control of the OTP response. The same
                    # endpoint serves both sign-up and existing-account login;
                    # forcing /about-you after a protocol fetch can turn a
                    # valid login into create_account invalid_state.
                    emit("running", step="[混合] 页面校验验证码")
                    _log("[4] [混合] 由当前页面提交 OTP，保留登录/注册分支状态")
                    otp_result = await _submit_verification_code(
                        page, code, timeout=15, mailbox=mailbox,
                    )
                else:
                    otp_result = await _submit_verification_code(
                        page, code, timeout=12, mailbox=mailbox,
                    )
                advanced = otp_result.advanced
                await dump_state(page, "填验证码后", verbose=not advanced)
                if not advanced:
                    check_stop()
                    verification_body = (
                        await safe_body_text(page, limit=1200, timeout=3)
                    ).lower()
                    if (
                        "account_deactivated" in verification_body
                        or "account has been deleted or deactivated" in verification_body
                    ):
                        last_error = "account_deactivated"
                        _log("[4] 该邮箱关联账号已删除或停用，标记为永久不可用")
                        emit(
                            "failed",
                            step="账号已删除或停用",
                            error=last_error,
                        )
                        return None
                    if not otp_result.should_resend:
                        last_error = "otp_accepted_navigation_stuck"
                        _log("[4] 验证码已被接受，但后续资料页恢复失败；停止补发")
                        emit(
                            "failed",
                            step="验证码已通过，后续导航未恢复",
                            error=last_error,
                        )
                        return None
                    _log("[4] 验证码未被页面接受，重新发送新验证码…")
                    emit("running", step="验证码未通过，重新发送")
                    if mailbox is not None:
                        mark_mailbox_code_boundary(mailbox, grace_seconds=0)
                    try:
                        await page.reload(timeout=15000, wait_until="commit")
                        await wait_for_any(
                            page,
                            selectors=[OTP_INPUT_SELECTOR],
                            url_substrings=["email-verification"],
                            timeout=6,
                        )
                        _log("[4] 已刷新验证码页，等待重发入口恢复")
                    except Exception as e:
                        _log(f"[4] 刷新验证码页失败: {str(e)[:80]}")
                    resent = False
                    resend_deadline = time.monotonic() + 40
                    waiting_logged = False
                    while not resent and time.monotonic() < resend_deadline:
                        check_stop()
                        recovery_send_status = await _click_resend_code(
                            page, allow_http_fallback=hybrid_mode
                        )
                        resent = _otp_resend_accepted(recovery_send_status)
                        if resent:
                            otp_send_confirmed = True
                        if resent:
                            break
                        if not waiting_logged:
                            if recovery_send_status in {409, 425, 429}:
                                _log(
                                    f"[4] 补发接口处于冷却状态 · HTTP {recovery_send_status}，稍后重试…"
                                )
                            else:
                                _log("[4] 重发按钮尚未可用，等待冷却…")
                            emit("running", step="等待重发按钮冷却")
                            waiting_logged = True
                        await asyncio.sleep(
                            5 if recovery_send_status in {409, 425, 429} else 2
                        )
                    if resent:
                        normal_wait = float(mail_config.get("wait_timeout") or 90)
                        recovery_config = dict(
                            mail_config,
                            wait_timeout=max(20.0, min(60.0, normal_wait)),
                        )
                        try:
                            fresh_code = await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: wait_for_code(
                                    recovery_config, mailbox, stop_check=_stop
                                ),
                            )
                        except JobCancelled:
                            raise
                        except Exception as e:
                            _log(f"[4] 重发后取验证码出错: {str(e)[:100]}")
                            fresh_code = None
                        check_stop()
                        if fresh_code and fresh_code != code:
                            _log(f"[4] 已收到重发验证码: {str(fresh_code)[:2]}****")
                            emit("running", step="已收到重发验证码")
                            await _submit_verification_code(
                                page,
                                fresh_code,
                                timeout=15 if hybrid_mode else 12,
                                mailbox=mailbox,
                            )
                            await dump_state(page, "重发验证码后")
                        elif fresh_code:
                            _log("[4] 重发后仍返回旧验证码，停止重复提交")
                        else:
                            _log("[4] 重发后未收到新验证码")
                    else:
                        _log("[4] 未找到重发验证码按钮")
            else:
                if otp_send_confirmed:
                    last_error = "otp_not_delivered"
                    _log(
                        "[4] 多次发码接口已确认但邮件未投递，隔离当前邮箱"
                    )
                    emit(
                        "failed",
                        step="验证码未投递，邮箱已隔离",
                        error=last_error,
                    )
                else:
                    last_error = "no_code"
                    _log("未收到验证码")
                    emit("failed", step="未收到验证码", error=last_error)
                return None
        check_timeout()

        cur_pre_ob = await _page_url(page)
        if await is_chatgpt_main(page):
            _log("[5] 已在主界面，跳过资料页")
        elif _is_about_you_url(cur_pre_ob) or await safe_count(
            page, 'input[name="age"]'
        ) > 0:
            _log("[5] 完善资料 / 引导页…")
            emit("running", step="完善资料 / onboarding")
            ob_ok = await handle_onboarding(page, index, stop_check=_stop)
            if not ob_ok and not await is_chatgpt_main(page):
                identity_gate = await is_identity_verification_page(page)
                last_error = (
                    "identity_verification_required"
                    if identity_gate
                    else "onboarding_failed"
                )
                step = (
                    "需要人工完成 Persona 身份核验"
                    if identity_gate
                    else "资料页未完成"
                )
                _log(f"[5] {step}")
                emit("failed", step=step, error=last_error)
                return None
        elif _is_verify_url(cur_pre_ob):
            last_error = "stuck_on_verification"
            _log(f"[5] 仍在验证码页，无法进资料 · url={cur_pre_ob[:80]}")
            emit("failed", step="仍在验证码页", error=last_error)
            return None
        elif _is_login_url(cur_pre_ob):
            last_error = "stuck_on_login"
            _log(f"[5] 仍在登录页，拒绝进入资料流程 · url={cur_pre_ob[:80]}")
            emit("failed", step="仍在登录页，无验证码", error=last_error)
            return None
        elif "password" in cur_pre_ob:
            last_error = "stuck_on_password"
            _log(f"[5] 仍在密码页，拒绝进入资料流程 · url={cur_pre_ob[:80]}")
            emit("failed", step="密码提交失败", error=last_error)
            return None
        else:
            _log(f"[5] 尝试引导 · url={cur_pre_ob[:80]}")
            emit("running", step="完善资料 / onboarding")
            ob_ok = await handle_onboarding(page, index, stop_check=_stop)
            if not ob_ok and not await is_chatgpt_main(page):
                identity_gate = await is_identity_verification_page(page)
                last_error = (
                    "identity_verification_required"
                    if identity_gate
                    else "onboarding_failed"
                )
                _log(
                    "[5] 需要人工完成 Persona 身份核验"
                    if identity_gate
                    else "[5] 引导未成功"
                )
                return None
        check_timeout()

        if not await is_chatgpt_main(page):
            try:
                await page.goto(
                    "https://chatgpt.com/",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                await wait_for_any(
                    page,
                    selectors=['#prompt-textarea', 'textarea'],
                    url_substrings=["chatgpt.com/"],
                    timeout=10,
                )
            except Exception:
                logger.debug("register_one: ignoring exception", exc_info=True)
        else:
            await asyncio.sleep(0.8)
        await dump_state(page, "最终")

        key_val, cookie_file = await save_platform_cookies(
            ctx,
            PLATFORM,
            pid,
            email=email,
            password=password if password_submitted else "",
            key_cookie_names=KEY_COOKIES,
        )

        browser_identity: dict[str, Any] = {}
        try:
            context_cookies = await ctx.cookies(
                ["https://chatgpt.com", "https://auth.openai.com"]
            )
            browser_identity["device_id"] = next(
                (
                    str(item.get("value") or "")
                    for item in context_cookies
                    if str(item.get("name") or "") == "oai-did"
                ),
                "",
            )
        except Exception:
            logger.debug("register_one: ignoring exception", exc_info=True)
        try:
            identity = await asyncio.wait_for(
                page.evaluate(
                    """() => ({
                        locale: navigator.language || '',
                        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                        timezone_offset_min: new Date().getTimezoneOffset(),
                    })"""
                ),
                timeout=2.0,
            )
            if isinstance(identity, dict):
                browser_identity.update(identity)
        except Exception:
            logger.debug("register_one: ignoring exception", exc_info=True)

        sess = None
        try:
            token_deadline = time.monotonic() + 20
            token_wait_logged = False
            while time.monotonic() < token_deadline:
                check_stop()
                sess = await fetch_chatgpt_session(
                    page,
                    timeout=max(
                        0.5,
                        min(7.0, token_deadline - time.monotonic()),
                    ),
                )
                if sess and sess.get("accessToken"):
                    break
                if not token_wait_logged:
                    _log("[chatgpt] session cookie 已建立，等待 accessToken 就绪…")
                    emit("running", step="等待 accessToken")
                    token_wait_logged = True
                await asyncio.sleep(1.5)
            if not (sess and sess.get("accessToken")):
                _log(f"[警告] session cookie 已建立但未拿到 accessToken · {email}")
        except Exception as e:
            _log(f"[警告] 保存 token 失败 · {email}: {e}")

        if sess and sess.get("accessToken"):
            renewable_tokens = None
            try:
                emit("running", step="获取长期续期凭证…")
                renewable_tokens = await fetch_platform_refresh_tokens(
                    ctx,
                    email=email,
                    device_id=str(browser_identity.get("device_id") or ""),
                )
                if renewable_tokens and renewable_tokens.get("refresh_token"):
                    _log(f"[chatgpt] refresh token 已保存 · {email}")
                else:
                    _log(f"[chatgpt] 本次未取得 refresh token · {email}")
            except Exception as e:
                _log(f"[chatgpt] 获取 refresh token 异常 · {email}: {str(e)[:160]}")
            export_session = dict(sess)
            if renewable_tokens:
                export_session.update(
                    {
                        "accessToken": renewable_tokens.get("access_token")
                        or export_session.get("accessToken"),
                        "refreshToken": renewable_tokens.get("refresh_token") or "",
                        "idToken": renewable_tokens.get("id_token") or "",
                    }
                )
            if save_chatgpt_tokens(export_session, email):
                _log(f"[成功] ChatGPT token 已保存 · {email}")
            record = build_account_record(
                export_session,
                email=email,
                password=password if password_submitted else "",
                proxy=proxy,
                session_cookie=key_val or "",
                browser_type=getattr(bb, "provider_name", "local"),
            )
            record["password_set"] = bool(password_submitted)
            record["totp_secret"] = ""
            record["mfa_enabled"] = False
            record["registration_state"] = "security_pending"
            record["group_id"] = str(group_id or "").strip()
            record["cookie_file"] = str(cookie_file or "")
            record["provider_type"] = str(
                (mailbox or {}).get("provider") or provider_type or ""
            ).strip()
            record["mailbox_provider_ref"] = str(
                (mailbox or {}).get("provider_ref") or ""
            ).strip()
            if renewable_tokens:
                record.update(
                    {
                        key: value
                        for key, value in renewable_tokens.items()
                        if value
                    }
                )
            record.update(
                {
                    key: value
                    for key, value in browser_identity.items()
                    if value not in {None, ""}
                }
            )
            if hybrid_mode:
                record["registration_method"] = "hybrid"
            if persist_result:
                _append_account(record)
            success = True
            try:
                c2a = c2a_config if isinstance(c2a_config, dict) else {}
                if (
                    c2a.get("enabled")
                    and c2a.get("auto_upload", True)
                    and sess
                    and sess.get("accessToken")
                ):
                    host = str(c2a.get("url") or "").strip()
                    key = str(c2a.get("key") or "").strip()
                    if host and key:
                        emit("running", step="上传 chatgpt2api…")
                        ok_up, msg_up = upload_session(
                            sess, email=email, host=host, key=key
                        )
                        if ok_up:
                            _log(f"[c2a] 已上传 · {email} · {msg_up}")
                            if not defer_success_event:
                                emit(
                                    "success",
                                    email=email,
                                    step=f"注册完成并已上传 · {email}",
                                )
                        else:
                            _log(f"[c2a] 上传失败 · {email}: {msg_up}")
                            if not defer_success_event:
                                emit(
                                    "success",
                                    email=email,
                                    step=f"注册完成（上传失败）· {email}",
                                )
                    else:
                        _log("[c2a] 已启用但未配置 URL/KEY，跳过上传")
                        if not defer_success_event:
                            emit("success", email=email, step=f"注册完成 · {email}")
                else:
                    if not defer_success_event:
                        emit("success", email=email, step=f"注册完成 · {email}")
            except Exception as e:
                _log(f"[c2a] 上传异常 · {email}: {str(e)[:120]}")
                if not defer_success_event:
                    emit("success", email=email, step=f"注册完成 · {email}")
            total_cost = time.time() - overall_start
            flow_cost = time.time() - start
            _log(
                f"[成功] 注册完成 · 账号={email} · "
                f"总耗时={total_cost:.1f}s 浏览器流程={flow_cost:.1f}s"
            )
            if defer_success_event:
                emit(
                    "running",
                    email=email,
                    step="浏览器注册成功，等待协议补全密码和 2FA",
                )
            if persist_result:
                try:
                    from common.account_operation_log import record_operation_event

                    record_operation_event(
                        record,
                        "registration",
                        "success",
                        message="注册成功",
                        metadata={
                            "engine": "browser",
                            "provider": str(record.get("provider_type") or ""),
                        },
                        secrets=(password, record.get("access_token"), record.get("session_token")),
                    )
                except Exception:
                    logger.debug("register_one: ignoring exception", exc_info=True)
            return record

        last_error = "no_access_token"
        if key_val:
            emit(
                "failed",
                step="已有 session cookie，但 accessToken 尚未就绪",
                error=last_error,
            )
            _log("[失败] 已有 session cookie，但 accessToken 尚未就绪；本次不计成功")
        else:
            emit("failed", step="没有 session cookie / accessToken", error=last_error)
            _log("[失败] 没有 session cookie / accessToken")
        return None

    except RegisteredEmailDetected:
        last_error = RegisteredEmailDetected.code
        emit("skipped", step="邮箱已被注册，已跳过并取消本次注册", skip_reason=last_error)
        _log("[跳过] 邮箱已被注册，未登录、未重置密码、未读取验证码")
        return None
    except JobCancelled:
        last_error = "cancelled"
        emit("cancelled", step="用户取消", error=last_error)
        _log("[停止] 用户取消")
        return None
    except asyncio.CancelledError:
        last_error = "cancelled"
        emit("cancelled", step="任务已取消", error=last_error)
        _log("[停止] 任务已取消")
        return None
    except Exception as e:
        last_error = str(e)[:200]
        if is_proxy_transport_error(e):
            emit(
                "running",
                step="当前代理连接失败，等待批调度器切换代理",
                error=last_error,
            )
            _log("当前代理传输失败，等待批调度器切换代理重试")
        else:
            emit("failed", step=f"错误: {last_error}", error=last_error)
            _log(f"错误: {e}")
        try:
            tb = _tb.format_exc()
            tail = "\n".join(tb.strip().splitlines()[-10:])
            _log(f"堆栈:\n{tail}")
        except Exception:
            logger.debug("register_one: ignoring exception", exc_info=True)
        return None
    finally:
        if (
            not success
            and last_error
            and last_error not in {"cancelled", "stop requested", RegisteredEmailDetected.code}
        ):
            try:
                await _capture_registration_diagnostic(last_error)
            except Exception:
                logger.debug("register_one: ignoring exception", exc_info=True)
        if persist_result and not success and last_error:
            try:
                from common.account_operation_log import record_operation_event

                record_operation_event(
                    {"email": email},
                    "registration",
                    "skipped" if last_error == RegisteredEmailDetected.code else (
                        "cancelled" if last_error in {"cancelled", "stop requested"} else "failed"
                    ),
                    message=(
                        "邮箱已被注册，已跳过" if last_error == RegisteredEmailDetected.code else (
                            "注册已停止" if last_error in {"cancelled", "stop requested"} else "注册失败"
                        )
                    ),
                    error=last_error,
                    metadata={"engine": "browser"},
                    secrets=(password,),
                )
            except Exception:
                logger.debug("register_one: ignoring exception", exc_info=True)
        if mailbox is not None:
            try:
                if success:
                    mark_mailbox_result(mailbox, success=True)
                elif mailbox.get("_otp_accepted_by_server"):
                    # OTP 已被服务端接受（账号已创建）但流程未走完：
                    # 标记 used 防止邮箱回池后被当作新邮箱重复注册
                    if requires_fresh_registration(mailbox):
                        mark_mailbox_result(
                            mailbox, success=False, error=last_error or "registration_incomplete",
                        )
                    else:
                        mark_mailbox_result(mailbox, success=True)
                    _log(
                        f"[邮箱] OTP 已接受但流程未完成，标记 used 不回池: "
                        f"{_mask_email(mailbox.get('address', ''))}"
                    )
                elif last_error in {"cancelled", "stop requested"}:
                    release_mailbox(mailbox)
                    _log(f"[mail] released (stop): {mailbox.get('address')}")
                else:
                    mark_mailbox_result(
                        mailbox, success=False, error=last_error or "failed"
                    )
            except Exception as e:
                _log(f"[邮箱] 标记结果失败: {e}")
                try:
                    release_mailbox(mailbox)
                except Exception:
                    logger.debug("register_one: ignoring exception", exc_info=True)
        if bb and pid:
            keep = keep_on_fail and not success and last_error not in {"cancelled", RegisteredEmailDetected.code}
            if keep:
                try:
                    await teardown(bb, pid, delete=False)
                    _log(f"[调试] 已保留浏览器窗口: id={pid}；停止任务或 10 分钟后关闭")
                    # 等待上限 10 分钟：无超时的死循环会挂死整批 asyncio.gather，
                    # 且吞掉 CancelledError 会破坏取消语义
                    keep_deadline = time.monotonic() + 600
                    while not _stop() and time.monotonic() < keep_deadline:
                        await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    # 取消信号不能吞：让外层 finally 完成清理后正常传播
                    _log("[调试] 保留等待被取消，继续清理浏览器")
                except Exception as e:
                    _log(f"[调试] 保留浏览器失败: {str(e)[:80]}")
            _unregister_session(bb, pid)
            try:
                await teardown(bb, pid, delete=True)
            except Exception as e:
                _log(f"[清理] 关闭浏览器失败: {str(e)[:80]}")
        try:
            _TASK_IDX.reset(token)
        except Exception:
            logger.debug("register_one: ignoring exception", exc_info=True)
        try:
            _TASK_EMAIL.reset(email_token)
        except Exception:
            logger.debug("register_one: ignoring exception", exc_info=True)
