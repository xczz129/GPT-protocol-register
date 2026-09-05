# -*- coding: utf-8 -*-
"""Page helpers: wait/detect/click/fill for ChatGPT auth UI."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.browser import react_fill, js_click
from register.cf import _switch_cf_node
from register.log import _log
from register.util import ROOT as PROJECT_ROOT, _COOKIE_BTNS

ROOT = PROJECT_ROOT

EMAIL_INPUT_SELECTOR = (
    'input[type="email"], input[name="email"], input[autocomplete="email"], '
    'input[autocomplete="username"], input[placeholder*="email" i], '
    'input[aria-label*="email" i], input[data-testid*="email" i]'
)

VISIBLE_EMAIL_INPUT_SELECTOR = ", ".join(
    f"{selector.strip()}:visible"
    for selector in EMAIL_INPUT_SELECTOR.split(",")
)

EMAIL_CONTINUE_LABELS = (
    "Continue",
    "続行",
    "次へ",
    "继续",
    "繼續",
    "Teruskan",
)

OTP_INPUT_SELECTOR = (
    'input[name="code"], input[inputmode="numeric"], '
    'input[autocomplete="one-time-code"]'
)


def _is_navigation_race(error: BaseException | str) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "execution context was destroyed",
            "cannot find context with specified id",
            "inspected target navigated or closed",
            "waiting for navigation to finish",
            "ns_binding_aborted",
            "frame was detached",
        )
    )


async def _page_url(page) -> str:
    try:
        return await asyncio.wait_for(
            page.evaluate("() => location.href || ''"), timeout=2
        ) or ""
    except Exception:
        try:
            return getattr(page, "url", "") or ""
        except Exception:
            return ""


def _is_login_url(url: str) -> bool:
    u = (url or "").lower()
    return "auth/login" in u or u.rstrip("/").endswith("/login")


def login_email_query(url: str) -> str | None:
    """Return the login route's email query, preserving an explicit blank."""
    try:
        raw_query = urlparse(str(url or "")).query
        # parse_qs 会把 "+" 解码成空格，别名邮箱（base+split@domain）会匹配失败，
        # 先把 "+" 保护为 %2B 再解码
        safe_query = raw_query.replace("+", "%2B")
        query = parse_qs(safe_query, keep_blank_values=True)
        if "email" not in query:
            return None
        return str((query.get("email") or [""])[0]).strip()
    except Exception:
        return None


def _is_about_you_url(url: str) -> bool:
    u = (url or "").lower()
    return "about-you" in u or "about_you" in u


def _is_verify_url(url: str) -> bool:
    u = (url or "").lower()
    return any(k in u for k in ("email-verification", "verify", "verification"))


def _is_existing_account_password_url(url: str) -> bool:
    u = (url or "").lower()
    return (
        any(marker in u for marker in ("/log-in/password", "/login/password", "/sign-in/password"))
        and "create-account" not in u
    )


def _is_auth_error_url(url: str) -> bool:
    u = (url or "").lower()
    return "/auth/error" in u or "auth.openai.com/error" in u


def auth_error_code_from_url(url: str) -> str:
    """Decode AuthApiFailure payloads embedded in /error URLs."""
    try:
        parsed = urlparse(str(url or ""))
        if not parsed.path.rstrip("/").endswith("/error"):
            return ""
        query = parse_qs(parsed.query)
        direct = str((query.get("error") or [""])[0]).strip().lower()
        if direct:
            return "".join(ch if ch.isalnum() else "_" for ch in direct).strip("_")
        payload = str((query.get("payload") or [""])[0]).strip()
        if not payload:
            return ""
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.b64decode(payload).decode("utf-8"))
        return str(data.get("errorCode") or data.get("error_code") or "").strip().lower()
    except Exception:
        return ""

async def dump_state(page, tag: str = "", verbose: bool = False) -> None:
    """Light page snapshot. Full input/button scan only when verbose=True."""
    try:
        url = await asyncio.wait_for(page.evaluate("() => location.href"), timeout=3)
    except Exception:
        try:
            url = page.url
        except Exception:
            url = "?"
    _log(f"--- 页面状态 {tag} ---")
    _log(f"url: {url}")
    if not verbose:
        return
    try:
        info = await asyncio.wait_for(
            page.evaluate(
                """() => {
                    const inputs = Array.from(document.querySelectorAll('input')).slice(0,6).map((el,i)=>({
                        i, type: el.type, name: el.name, ph: el.placeholder||''
                    }));
                    const buttons = Array.from(document.querySelectorAll('button')).slice(0,10)
                        .map(b => (b.innerText||'').trim().slice(0,30)).filter(Boolean);
                    const body = (document.body && document.body.innerText || '').slice(0,220).replace(/\\n/g,' | ');
                    return {inputs, buttons, body};
                }"""
            ),
            timeout=4,
        )
        for inp in info.get("inputs") or []:
            _log(f"  input[{inp.get('i')}] type={inp.get('type')} name={inp.get('name')} placeholder={inp.get('ph')}")
        _log(f"  buttons: {info.get('buttons')}")
        _log(f"  body: {info.get('body')}")
    except Exception as e:
        _log(f"页面状态读取失败: {e}")


async def safe_body_text(page, limit: int = 800, timeout: float = 3.0) -> str:
    """Read body text via evaluate — Playwright inner_text hangs on Obscura."""
    try:
        return await asyncio.wait_for(
            page.evaluate(
                """(lim) => (document.body && document.body.innerText || '').slice(0, lim)""",
                limit,
            ),
            timeout=timeout,
        ) or ""
    except Exception:
        return ""


async def safe_count(page, selector: str, timeout: float = 3.0) -> int:
    """Count elements via querySelectorAll — more reliable than locator.count on Obscura."""
    try:
        n = await asyncio.wait_for(
            page.evaluate(
                """(sel) => {
                    try { return document.querySelectorAll(sel).length; }
                    catch (e) { return 0; }
                }""",
                selector,
            ),
            timeout=timeout,
        )
        return int(n or 0)
    except Exception as error:
        if _is_navigation_race(error):
            return 0
        try:
            return int(
                await asyncio.wait_for(
                    page.locator(selector).count(),
                    timeout=max(0.2, timeout),
                )
            )
        except Exception:
            return 0


async def wait_for_any(
    page,
    *,
    selectors: list[str] | None = None,
    url_substrings: list[str] | None = None,
    timeout: float = 15.0,
    poll: float = 0.15,
) -> bool:
    """Poll until any selector is present or URL contains any substring."""
    deadline = time.monotonic() + max(0.3, timeout)
    sels = selectors or []
    combined_sel = ", ".join(sels)
    urls = [u.lower() for u in (url_substrings or [])]
    while time.monotonic() < deadline:
        try:
            # Prefer cheap page.url; evaluate only as fallback
            cur = ""
            try:
                cur = (getattr(page, "url", "") or "").lower()
            except Exception:
                cur = ""
            if not cur:
                cur = await asyncio.wait_for(
                    page.evaluate("() => (location.href || '').toLowerCase()"), timeout=1
                )
                cur = cur or ""
            for u in urls:
                if u not in cur:
                    continue
                # bare chatgpt.com/ must not match /auth/ paths
                if u == "chatgpt.com/" and "/auth" in cur:
                    continue
                return True
        except Exception:
            pass
        if combined_sel and await safe_count(page, combined_sel, timeout=0.6) > 0:
            return True
        await asyncio.sleep(poll)
    return False




