# -*- coding: utf-8 -*-
"""About-you / onboarding steps."""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.browser import react_fill
from register.log import _log
from register.util import rand_name
from register.page import (
    dump_state,
    safe_count,
    wait_for_any,
    is_chatgpt_main,
    click_any_exact,
    blur_field,
    _about_you_left,
    _force_react_value,
    click_finish_button,
    _page_url,
    _is_login_url,
    _is_about_you_url,
    _is_verify_url,
)


async def _bounded_count(locator) -> int:
    try:
        return int(await asyncio.wait_for(locator.count(), timeout=0.8))
    except Exception:
        return 0


async def is_identity_verification_page(page) -> bool:
    """Detect the Persona identity-verification gate shown after account creation."""
    try:
        url = str(getattr(page, "url", "") or "").lower()
    except Exception:
        url = ""
    if "verify-your-identity" in url or "identity-verification" in url:
        return True
    try:
        body = str(
            await page.locator("body").inner_text(timeout=1200)
            or ""
        ).lower()
    except Exception:
        body = ""
    return (
        "government id" in body
        or "additional information required" in body
        or ("secured with" in body and "persona" in body)
    )


async def handle_onboarding(page, index: int, max_rounds: int = 6) -> bool:
    # Already on main app — skip entire onboarding loop
    if await is_chatgpt_main(page):
        _log("  [资料] 已在 ChatGPT 主界面，跳过引导")
        return True

    if await is_identity_verification_page(page):
        _log("  [资料] 检测到 Persona 身份核验页，需要人工完成证件核验")
        return False

    name_done = False
    age_done = False
    bday_done = False
    for r in range(max_rounds):
        if await is_chatgpt_main(page):
            _log("  [资料] 已进入 ChatGPT 主界面")
            return True

        if await is_identity_verification_page(page):
            _log("  [资料] 已进入 Persona 身份核验页，停止重复点击")
            return False

        await asyncio.sleep(0.4)
        try:
            url = page.url.lower()
        except Exception:
            url = ""
        _log(f"  [资料] 第 {r + 1}/{max_rounds} 轮 · url={url[:80]}")

        name_sel = (
            'input[name="name"], input[placeholder*="name" i], '
            'input[placeholder*="全名"], input[placeholder*="姓名"], '
            'input[autocomplete="name"]'
        )
        age_sel = (
            'input[name="age"], input[type="number"], '
            'input[placeholder*="age" i], input[placeholder*="年齢"], '
            'input[placeholder*="年龄"]'
        )
        on_about_you = False
        try:
            on_about_you = (
                "about-you" in url
                or await _bounded_count(page.locator('input[name="age"]')) > 0
            )
        except Exception:
            on_about_you = False

        if on_about_you:
            if await _bounded_count(page.locator(name_sel)) > 0:
                first, last = rand_name()
                full = f"{first} {last}"
                if await _force_react_value(page, name_sel, full) or await react_fill(
                    page, name_sel, full, tries=2, delay=10, settle=0.12, verbose=False
                ):
                    if not name_done:
                        _log(f"  [资料] 姓名: {full}")
                    name_done = True
            age_val = str(random.randint(22, 35))  # nosemgrep: gitlab.bandit.B311
            if await _bounded_count(page.locator(age_sel)) > 0:
                if await _force_react_value(page, age_sel, age_val) or await react_fill(
                    page, age_sel, age_val, tries=2, delay=10, settle=0.12, verbose=False
                ):
                    if not age_done:
                        _log(f"  [资料] 年龄: {age_val}")
                    age_done = True
                try:
                    await page.locator(age_sel).first.press("Tab")
                except Exception:
                    pass
                await asyncio.sleep(0.2)

            finish_state = await click_finish_button(page, index, age_sel, max_wait=12)
            if finish_state:
                # Wait briefly for navigation after create_account
                await wait_for_any(
                    page,
                    url_substrings=["chatgpt.com/", "callback"],
                    selectors=['#prompt-textarea', 'textarea'],
                    timeout=10,
                )
                if await is_chatgpt_main(page) or await _about_you_left(page):
                    if await is_chatgpt_main(page):
                        _log("  [资料] 已进入 ChatGPT 主界面")
                        return True
                if finish_state == "submitted":
                    _log("  [资料] 账号资料已提交，但授权回调未完成；停止重复提交")
                    await dump_state(page, "资料回调未完成", verbose=True)
                    return False
                continue

        if not on_about_you and await _bounded_count(page.locator(name_sel)) > 0:
            first, last = rand_name()
            if await react_fill(
                page, name_sel, f"{first} {last}", tries=2, delay=10, settle=0.12, verbose=False
            ):
                _log(f"  [资料] 姓名: {first} {last}")
                await asyncio.sleep(0.3)

        bday = page.locator(
            'input[type="date"]:visible, '
            'input[name="birthday"]:not([type="hidden"]):visible, '
            'input[name="dob"]:visible, '
            'input[placeholder*="birth" i]:visible, '
            'input[placeholder*="生日"]:visible, '
            'input[placeholder*="出生"]:visible'
        )
        if not bday_done and not on_about_you and await _bounded_count(bday) > 0:
            filled = False
            try:
                first_bday = bday.first
                if await first_bday.get_attribute("type") == "date":
                    await first_bday.fill("1995-06-15")
                    filled = (await first_bday.input_value()).strip() != ""
            except Exception:
                pass
            if not filled:
                for v in ["1995-06-15", "06/15/1995"]:
                    if await react_fill(
                        page,
                        'input[type="date"]:visible, input[name="dob"]:visible',
                        v, tries=1, delay=8, settle=0.1, verbose=False,
                    ):
                        filled = True
                        break
            if filled:
                _log("  [资料] 生日已填写")
                bday_done = True
                await blur_field(
                    page, 'input[type="date"]:visible, input[name="dob"]:visible'
                )
                await asyncio.sleep(0.15)

        labels = [
            "Finish creating account", "アカウントの作成を完了する",
            "完成建立帳戶", "完成建立帳號", "完成創建帳戶", "完成創建帳號",
            "完成创建账户", "完成创建账号", "完成建立账户",
            "Selesaikan penciptaan akaun", "Selesaikan penciptaan",
            "Continue", "继续", "繼續", "Agree", "同意", "I agree",
            "Next", "下一步", "Get started", "开始", "Confirm", "确认", "確認",
            "Submit", "提交", "完成", "続行", "完了", "次へ", "同意する",
            "はい", "始める", "Teruskan", "Setuju", "Mula",
        ]
        clicked = await click_any_exact(page, labels)
        if clicked:
            _log("  [资料] 已点击引导按钮")
            await wait_for_any(
                page,
                url_substrings=["chatgpt.com/", "callback"],
                selectors=['#prompt-textarea'],
                timeout=6,
            )

        if not clicked and await _bounded_count(page.locator(age_sel)) > 0:
            try:
                await page.locator(age_sel).first.press("Enter")
                await asyncio.sleep(0.8)
            except Exception:
                pass
            try:
                btn = (
                    page.locator('button:not([disabled]):not([aria-disabled="true"])')
                    .filter(has_not_text="Google")
                    .filter(has_not_text="Apple")
                    .filter(has_not_text="Back")
                )
                if await _bounded_count(btn) >= 1:
                    await btn.first.click(timeout=5000)
                    _log("  [资料] 已点击提交按钮")
                    clicked = True
                    await asyncio.sleep(1)
            except Exception as e:
                _log(f"  [资料] 兜底提交失败: {str(e)[:60]}")

        if await is_chatgpt_main(page):
            _log("  [资料] 已进入 ChatGPT 主界面")
            return True
        try:
            url = page.url.lower()
        except Exception:
            url = ""
        if "chatgpt.com" in url and "auth" not in url and "onboarding" not in url and "about-you" not in url:
            _log("  [资料] 已到 chatgpt.com，结束引导")
            return True

        if (
            not clicked
            and not on_about_you
            and await _bounded_count(page.locator(name_sel)) == 0
            and await _bounded_count(bday) == 0
        ):
            break

    await dump_state(page, "资料结束", verbose=True)
    return False