async def wait_url_changed(page, prev_url: str, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    prev = (prev_url or "").lower()
    while time.monotonic() < deadline:
        try:
            cur = (page.url or "").lower()
            if cur and cur != prev:
                return True
        except Exception:
            return True
        await asyncio.sleep(0.2)
    return False


async def is_chatgpt_main(page) -> bool:
    """True when the logged-in app DOM is present, even if SPA URL is stale."""
    try:
        url = await asyncio.wait_for(
            page.evaluate("() => (location.href || '').toLowerCase()"), timeout=2
        )
        url = url or ""
    except Exception:
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
    if "chatgpt.com" not in url:
        return False
    if await safe_count(
        page,
        '#prompt-textarea, [data-testid="composer-speech-button"], textarea',
        timeout=0.8,
    ) > 0:
        return True
    body = (await safe_body_text(page, 400, timeout=1.0)).lower()
    app_markers = (
        "what's on your mind",
        "what’s on your mind",
        "ready when you are",
        "new chat",
        "search chats",
    )
    if sum(marker in body for marker in app_markers) >= 2:
        return True
    if any(k in url for k in ("/auth", "about-you", "email-verification", "onboarding")):
        return False
    return "chatgpt.com" in url and "/auth" not in url



async def click_exact(page, label: str, timeout: int = 5000) -> bool:
    """Click a button by label. Prefer Playwright trusted click, then JS."""
    # 0) Playwright exact role first (Camoufox needs trusted click for form submit)
    try:
        btn = page.get_by_role("button", name=label, exact=True)
        if await asyncio.wait_for(btn.count(), timeout=0.8) > 0:
            target = btn.first
            try:
                if not await asyncio.wait_for(target.is_enabled(), timeout=0.5):
                    for _ in range(8):
                        await asyncio.sleep(0.25)
                        if await asyncio.wait_for(target.is_enabled(), timeout=0.5):
                            break
            except Exception:
                pass
            await target.click(timeout=min(timeout, 4000))
            return True
    except Exception as e:
        if "Execution context was destroyed" in str(e):
            return True
    # 1) JS: match button/link text exactly (skip social providers)
    try:
        ok = await asyncio.wait_for(page.evaluate(
            """(label) => {
                const isSocial = (t) => {
                    const s = (t || '').toLowerCase();
                    return s.includes('google') || s.includes('apple') || s.includes('phone')
                        || s.includes('microsoft') || s.includes('电话') || s.includes('手機')
                        || s.includes('continue with');
                };
                const nodes = Array.from(document.querySelectorAll(
                    'button, a[role="button"], [role="button"]'
                ));
                for (const el of nodes) {
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t === label && !isSocial(t)) {
                        el.click();
                        return true;
                    }
                }
                // partial match fallback (not social, not "Continue with …")
                for (const el of nodes) {
                    if (el.disabled) continue;
                    const t = (el.innerText || el.textContent || '').trim();
                    if (isSocial(t)) continue;
                    if (t.includes(label) && t.length < label.length + 40) {
                        if (label === 'Continue' && t.toLowerCase().includes('continue with')) continue;
                        el.click();
                        return true;
                    }
                }
                return false;
            }""",
            label,
        ), timeout=0.8)
        if ok:
            return True
    except Exception:
        pass
    # 2) Playwright short timeout — exact role match only
    try:
        btn = page.get_by_role("button", name=label, exact=True)
        if await asyncio.wait_for(btn.count(), timeout=0.8) > 0:
            await btn.first.click(timeout=min(timeout, 1500))
            return True
    except Exception:
        pass
    try:
        cand = page.locator(f'button:has-text("{label}")')
        n = int(await asyncio.wait_for(cand.count(), timeout=0.8) or 0)
        for i in range(max(0, n)):
            try:
                t = (await cand.nth(i).inner_text()).strip()
            except Exception:
                continue
            if t == label:
                await cand.nth(i).click(timeout=min(timeout, 1500))
                return True
    except Exception:
        pass
    return False


async def click_any_exact(page, labels) -> bool:
    wanted = [str(label) for label in labels if str(label)]
    if not wanted:
        return False
    try:
        matched = await asyncio.wait_for(
            page.evaluate(
                """(labels) => {
                    const wanted = new Set(labels);
                    for (const el of document.querySelectorAll('button, a[role="button"], [role="button"]')) {
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                        const text = (el.innerText || el.textContent || '').trim();
                        const lower = text.toLowerCase();
                        if (lower.includes('google') || lower.includes('apple') || lower.includes('continue with')) continue;
                        if (wanted.has(text)) return text;
                    }
                    return '';
                }""",
                wanted,
            ),
            timeout=1.0,
        )
        return bool(matched) and await click_exact(page, str(matched), timeout=2500)
    except Exception:
        return False


async def switch_password_to_email_code(page) -> bool:
    """Switch an existing-account password page to its email-code route."""
    labels = (
        "Log in with a code",
        "Email me a login code",
        "Continue with email code",
        "Continue with email verification code",
        "Use a login code",
        "Use email code",
        "Send me a code",
        "使用验证码登录",
        "通过验证码登录",
        "使用邮箱验证码",
    )
    for label in labels:
        if await click_exact(page, label, timeout=2500):
            return True

    # Text changes occasionally. Keep the DOM fallback constrained to a
    # code-login control on the password page.
    try:
        clicked = bool(
            await asyncio.wait_for(
                page.evaluate(
                    """() => {
                        const nodes = document.querySelectorAll(
                            'button, a, [role="button"]'
                        );
                        for (const el of nodes) {
                            if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                            const text = (el.innerText || el.textContent || '').trim();
                            const lower = text.toLowerCase();
                            const codeLogin = lower.includes('code') && (
                                lower.includes('log in') || lower.includes('login') ||
                                lower.includes('email') || lower.includes('verification')
                            );
                            const localized = text.includes('验证码') && (
                                text.includes('登录') || text.includes('邮箱')
                            );
                            if (!codeLogin && !localized) continue;
                            if (lower.includes('google') || lower.includes('apple') ||
                                lower.includes('phone')) continue;
                            el.click();
                            return true;
                        }
                        return false;
                    }"""
                ),
                timeout=2.0,
            )
        )
        if clicked:
            return True
    except Exception:
        pass

    # The browser is already holding the login transaction cookies. Requesting
    # passwordless OTP from this page is the same endpoint used by the UI and
    # avoids depending on button copy.
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                """async () => {
                    const response = await fetch('/api/accounts/passwordless/send-otp', {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json',
                            'Content-Type': 'application/json',
                        },
                        body: '{}',
                    });
                    return {ok: response.ok, status: response.status};
                }"""
            ),
            timeout=12,
        )
    except Exception:
        return False
    if not isinstance(result, dict) or not result.get("ok"):
        return False
    try:
        await page.goto(
            "https://auth.openai.com/email-verification",
            timeout=10000,
            wait_until="commit",
        )
    except Exception as error:
        if not _is_navigation_race(error):
            return False
    return True


async def click_submit_or_enter(page, enter_selector: str | None = None) -> bool:
    """Click primary submit via JS (avoids Obscura Playwright click hang)."""
    try:
        ok = await asyncio.wait_for(page.evaluate(
            """(emailSelector) => {
                // Ensure email input still has value right before submit
                const emailEl = document.querySelector(emailSelector);
                if (emailEl && emailEl.value) {
                    try {
                        const tracker = emailEl._valueTracker;
                        if (tracker) tracker.setValue('');
                    } catch (e) {}
                    try {
                        emailEl.dispatchEvent(new InputEvent('input', {
                            bubbles: true, data: emailEl.value, inputType: 'insertText'
                        }));
                    } catch (e) {
                        emailEl.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                    emailEl.dispatchEvent(new Event('change', {bubbles: true}));
                    // fiber onChange again
                    const keys = Object.keys(emailEl);
                    for (const k of keys) {
                        if (k.startsWith('__reactProps$') || k.startsWith('__reactEventHandlers$')) {
                            const p = emailEl[k];
                            if (p && typeof p.onChange === 'function') {
                                try {
                                    p.onChange({
                                        target: emailEl, currentTarget: emailEl,
                                        type: 'change', bubbles: true,
                                        preventDefault() {}, stopPropagation() {}, persist() {},
                                        isDefaultPrevented() { return false; },
                                        isPropagationStopped() { return false; },
                                    });
                                } catch (e) {}
                            }
                        }
                    }
                }
                const subs = Array.from(document.querySelectorAll('button[type="submit"]'));
                for (const b of subs) {
                    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                    const t = (b.innerText || '').toLowerCase();
                    if (t.includes('google') || t.includes('apple') || t.includes('phone')) continue;
                    b.click();
                    return true;
                }
                // Continue button by text (not social)
                const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                for (const b of btns) {
                    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                    const t = (b.innerText || '').trim();
                    const tl = t.toLowerCase();
                    if (tl.includes('google') || tl.includes('apple') || t.toLowerCase().includes('continue with')) continue;
                    if (t === 'Continue' || t === '继续' || t === '繼續' || t === 'Next' || t === '下一步') {
                        b.click();
                        return true;
                    }
                }
                const form = document.querySelector('form');
                if (form) {
                    if (form.requestSubmit) { form.requestSubmit(); return true; }
                    form.submit();
                    return true;
                }
                return false;
            }""",
            EMAIL_INPUT_SELECTOR,
        ), timeout=1.2)
        if ok:
            return True
    except Exception:
        pass
    if await js_click(page, 'button[type="submit"]'):
        return True
    if enter_selector:
        try:
            await page.locator(enter_selector).first.press("Enter", timeout=1500)
            return True
        except Exception:
            pass
    return False



async def dismiss_cookie_banner(page) -> bool:
    if not await click_any_exact(page, _COOKIE_BTNS):
        return False
    _log("[Cookie] 已关闭弹窗")
    await asyncio.sleep(0.2)
    return True


async def fill_email_verified(page, email: str, tries: int = 2) -> bool:
    """Fill email (Camoufox). Prefer one-shot JS write like Grok register."""
    sel = EMAIL_INPUT_SELECTOR
    try:
        url = await _page_url(page)
        if _is_verify_url(url) or _is_about_you_url(url) or await is_chatgpt_main(page):
            return True
        if await safe_count(page, sel, timeout=0.5) == 0:
            if await safe_count(
                page,
                'input[name="code"], input[inputmode="numeric"], '
                'input[type="password"], input[name="age"]',
                timeout=0.5,
            ) > 0:
                return True
    except Exception:
        pass

    for i in range(tries):
        await dismiss_cookie_banner(page)
        try:
            result = await asyncio.wait_for(page.evaluate(
                """(args) => {
                    const {email, selector} = args;
                    function isVisible(node) {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }
                    const el = Array.from(document.querySelectorAll(selector))
                        .find((n) => isVisible(n) && !n.disabled) || null;
                    if (!el) return {ok:false, reason:'no_el'};
                    el.focus();
                    try { el.click(); } catch (e) {}
                    const desc = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    );
                    try {
                        const tracker = el._valueTracker;
                        if (tracker) tracker.setValue('');
                    } catch (e) {}
                    if (desc && desc.set) {
                        desc.set.call(el, '');
                        desc.set.call(el, email);
                    } else {
                        el.value = email;
                    }
                    try {
                        el.dispatchEvent(new InputEvent('input', {
                            bubbles: true, cancelable: true,
                            data: email, inputType: 'insertText'
                        }));
                    } catch (e) {
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    try {
                        for (const k of Object.keys(el)) {
                            if (k.startsWith('__reactProps$') || k.startsWith('__reactEventHandlers$')) {
                                const p = el[k];
                                if (p && typeof p.onChange === 'function') {
                                    p.onChange({
                                        target: el, currentTarget: el, type: 'change',
                                        bubbles: true,
                                        preventDefault() {}, stopPropagation() {}, persist() {},
                                        isDefaultPrevented() { return false; },
                                        isPropagationStopped() { return false; },
                                    });
                                }
                            }
                        }
                    } catch (e) {}
                    try { el.blur(); } catch (e) {}
                    return {ok: (el.value || '').trim() === email, value: (el.value || '').trim()};
                }""",
                {"email": email, "selector": sel},
            ), timeout=1.2)
            if isinstance(result, dict) and result.get("ok"):
                return True
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                return True

        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=3000)
            await loc.fill(email, timeout=2000)
            got = (await loc.input_value(timeout=1000) or "").strip()
            if got == email:
                return True
            cur = got
        except Exception:
            cur = ""

        _log(f"[2] 邮箱未写入成功，重试 {i + 1}/{tries} · 当前='{str(cur)[:40]}'")
        await asyncio.sleep(0.2)
    return False


async def fill_otp_verified(page, code: str, tries: int = 2) -> bool:
    """Enter OTP through trusted keyboard events and verify the rendered value."""
    expected = str(code or "").strip()
    if not expected:
        return False

    for attempt in range(tries):
        inputs = page.locator(OTP_INPUT_SELECTOR)
        try:
            count = min(await inputs.count(), 8)
        except Exception:
            count = 0
        visible = []
        for index in range(count):
            item = inputs.nth(index)
            try:
                if await item.is_visible():
                    visible.append(item)
            except Exception:
                continue
        if not visible:
            return False

        # Clear every visible cell with real key events. This supports both a
        # single controlled input and segmented one-character OTP controls.
        for item in visible:
            try:
                await item.click(timeout=1000)
                await item.press("Control+A", timeout=500)
                await item.press("Backspace", timeout=500)
            except Exception:
                pass
        try:
            await visible[0].click(timeout=1200)
            await page.keyboard.type(expected, delay=35)
        except Exception:
            pass
        await asyncio.sleep(0.2)

        values = []
        for item in visible:
            try:
                values.append((await item.input_value(timeout=700) or "").strip())
            except Exception:
                values.append("")
        if expected in values or "".join(values) == expected:
            return True

        # Retain the established controlled-input fallback for uncommon UIs.
        if await react_fill(
            page,
            OTP_INPUT_SELECTOR,
            expected,
            tries=1,
            delay=8,
            settle=0.08,
            verbose=False,
        ):
            return True
        _log(f"[4] 验证码未写入成功，重试 {attempt + 1}/{tries}")
    return False


async def submit_email_continue(
    page,
    email: str,
    *,
    retry_click: bool = True,
) -> str:
    """Camoufox-friendly email submit: trusted fill + Continue (+ re-click if spinner).

    On Camoufox/Firefox the first Continue often spins 5–10s without leaving
    the login page. A second trusted Continue (without retyping) usually
    completes the authorize → email-verification redirect.
    """
    try:
        u = await _page_url(page)
        if _is_auth_error_url(u):
            return "auth_error"
        if _is_verify_url(u) or _is_about_you_url(u) or "auth.openai.com" in u:
            return "advanced"
        if await safe_count(
            page,
            'input[name="code"], input[inputmode="numeric"], input[type="password"]',
            timeout=0.4,
        ) > 0:
            return "advanced"
    except Exception:
        pass

    sel = EMAIL_INPUT_SELECTOR
    loc = page.locator(sel).first
    try:
        await loc.wait_for(state="visible", timeout=5000)
    except Exception:
        return "no_el"

    # Use a real browser input event. Camoufox can update the DOM value via
    # locator.fill() without synchronizing the React login form state.
    filled_ok = False
    try:
        await loc.click(timeout=1500)
        await loc.press("Control+A", timeout=800)
        await loc.press("Backspace", timeout=800)
        await page.keyboard.type(email, delay=25)
        got = (await loc.input_value(timeout=800) or "").strip()
        filled_ok = got == email
    except Exception as e:
        if "Execution context was destroyed" in str(e):
            return "clicked"

    if not filled_ok:
        # JS setter + fiber as backup
        try:
            ok = await asyncio.wait_for(page.evaluate(
                """(args) => {
                    const {email, selector} = args;
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    el.focus();
                    const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                    try { if (el._valueTracker) el._valueTracker.setValue(''); } catch (e) {}
                    if (desc && desc.set) { desc.set.call(el, ''); desc.set.call(el, email); }
                    else el.value = email;
                    try {
                        el.dispatchEvent(new InputEvent('input', {
                            bubbles: true, data: email, inputType: 'insertText'
                        }));
                    } catch (e) {
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return (el.value || '').trim() === email;
                }""",
                {"email": email, "selector": sel},
            ), timeout=1.2)
            filled_ok = bool(ok)
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                return "clicked"

    if not filled_ok:
        if not await fill_email_verified(page, email, tries=1):
            return "fill_fail"

    await asyncio.sleep(0.15)

    async def _already_advanced() -> bool:
        try:
            u = await _page_url(page)
            if _is_auth_error_url(u):
                return False
            if _is_verify_url(u) or _is_about_you_url(u) or "auth.openai.com" in u:
                return True
            if any(k in u for k in ("password", "create-account", "username_password")):
                return True
            if await safe_count(
                page,
                'input[name="code"], input[inputmode="numeric"], '
                'input[type="password"], input[name="age"]',
                timeout=0.35,
            ) > 0:
                return True
        except Exception:
            pass
        return False

    async def _click_continue_once() -> bool:
        """Trusted Continue / submit. Returns True if click was issued."""
        try:
            btn = page.get_by_role("button", name="Continue", exact=True)
            if await asyncio.wait_for(btn.count(), timeout=1.0) > 0:
                target = btn.first
                try:
                    for _ in range(12):
                        if await asyncio.wait_for(target.is_enabled(), timeout=0.5):
                            break
                        await asyncio.sleep(0.1)
                except Exception:
                    pass
                # Do not spend the whole navigation window inside click(). The
                # state watcher below owns navigation/DOM transition timing.
                await target.click(timeout=2500, no_wait_after=True)
                return True
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                return True
            if "TimeoutError" in type(e).__name__ or "Timeout" in str(e):
                # A click timeout can happen before the pointer event is sent.
                # Only accept it when the page visibly entered submission;
                # otherwise continue to the DOM form fallback below.
                if await _already_advanced() or await _is_continue_spinning():
                    return True
                _log("[2] Continue 点击未落地，改用页面表单提交…")
        try:
            if await click_exact(page, "Continue", timeout=2500):
                return True
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                return True
        try:
            if await click_submit_or_enter(page, sel):
                return True
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                return True
        # form.requestSubmit as last resort
        try:
            ok = await asyncio.wait_for(
                page.evaluate(
                    """() => {
                        const form = document.querySelector('form');
                        if (!form) return false;
                        const btn = form.querySelector('button[type="submit"]');
                        if (btn && form.requestSubmit) { form.requestSubmit(btn); return true; }
                        if (form.requestSubmit) { form.requestSubmit(); return true; }
                        form.submit();
                        return true;
                    }"""
                ),
                timeout=1.0,
            )
            return bool(ok)
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                return True
        return False

    async def _is_continue_spinning() -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    page.evaluate(
                        """() => {
                            const nodes = Array.from(document.querySelectorAll(
                                'button, [role="button"]'
                            ));
                            for (const b of nodes) {
                                const t = (b.innerText || b.textContent || '').trim();
                                if (t !== 'Continue' && t !== '继续' && t !== '繼續') continue;
                                if (b.disabled || b.getAttribute('aria-busy') === 'true'
                                    || b.getAttribute('data-loading') === 'true') return true;
                                if (b.querySelector(
                                    '[class*="spin"],[class*="loader"],[class*="Spinner"],[class*="loading"],svg[class*="animate"]'
                                )) return true;
                            }
                            return false;
                        }"""
                    ),
                    timeout=0.8,
                )
            )
        except Exception:
            return False

    clicked = await _click_continue_once()
    if not clicked:
        return "filled"

    # Pure-browser mode issues one trusted click per outer attempt. Hybrid mode
    # retains the established one re-click behavior for its protocol fallback.
    waves = ((2.5, True), (4.5, True)) if retry_click else ((5.0, False),)
    for wait_seconds, can_retry in waves:
        deadline = time.monotonic() + wait_seconds
        saw_spin = False
        while time.monotonic() < deadline:
            if await _already_advanced():
                return "advanced"
            if await _is_continue_spinning():
                saw_spin = True
            await asyncio.sleep(0.35)
        if await _already_advanced():
            return "advanced"
        if not can_retry:
            break
        # Still on login: re-click Continue once without retyping (Camoufox pattern)
        try:
            cur = await _page_url(page)
            still_email = await safe_count(page, sel, timeout=0.4) > 0
            if still_email and _is_login_url(cur):
                # ensure value still present
                try:
                    got = (await loc.input_value(timeout=500) or "").strip()
                    if got != email:
                        await loc.fill(email, timeout=2000)
                except Exception:
                    pass
                _log(
                    f"[2] Continue 仍在登录页"
                    f"{'（转圈）' if saw_spin else ''}，再点一次…"
                )
                await _click_continue_once()
                continue
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                return "clicked"
        break

    if await _already_advanced():
        return "advanced"
    return "clicked"


async def submit_email_browser_ui(page, email: str) -> str:
    """Submit the email through UI and require a real auth request as evidence."""
    current = await _page_url(page)
    if _is_auth_error_url(current):
        return "auth_error"
    if not _is_login_url(current):
        return "advanced"

    try:
        stable = await page.wait_for_function(
            """(selector) => {
                    const visible = (node) => {
                        if (!node || node.disabled) return false;
                        const style = getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden'
                            && rect.width > 0 && rect.height > 0;
                    };
                    const input = Array.from(document.querySelectorAll(selector)).find(visible);
                    const form = input && input.closest('form');
                    const labels = new Set([
                        'Continue', '続行', '次へ', '继续', '繼續', 'Teruskan'
                    ]);
                    const button = form && Array.from(form.querySelectorAll('button'))
                        .find((node) => visible(node)
                            && labels.has((node.innerText || node.textContent || '').trim()));
                    if (!input || !form || !button || document.readyState === 'loading') {
                        window.__oaiEmailFormStable = null;
                        return false;
                    }
                    const previous = window.__oaiEmailFormStable;
                    if (!previous || previous.input !== input
                        || previous.form !== form || previous.button !== button) {
                        window.__oaiEmailFormStable = {
                            input, form, button, since: performance.now()
                        };
                        return false;
                    }
                    return performance.now() - previous.since >= 800;
                }""",
            arg=EMAIL_INPUT_SELECTOR,
            timeout=6000,
        )
        if stable is None:
            return "form_unstable"
    except Exception as error:
        if _is_navigation_race(error):
            return "advanced"
        if "timeout" in str(error).lower():
            return "form_unstable"
        _log(f"[2] [全浏览器] 表单稳定性检查异常: {str(error)[:120]}")
        return "form_unstable"
    # Do not use Playwright's locator.fill() as the primary path here.  The
    # current ChatGPT login form is React-controlled and Camoufox can update
    # the DOM value without updating the form state.  In that state the
    # Continue button remains enabled but its submit handler exits before
    # issuing /api/auth/signin/openai.  The verified helper uses the native
    # setter, input/change events and the React onChange bridge.
    try:
        if not await fill_email_verified(page, email, tries=2):
            return "fill_fail"
        loc = page.locator(VISIBLE_EMAIL_INPUT_SELECTOR).first
        await loc.wait_for(state="visible", timeout=3000)
        value = (await loc.input_value(timeout=800) or "").strip()
    except Exception as error:
        if _is_navigation_race(error):
            return "advanced"
        _log(f"[2] [全浏览器] 稳定输入框在操作时被替换: {str(error)[:100]}")
        return "form_unstable"
    if value != email:
        return "fill_fail"
    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass

    auth_started = asyncio.Event()

    def capture_auth_request(request) -> None:
        try:
            url = str(request.url or "")
            method = str(request.method or "GET").upper()
            if (
                (method == "POST" and "/api/auth/signin/" in url)
                or "auth.openai.com/api/accounts/authorize" in url
            ):
                auth_started.set()
        except Exception:
            pass

    try:
        page.on("request", capture_auth_request)
    except Exception:
        pass

    async def observe(timeout: float) -> str:
        deadline = time.monotonic() + timeout
        next_dom_check = 0.0
        saw_matching_route = False
        saw_mismatched_route = False
        while time.monotonic() < deadline:
            if auth_started.is_set():
                return "submitted"
            try:
                url = str(getattr(page, "url", "") or "")
            except Exception:
                url = ""
            if url and not _is_login_url(url):
                return "advanced"
            routed_email = login_email_query(url)
            if routed_email is not None:
                if routed_email == email:
                    saw_matching_route = True
                else:
                    saw_mismatched_route = True
            now = time.monotonic()
            if now >= next_dom_check:
                next_dom_check = now + 1.0
                if await safe_count(
                    page,
                    'input[name="code"], input[inputmode="numeric"], '
                    'input[type="password"], input[name="age"]',
                    timeout=0.4,
                ) > 0:
                    return "advanced"
            await asyncio.sleep(0.2)
        if auth_started.is_set():
            return "submitted"
        if saw_matching_route:
            return "routed"
        if saw_mismatched_route:
            return "email_state_lost"
        return ""

    try:
        button = None
        for label in EMAIL_CONTINUE_LABELS:
            candidate = page.get_by_role("button", name=label, exact=True)
            try:
                if await asyncio.wait_for(candidate.count(), timeout=0.6) > 0:
                    button = candidate.first
                    break
            except Exception as error:
                if _is_navigation_race(error):
                    return "advanced"
        try:
            if button is not None:
                # The consent sheet can be mounted lazily and cause a single
                # React render. Re-read the controlled input immediately
                # before the trusted click so the submit handler sees the
                # current email rather than a stale DOM-only value.
                current_value = (
                    await page.locator(VISIBLE_EMAIL_INPUT_SELECTOR).first
                    .input_value(timeout=800)
                    or ""
                ).strip()
                if current_value != email:
                    if not await fill_email_verified(page, email, tries=1):
                        return "fill_fail"
                await button.click(timeout=3000, no_wait_after=True)
        except Exception as error:
            if _is_navigation_race(error):
                return "advanced"
        state = await observe(7.0)
        if state:
            return state
        routed_email = login_email_query(await _page_url(page))
        if routed_email is not None:
            return "routed" if routed_email == email else "email_state_lost"
        try:
            current_input = page.locator(VISIBLE_EMAIL_INPUT_SELECTOR).first
            current_value = (
                await current_input.input_value(timeout=800) or ""
            ).strip()
            if current_value != email:
                return "email_state_lost"
        except Exception:
            return "email_state_lost"
        return "no_auth_request"
    finally:
        try:
            page.remove_listener("request", capture_auth_request)
        except Exception:
            pass


async def start_nextauth_email_flow(page, email: str) -> str:
    """Start the HAR-observed NextAuth flow inside the current browser session."""
    current = str(await _page_url(page) or "")
    if _is_auth_error_url(current):
        return f"auth_error_{auth_error_code_from_url(current) or 'unknown'}"
    if "auth.openai.com" in current or _is_verify_url(current):
        return "advanced"
    device_id = ""
    try:
        cookies = await page.context.cookies("https://chatgpt.com")
        device_id = next(
            (
                str(item.get("value") or "")
                for item in cookies
                if str(item.get("name") or "") == "oai-did"
            ),
            "",
        )
    except Exception:
        pass

    result = None
    request_context = getattr(getattr(page, "context", None), "request", None)
    if request_context is not None:
        try:
            csrf_response = await asyncio.wait_for(
                request_context.get(
                    "https://chatgpt.com/api/auth/csrf",
                    headers={"Accept": "application/json"},
                    timeout=10000,
                ),
                timeout=12,
            )
            csrf_data = await csrf_response.json()
            csrf_token = str((csrf_data or {}).get("csrfToken") or "")
            if not csrf_token:
                raise RuntimeError(f"csrf_http_{csrf_response.status}")
            query = urlencode(
                {
                    "prompt": "login",
                    "ext-passkey-client-capabilities": "11111",
                    "ext-oai-did": device_id or str(uuid.uuid4()),
                    "auth_session_logging_id": str(uuid.uuid4()),
                    "screen_hint": "login_or_signup",
                    "login_hint": email,
                }
            )
            signin_response = await asyncio.wait_for(
                request_context.post(
                    f"https://chatgpt.com/api/auth/signin/openai?{query}",
                    headers={"Accept": "application/json"},
                    form={
                        "callbackUrl": "https://chatgpt.com/",
                        "csrfToken": csrf_token,
                        "json": "true",
                    },
                    timeout=12000,
                ),
                timeout=14,
            )
            signin_data = await signin_response.json()
            result = {
                "ok": bool(signin_response.ok),
                "stage": "signin_context_request",
                "status": int(signin_response.status),
                "url": str((signin_data or {}).get("url") or ""),
            }
            _log(
                f"[2] NextAuth 浏览器会话请求完成 · HTTP {signin_response.status}"
            )
        except Exception as error:
            _log(f"[2] NextAuth 浏览器会话请求失败，回退页面请求: {str(error)[:100]}")

    try:
        if result is None:
            result = await asyncio.wait_for(
                page.evaluate(
                    """async ({email, deviceId}) => {
                    const csrfResp = await fetch('/api/auth/csrf', {
                        credentials: 'include',
                        headers: {'Accept': 'application/json'},
                    });
                    if (!csrfResp.ok) {
                        return {ok: false, stage: 'csrf', status: csrfResp.status};
                    }
                    let csrfData = {};
                    try { csrfData = await csrfResp.json(); }
                    catch (e) {
                        return {ok: false, stage: 'csrf_json', status: csrfResp.status};
                    }
                    if (!csrfData || !csrfData.csrfToken) {
                        return {ok: false, stage: 'csrf_json', status: csrfResp.status};
                    }
                    const did = deviceId || crypto.randomUUID();
                    const query = new URLSearchParams({
                        prompt: 'login',
                        'ext-passkey-client-capabilities': '11111',
                        'ext-oai-did': did,
                        auth_session_logging_id: crypto.randomUUID(),
                        screen_hint: 'login_or_signup',
                        login_hint: email,
                    });
                    const body = new URLSearchParams({
                        callbackUrl: 'https://chatgpt.com/',
                        csrfToken: csrfData.csrfToken,
                        json: 'true',
                    });
                    const signInResp = await fetch(
                        `/api/auth/signin/openai?${query.toString()}`,
                        {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Accept': 'application/json',
                                'Content-Type': 'application/x-www-form-urlencoded',
                            },
                            body: body.toString(),
                        },
                    );
                    let data = {};
                    try { data = await signInResp.json(); } catch (e) {}
                    return {
                        ok: signInResp.ok,
                        stage: 'signin',
                        status: signInResp.status,
                        url: data && typeof data.url === 'string' ? data.url : '',
                    };
                    }""",
                    {"email": email, "deviceId": device_id},
                ),
                timeout=12,
            )
    except Exception as error:
        current = str(await _page_url(page) or "")
        if "auth.openai.com" in current or _is_verify_url(current):
            _log("[2] NextAuth 请求期间页面已完成导航")
            return "advanced"
        _log(f"[2] NextAuth 兜底请求失败: {str(error)[:100]}")
        return "error"

    current = str(await _page_url(page) or "")
    if "auth.openai.com" in current or _is_verify_url(current):
        return "advanced"
    if not isinstance(result, dict) or not result.get("ok"):
        stage = str((result or {}).get("stage") or "unknown")
        status = str((result or {}).get("status") or "?")
        _log(f"[2] NextAuth 兜底未成功 · stage={stage} HTTP={status}")
        return f"http_{status}"

    authorize_url = str(result.get("url") or "").strip()
    if not authorize_url.startswith("https://auth.openai.com/"):
        _log("[2] NextAuth 兜底未返回可信 authorize URL")
        return "bad_url"

    # Start the authorize hand-off immediately.  Camoufox can report a goto
    # timeout while the same navigation is still progressing in the browser;
    # observe that in-flight navigation instead of starting a second auth chain.
    try:
        await page.goto(authorize_url, timeout=8000, wait_until="commit")
    except Exception as error:
        error_text = str(error or "")
        if "timeout" in error_text.lower():
            _log("[2] authorize 响应较慢，继续观察当前导航…")
        elif not _is_navigation_race(error):
            _log(
                f"[2] authorize 导航触发异常: {error_text.splitlines()[0][:100]}"
            )

    moved = await wait_for_any(
        page,
        selectors=['input[name="code"]', 'input[type="password"]'],
        url_substrings=[
            "auth.openai.com", "email-verification", "auth/error", "about-you",
        ],
        timeout=10,
        poll=0.2,
    )
    current = await _page_url(page)
    if _is_auth_error_url(current):
        code = auth_error_code_from_url(current) or "unknown"
        _log(f"[2] authorize 进入错误页 · {code}")
        return f"auth_error_{code}"
    if "auth.openai.com" not in current and not moved:
        _log("[2] authorize 在 18s 观察窗口内未推进")
        return "goto_failed"
    return "advanced"


async def validate_otp_via_browser_session(page, code: str) -> bool:
    """Validate OTP by HTTP inside the authenticated browser origin."""
    current = await _page_url(page)
    if "auth.openai.com" not in current or not _is_verify_url(current):
        return False
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                """async (code) => {
                    const response = await fetch('/api/accounts/email-otp/validate', {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json',
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({code}),
                    });
                    let data = {};
                    try { data = await response.json(); } catch (e) {}
                    const candidates = data && typeof data === 'object'
                        ? [data.continue_url, data.continueUrl, data.redirect_url,
                           data.redirectUrl, data.url]
                        : [];
                    const next = candidates.find(value => typeof value === 'string' && value.trim());
                    let continueUrl = '';
                    if (next) {
                        try { continueUrl = new URL(next, location.origin).href; } catch (e) {}
                    }
                    return {
                        ok: response.ok,
                        status: response.status,
                        continueUrl,
                    };
                }""",
                str(code),
            ),
            timeout=15,
        )
    except Exception as error:
        current = await _page_url(page)
        if _is_about_you_url(current):
            return True
        _log(f"[4] [混合] OTP 协议校验异常: {str(error)[:100]}")
        return False
    status = int((result or {}).get("status") or 0)
    if not isinstance(result, dict) or not result.get("ok"):
        _log(f"[4] [混合] OTP 协议校验失败 · HTTP {status}")
        return False
    continue_url = str(result.get("continueUrl") or "").strip()
    if not (
        continue_url.startswith("https://auth.openai.com/")
        and _is_about_you_url(continue_url)
    ):
        continue_url = "https://auth.openai.com/about-you"
        _log("[4] [混合] OTP 已被接受，响应未提供资料页地址，使用当前会话继续")
    else:
        _log(f"[4] [混合] OTP 协议校验成功 · HTTP {status}")
    try:
        await page.goto(continue_url, timeout=20000, wait_until="commit")
    except Exception as error:
        if not _is_navigation_race(error):
            _log(f"[4] [混合] 进入资料页导航异常: {str(error)[:100]}")
    await wait_for_any(
        page,
        selectors=['input[name="age"]', 'input[name="name"]'],
        url_substrings=["about-you"],
        timeout=8,
        poll=0.25,
    )
    return _is_about_you_url(await _page_url(page)) or await safe_count(
        page, 'input[name="age"], input[name="name"]', timeout=0.8
    ) > 0


async def detect_challenge(page) -> bool:
    sel = (
        'iframe[src*="arkose"], #arkose, [data-pkey], #FunCaptcha, '
        '.cf-turnstile, iframe[src*="turnstile"], '
        'iframe[src*="challenges.cloudflare"], iframe[src*="hcaptcha"]'
    )
    return await safe_count(page, sel) > 0


async def _is_cf_blocked(page) -> bool:
    try:
        if await safe_count(page, EMAIL_INPUT_SELECTOR, timeout=0.8) > 0:
            return False
        body = (await safe_body_text(page, limit=1200, timeout=1.0)).strip().lower()
        has_ts = await safe_count(page,
            'input[name="cf-turnstile-response"], .cf-turnstile, '
            'iframe[src*="challenges.cloudflare"]', timeout=0.8
        ) > 0
        challenge_text = any(
            marker in body
            for marker in (
                "verify you are human",
                "checking your browser",
                "performing security verification",
                "security verification",
                "cloudflare ray id",
                "cf-chl-",
            )
        )
        # An empty body is commonly just a Firefox/Camoufox navigation race.
        # Treat only explicit challenge DOM/text as Cloudflare; the caller's
        # hydration recovery handles incomplete document shells.
        return has_ts or challenge_text
    except Exception:
        return False


async def _click_turnstile(page) -> bool:
    for sel in ('iframe[src*="challenges.cloudflare"]', 'iframe[src*="turnstile"]'):
        try:
            if await page.locator(sel).count() > 0:
                fr = page.frame_locator(sel).first
                for inner in ("input[type=checkbox]", "label", "body"):
                    loc = fr.locator(inner)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000)
                        return True
        except Exception:
            pass
    try:
        if await page.locator(".cf-turnstile").count() > 0:
            box = await page.locator(".cf-turnstile").first.bounding_box()
            if box:
                await page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
                return True
    except Exception:
        pass
    return False


async def _click_resend_code(page, *, allow_http_fallback: bool = True) -> int:
    """Trigger OTP resend and return the observed HTTP status.

    A successful DOM click only means the browser accepted the pointer event;
    React can still suppress the request or the API can reject it.  Observe the
    real endpoint and fall back to a same-origin fetch in the current auth
    session when no request was emitted.
    """
    labels = [
        "Resend code", "Resend email", "Resend", "Send again", "Send a new code",
        "再送信", "再送", "コードを再送", "重新发送", "重新發送", "重发",
        "重新傳送電郵", "重新传送电邮", "重新傳送", "重新传送",
        "重新傳送驗證碼", "重新獲取", "Kirim semula", "Hantar semula",
    ]
    loop = asyncio.get_running_loop()
    response_future = loop.create_future()

    def capture(response):
        try:
            url = str(response.url or "")
            if "/api/accounts/email-otp/resend" in url and not response_future.done():
                response_future.set_result(response)
        except Exception:
            pass

    try:
        page.on("response", capture)
    except Exception:
        pass
    clicked_label = ""
    try:
        for lbl in labels:
            for getter in (
                lambda l=lbl: page.get_by_role("button", name=l, exact=False),
                lambda l=lbl: page.get_by_role("link", name=l, exact=False),
                lambda l=lbl: page.locator(f'button:has-text("{l}")'),
                lambda l=lbl: page.locator(f'a:has-text("{l}")'),
            ):
                try:
                    loc = getter()
                    if await loc.count() > 0 and await loc.first.is_visible():
                        try:
                            if not await loc.first.is_enabled():
                                continue
                        except Exception:
                            pass
                        await loc.first.click(timeout=2500, no_wait_after=True)
                        clicked_label = lbl
                        _log(f"[4] 已点击重发入口 ('{lbl}')，等待发送接口响应…")
                        break
                except Exception:
                    pass
            if clicked_label:
                break

        if clicked_label:
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(response_future), timeout=7.0
                )
                status = int(getattr(response, "status", 0) or 0)
                if 200 <= status < 300:
                    _log(f"[4] 验证码发送接口已确认 · HTTP {status}")
                elif status in {409, 425, 429}:
                    _log(f"[4] 验证码发送接口返回 HTTP {status} · 冷却中")
                else:
                    _log(f"[4] 验证码发送接口失败 · HTTP {status}")
                return status
            except asyncio.TimeoutError:
                if not allow_http_fallback:
                    _log("[4] 点击后未观察到发送接口请求；全程浏览器模式不使用接口补发")
                    return 0
                _log("[4] 点击后未观察到发送接口请求，改用当前会话补发…")

        if not allow_http_fallback:
            _log("[4] 页面未触发验证码补发；全程浏览器模式不使用接口补发")
            return 0

        try:
            result = await page.evaluate(
                """async () => {
                    try {
                        const response = await fetch(
                            '/api/accounts/email-otp/resend',
                            {method: 'POST', credentials: 'include'}
                        );
                        return {status: response.status, ok: response.ok};
                    } catch (error) {
                        return {status: 0, ok: false, error: String(error)};
                    }
                }"""
            )
            status = int((result or {}).get("status") or 0)
            if 200 <= status < 300:
                _log(f"[4] 当前浏览器会话补发成功 · HTTP {status}")
            elif status in {409, 425, 429}:
                _log(f"[4] 当前浏览器会话补发返回 HTTP {status} · 冷却中")
            else:
                detail = str((result or {}).get("error") or "")[:100]
                _log(
                    f"[4] 当前浏览器会话补发失败 · HTTP {status}"
                    + (f" · {detail}" if detail else "")
                )
            return status
        except Exception as error:
            _log(f"[4] 当前浏览器会话补发异常: {str(error)[:100]}")
            return 0
    finally:
        try:
            page.remove_listener("response", capture)
        except Exception:
            pass
        if not response_future.done():
            response_future.cancel()


async def blur_field(page, selector: str) -> None:
    try:
        el = page.locator(selector).first
        if await asyncio.wait_for(el.count(), timeout=0.8) == 0:
            return
        await el.evaluate(
            """(node) => {
                node.dispatchEvent(new Event('blur', {bubbles: true}));
                node.dispatchEvent(new Event('focusout', {bubbles: true}));
                if (typeof node.blur === 'function') node.blur();
            }"""
        )
    except Exception:
        pass


async def _about_you_left(page) -> bool:
    if await is_chatgpt_main(page):
        return True
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "/auth/error" in url or "error=oauthcallback" in url:
        return False
    if "about-you" not in url and "about_you" not in url:
        return True
    return False


async def _force_react_value(page, selector: str, value: str) -> bool:
    """Stronger fill for OpenAI React controlled inputs."""
    el = page.locator(selector).first
    try:
        if await asyncio.wait_for(el.count(), timeout=0.8) == 0:
            return False
    except Exception:
        return False
    for attempt in range(3):
        try:
            await el.click(timeout=2500)
            await el.fill("")
            await el.fill(value)
            # light type only if fill didn't stick
            got_pre = ""
            try:
                got_pre = (await el.input_value()).strip()
            except Exception:
                pass
            if got_pre != value:
                await el.type(value, delay=15)
            await el.evaluate(
                """(node, v) => {
                    const proto = node.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(node, v);
                    else node.value = v;
                    for (const ev of ['input', 'change', 'blur']) {
                        node.dispatchEvent(new Event(ev, { bubbles: true }));
                    }
                    try {
                        node.dispatchEvent(new InputEvent('input', { bubbles: true, data: v, inputType: 'insertText' }));
                    } catch (e) {}
                }""",
                value,
            )
            await blur_field(page, selector)
            await asyncio.sleep(0.12)
            got = (await el.input_value()).strip()
            if got == value:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.15)
    return False


async def _recover_nextauth_session(page) -> bool:
    """Open ChatGPT root when callback already set the NextAuth session cookie."""
    try:
        cookies = await page.context.cookies("https://chatgpt.com")
        has_session = any(
            str(item.get("name") or "") == "__Secure-next-auth.session-token"
            and bool(str(item.get("value") or ""))
            for item in cookies
        )
    except Exception:
        has_session = False
    if not has_session:
        return False
    _log("[资料] OAuth callback 已设置 session，直接打开 ChatGPT 主页")
    try:
        await page.goto("https://chatgpt.com/", timeout=15000, wait_until="commit")
    except Exception:
        pass
    await wait_for_any(
        page,
        selectors=['#prompt-textarea', 'textarea'],
        url_substrings=["chatgpt.com/"],
        timeout=8,
        poll=0.25,
    )
    if await is_chatgpt_main(page):
        return True
    current = await _page_url(page)
    current_lower = current.lower()
    return "chatgpt.com" in current_lower and "/auth/error" not in current_lower


async def follow_create_account_response(page, response, callback_future=None) -> bool:
    """Use the protocol flow's continue_url instead of waiting on SPA routing."""
    if response is None:
        return False
    status = int(getattr(response, "status", 0) or 0)
    try:
        data = await response.json()
    except Exception:
        data = {}

    def find_continue_url(value, depth: int = 0) -> str:
        if depth > 3:
            return ""
        if isinstance(value, dict):
            preferred = (
                "continue_url", "continueUrl", "redirect_url", "redirectUrl", "url"
            )
            for key in preferred:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for child in value.values():
                candidate = find_continue_url(child, depth + 1)
                if candidate:
                    return candidate
        elif isinstance(value, list):
            for child in value:
                candidate = find_continue_url(child, depth + 1)
                if candidate:
                    return candidate
        return ""

    def find_page_type(value, depth: int = 0) -> str:
        if depth > 3:
            return ""
        if isinstance(value, dict):
            for key in ("page_type", "pageType", "type"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for child in value.values():
                candidate = find_page_type(child, depth + 1)
                if candidate:
                    return candidate
        elif isinstance(value, list):
            for child in value:
                candidate = find_page_type(child, depth + 1)
                if candidate:
                    return candidate
        return ""

    def route_for_log(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "-"
        try:
            parsed = urlparse(raw)
            if parsed.netloc:
                return f"{parsed.netloc}{parsed.path or '/'}"
            return parsed.path or "-"
        except Exception:
            return "-"

    continue_url = find_continue_url(data)
    page_type = find_page_type(data)
    try:
        location = str(
            (getattr(response, "headers", {}) or {}).get("location") or ""
        ).strip()
    except Exception:
        location = ""
    fields = sorted(str(key) for key in data) if isinstance(data, dict) else []
    if not continue_url:
        continue_url = location
    _log(
        f"[资料] create_account 响应诊断 · HTTP {status}"
        f" · page={page_type or '?'}"
        f" · continue={route_for_log(continue_url)}"
        f" · location={route_for_log(location)}"
        f" · fields={fields[:12]}"
    )
    identity_route = "verify-your-identity" in f"{continue_url} {location}".lower()
    if identity_route or "identity" in page_type.lower():
        _log("[资料] create_account 已由服务端分流到 Persona 身份核验")
    if not continue_url:
        _log(
            f"[资料] create_account 响应缺少跳转地址 · HTTP {status}"
            f" · fields={fields[:12]}"
        )
        if await _recover_nextauth_session(page):
            return True
        return False
    _log(f"[资料] 已捕获 create_account 回调 · HTTP {status}，等待授权跳转")

    callback_response = None
    if callback_future is not None:
        try:
            callback_response = await asyncio.wait_for(
                asyncio.shield(callback_future), timeout=8.0
            )
        except Exception:
            callback_response = None
    if callback_response is not None:
        status = int(getattr(callback_response, "status", 0) or 0)
        _log(f"[资料] 已观察到 OAuth callback 响应 · HTTP {status}")
        await wait_for_any(
            page,
            url_substrings=["chatgpt.com/"],
            selectors=['#prompt-textarea', 'textarea'],
            timeout=6,
            poll=0.25,
        )
        if await is_chatgpt_main(page):
            return True
        # The callback code is one-time. Never replay continue_url after the
        # browser already requested it; recover from its Set-Cookie instead.
        return await _recover_nextauth_session(page)

    await wait_for_any(
        page,
        url_substrings=["chatgpt.com/", "callback"],
        selectors=['#prompt-textarea', 'textarea'],
        timeout=8,
        poll=0.25,
    )
    if await _about_you_left(page):
        return True
    current_url = await _page_url(page)
    if _is_auth_error_url(current_url):
        if await _recover_nextauth_session(page):
            return True
        _log("[资料] OAuth 回调失败，交由任务重试重新登录")
        return False
    if not _is_about_you_url(current_url):
        return False

    # Only use the captured URL after the SPA had a full chance to consume it.
    # Navigating immediately races the page handler and reuses a one-time code.
    _log("[资料] SPA 未自动跳转，使用回调地址兜底一次")
    try:
        await page.goto(continue_url, timeout=15000, wait_until="commit")
    except Exception:
        pass
    await wait_for_any(
        page,
        url_substrings=["chatgpt.com/", "callback"],
        selectors=['#prompt-textarea', 'textarea'],
        timeout=6,
        poll=0.25,
    )
    current_url = await _page_url(page)
    if _is_auth_error_url(current_url):
        return await _recover_nextauth_session(page)
    return await _about_you_left(page)


async def confirm_birthdate_dialog(
    page,
    *,
    timeout: float = 4.0,
    response_future=None,
) -> bool:
    """Confirm the localized date-of-birth modal after about-you submit."""
    deadline = time.monotonic() + max(0.2, float(timeout))
    labels = (
        "OK", "Confirm", "確認", "确认", "確定", "确定", "はい",
        "Bestätigen", "Confirmer", "Confirmar", "Xác nhận", "확인",
    )
    while time.monotonic() < deadline:
        if response_future is not None and response_future.done():
            return False
        body = (await safe_body_text(page, limit=1600, timeout=1.0)).lower()
        has_birthdate = any(
            marker in body
            for marker in (
                "date of birth", "birthday", "生年月日", "出生日期", "出生年月日",
                "geburtsdatum", "date de naissance", "data de nascimento",
                "ngày sinh", "생년월일",
            )
        )
        has_confirmation = any(
            marker in body
            for marker in (
                "set", "record", "設定", "设置", "設定しています", "記録用",
                "bestät", "confirmer", "definir", "registro", "xác nhận", "설정",
            )
        )
        if has_birthdate and has_confirmation:
            if await click_any_exact(page, labels):
                _log("[资料] 已确认出生日期弹窗")
                await asyncio.sleep(0.2)
                return True
        await asyncio.sleep(0.15)
    return False


async def click_finish_button(page, index: int, age_sel: str, max_wait: int = 12) -> str:

    async def submission_busy() -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    page.evaluate(
                        """() => {
                            const b = document.querySelector('button[type="submit"]')
                                || document.querySelector('button.btn-primary');
                            if (!b) return false;
                            return b.disabled
                                || b.getAttribute('aria-disabled') === 'true'
                                || !(b.innerText || b.textContent || '').trim();
                        }"""
                    ),
                    timeout=1.2,
                )
            )
        except Exception:
            return False

    async def find_btn():
        try:
            cand = page.locator('button[type="submit"], button.btn-primary').filter(
                has_not_text="Google"
            ).filter(has_not_text="Apple").filter(has_not_text="Back")
            n = await asyncio.wait_for(cand.count(), timeout=0.8)
            for i in range(n):
                txt = (await asyncio.wait_for(
                    cand.nth(i).inner_text(), timeout=0.5
                )).strip().lower()
                if any(k in txt for k in ("finish", "create", "完成", "创建", "account")):
                    return cand.nth(i)
            if n == 1:
                return cand.first
        except Exception:
            pass
        return None

    deadline = time.time() + max_wait
    while time.time() < deadline:
        if await _about_you_left(page):
            return "complete"
        btn = await find_btn()
        if btn is None:
            await asyncio.sleep(0.4)
            continue
        try:
            disabled = await btn.get_attribute("disabled")
            aria_dis = await btn.get_attribute("aria-disabled")
        except Exception:
            disabled = aria_dis = None

        if disabled is not None or aria_dis == "true":
            try:
                ae = page.locator(age_sel).first
                if await asyncio.wait_for(ae.count(), timeout=0.8) > 0:
                    await ae.focus()
                    await ae.press("Tab")
                    await blur_field(page, age_sel)
            except Exception:
                pass
            await asyncio.sleep(0.4)
            continue

        try:
            await btn.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        # Capture the same create_account response used by the protocol flow.
        # Its continue_url is more reliable than waiting for the SPA router.
        response_future = asyncio.get_running_loop().create_future()
        callback_future = asyncio.get_running_loop().create_future()

        def capture_oauth_responses(response):
            try:
                url = str(response.url)
                if "/api/accounts/create_account" in url and not response_future.done():
                    response_future.set_result(response)
                elif "/api/auth/callback/openai" in url and not callback_future.done():
                    callback_future.set_result(response)
            except Exception:
                pass

        try:
            page.on("response", capture_oauth_responses)
        except Exception:
            pass

        def detach_capture() -> None:
            try:
                page.remove_listener("response", capture_oauth_responses)
            except Exception:
                pass
            if not response_future.done():
                response_future.cancel()
            if not callback_future.done():
                callback_future.cancel()

        # Schedule the click after evaluate returns. React submit handlers can
        # synchronously block long enough to make a direct b.click() evaluate
        # time out even though the form has already entered its loading state.
        clicked_ok = False
        try:
            await asyncio.wait_for(btn.evaluate(
                """(b) => {
                    setTimeout(() => {
                        if (!b.disabled && b.getAttribute('aria-disabled') !== 'true') {
                            b.click();
                        }
                    }, 0);
                    return true;
                }"""
            ), timeout=1.2)
            clicked_ok = True
            _log("[资料] 已触发完成创建按钮，等待服务端响应")
        except Exception as e:
            if response_future.done() or await submission_busy():
                clicked_ok = True
                _log("[资料] 完成按钮已进入提交中状态")
            else:
                _log(f"[资料] JS 触发失败: {str(e)[:80]}")
                try:
                    await btn.click(timeout=2500)
                    clicked_ok = True
                    _log("[资料] 已点击完成创建按钮")
                except Exception as e2:
                    if response_future.done() or await submission_busy():
                        clicked_ok = True
                        _log("[资料] 完成按钮已进入提交中状态")
                    else:
                        _log(f"[资料] 完成按钮点击失败: {str(e2)[:80]}")
                        detach_capture()
                        await asyncio.sleep(0.4)
                        continue

        if not clicked_ok:
            detach_capture()
            await asyncio.sleep(0.4)
            continue

        # The localized about-you flow may show a DOM confirmation modal
        # before it emits create_account. Keep the response listener attached
        # while acknowledging that modal.
        await confirm_birthdate_dialog(
            page,
            timeout=4.0,
            response_future=response_future,
        )

        captured_response = None
        try:
            captured_response = await asyncio.wait_for(
                asyncio.shield(response_future), timeout=30.0
            )
        except Exception:
            pass
        if captured_response is not None:
            followed = await follow_create_account_response(
                page, captured_response, callback_future
            )
            detach_capture()
            return "complete" if followed else "submitted"

        if await _about_you_left(page):
            detach_capture()
            _log("[资料] 已离开 about-you 页")
            return "complete"

        if await submission_busy():
            _log("[资料] 创建请求仍在处理中，继续等待（不会重复提交）")
            try:
                captured_response = await asyncio.wait_for(
                    asyncio.shield(response_future), timeout=25.0
                )
            except Exception:
                captured_response = None
            if captured_response is not None:
                followed = await follow_create_account_response(
                    page, captured_response, callback_future
                )
                detach_capture()
                return "complete" if followed else "submitted"
            if await _about_you_left(page):
                detach_capture()
                _log("[资料] 创建请求完成并已离开 about-you")
                return "complete"
            if await submission_busy():
                detach_capture()
                _log("[资料] 创建请求长时间处理中，本轮停止点击并保留会话等待")
                return "pending"

        _log("[资料] 未捕获 create_account 响应且按钮已恢复，下一轮再试")
        detach_capture()
        return "retry"

    shot = ROOT / "screenshots" / f"chatgpt_onboarding_stuck_{index}.png"
    try:
        await page.screenshot(path=str(shot))
        _log(f"[资料] 卡住截图: {shot.name}")
    except Exception:
        pass
    return ""
