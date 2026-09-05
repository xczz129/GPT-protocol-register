from __future__ import annotations
import argparse, secrets, webbrowser, getpass, logging, subprocess, sys
from pathlib import Path
from .oauth import create_pkce, authorization_url, wait_for_callback, exchange_code, save_token, ProtocolClient, totp_from_secret
from .import_jobs import _phone_requirement

def main() -> None:
    p = argparse.ArgumentParser(description="Codex OAuth PKCE helper")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--output", type=Path, default=None,
                   help="自定义 Token 输出路径；默认按邮箱保存为 data/<邮箱>.json")
    p.add_argument("--email")
    p.add_argument("--account-file", type=Path,
                   help="读取一行‘邮箱----密码----2FA密钥’的本地文本文件")
    p.add_argument("--account-index", type=int, default=None, help=argparse.SUPPRESS)
    args = p.parse_args()
    if not args.account_file and not args.email and Path("accounts.txt").exists():
        args.account_file = Path("accounts.txt")
    account_line = None
    if args.account_file:
        account_lines = [raw.strip() for raw in args.account_file.read_text(encoding="utf-8").splitlines()
                         if raw.strip() and not raw.strip().startswith("#")]
        if args.account_index is None and len(account_lines) > 1:
            print(f"检测到 {len(account_lines)} 个账号，开始批量处理（逐个执行，手机号/短信按账号输入）。")
            failures = 0
            for index in range(len(account_lines)):
                print(f"\n===== 账号 {index + 1}/{len(account_lines)} =====")
                result = subprocess.run([
                    sys.executable, "-m", "codex_oauth.cli",
                    "--account-file", str(args.account_file),
                    "--account-index", str(index),
                ])
                if result.returncode != 0:
                    failures += 1
                    print(f"账号 {index + 1} 失败，继续处理下一个。")
            print(f"批量处理完成：总数 {len(account_lines)}，失败 {failures}。")
            raise SystemExit(1 if failures else 0)
        if not account_lines:
            raise SystemExit("账号文件中没有可用数据行")
        if args.account_index is not None:
            if args.account_index < 0 or args.account_index >= len(account_lines):
                raise SystemExit("账号索引超出范围")
            account_line = account_lines[args.account_index]
        else:
            account_line = account_lines[0]
        parts = account_line.split("----", 2)
        if len(parts) != 3 or not all(parts):
            raise SystemExit("账号行格式错误，应为：邮箱----密码----2FA_BASE32密钥")
        args.email, file_password, file_mfa = [x.strip() for x in parts]
    else:
        file_password = file_mfa = None
    if args.email:
        def output_for_email(email: str) -> Path:
            # 邮箱通常可直接作为文件名；仅替换路径分隔符和 Windows 保留字符。
            safe = "".join("_" if c in '/\\:*?\"<>|' else c for c in email.strip())
            return Path("data") / f"{safe}.json"
        output_path = args.output or output_for_email(args.email)
        Path("data").mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename="data/oauth.log", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        def record(message: str):
            print(message)
            logging.info(message)
        client = ProtocolClient(args.email)
        record("[1/8] 建立 HTTP 会话并初始化 PKCE...")
        record("[2/8] 提交邮箱...")
        client.bootstrap(); client.submit_email()
        record("[2/8] 邮箱提交成功")
        password = file_password or getpass.getpass("Password: ")
        if file_password:
            record("[3/8] 已从账号文件读取密码（内容不显示）")
        record("[3/8] 提交密码...")
        client.verify_password(password)
        record("[3/8] 密码验证成功")
        record("[4/8] 解析 MFA 因子并申请 challenge...")
        mfa_id = client.mfa_id(); client.issue_mfa_challenge(mfa_id)
        record("[4/8] MFA challenge 已申请")
        while True:
            mfa_input = file_mfa or getpass.getpass("2FA 当前 6 位码或 Base32 密钥: ").strip()
            file_mfa = None
            try:
                mfa_code = mfa_input if mfa_input.isdigit() and len(mfa_input) == 6 else totp_from_secret(mfa_input)
            except ValueError as exc:
                print(f"2FA 密钥格式错误: {exc}")
                continue
            if mfa_code != mfa_input:
                record("[5/8] 已从 Base32 密钥生成当前 TOTP")
            record("[5/8] 提交 2FA...")
            try:
                mfa_response = client.verify_mfa(mfa_id, mfa_code)
                record("[5/8] 2FA 验证成功")
                break
            except Exception as exc:
                record(f"2FA 验证失败: {exc}")
                record("请确认使用认证器当前显示的 6 位动态码（不要输入恢复码或旧码）。")
                mfa_id = client.mfa_id()
                client.issue_mfa_challenge(mfa_id)
                record("已重新申请 MFA challenge，请再次输入。")
        pending_phone = None
        # 已绑定手机号的账号会在 MFA 后直接进入 Codex consent，不再需要 add-phone。
        try:
            mfa_data = mfa_response.json()
        except Exception:
            mfa_data = {}
        phone_required, phone_reason = _phone_requirement(mfa_data)
        if phone_required is False:
            record(f"[6/8] 当前账号无需手机号，跳过接码（{phone_reason}），直接选择 workspace...")
            callback = client.select_workspace()
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(callback).query)
            if not q.get("code", [""])[0]:
                raise RuntimeError("workspace callback 缺少 code")
            from .oauth import exchange_code
            token = exchange_code(q["code"][0], client.verifier)
            record("[8/8] Token 获取成功")
            save_token(token, output_path); record(f"Token saved to {output_path}"); return
        if phone_required is None:
            record("[6/8] MFA 未返回明确手机号步骤，先尝试直接选择 workspace...")
            try:
                callback = client.select_workspace()
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(callback).query)
                if q.get("code", [""])[0]:
                    from .oauth import exchange_code
                    token = exchange_code(q["code"][0], client.verifier)
                    record("[8/8] Token 获取成功")
                    save_token(token, output_path); record(f"Token saved to {output_path}"); return
            except Exception as exc:
                if "invalid_auth_step" not in str(exc).lower() and "add-phone" not in str(exc).lower():
                    raise
                record("workspace 返回手机号步骤，继续手机号流程")
        record(f"[6/8] 服务端要求手机号（{phone_reason}）")
        record("[6/8] 当前 CLI 未配置自动接码，将等待手动输入手机号和短信验证码")
        while True:
            phone = (pending_phone or input("手机号（+国家码...，失败可直接换一个）: ").strip())
            pending_phone = None
            try:
                record(f"[6/8] 发送手机验证码（{phone}）...")
                client.send_phone(phone)
                record("[6/8] 短信已请求，等待你输入验证码")
                phone_code = input("SMS verification code: ").strip()
                record("[7/8] 提交手机验证码...")
                client.submit_phone_otp(phone_code)
                record("[7/8] 手机验证成功，选择 workspace...")
                callback = client.select_workspace()
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(callback).query)
                if q.get("state", [""])[0] and q["state"][0] != client.state:
                    raise RuntimeError("callback state mismatch")
                from .oauth import exchange_code
                token = exchange_code(q["code"][0], client.verifier)
                record("[8/8] Token 获取成功")
                break
            except Exception as exc:
                error_text = str(exc)
                # 账号已经完成手机号步骤时，重新登录会话可能直接拒绝 add-phone。
                # 此时跳过手机号，尝试从当前认证状态选择 workspace 并换 token。
                if "invalid_auth_step" in error_text and "/add-phone/send" in error_text:
                    record("服务端表示当前会话已不在手机号步骤，尝试直接选择 workspace...")
                    try:
                        callback = client.select_workspace()
                        from urllib.parse import parse_qs, urlparse
                        q = parse_qs(urlparse(callback).query)
                        if not q.get("code", [""])[0]:
                            raise RuntimeError("workspace callback 缺少 code")
                        if q.get("state", [""])[0] and q["state"][0] != client.state:
                            raise RuntimeError("callback state mismatch")
                        from .oauth import exchange_code
                        token = exchange_code(q["code"][0], client.verifier)
                        record("[8/8] 已跳过手机号步骤并成功获取 Token")
                        break
                    except Exception as resume_exc:
                        record(f"直接恢复授权失败: {resume_exc}")
                else:
                    record(f"手机号验证失败: {exc}")
                answer = input("换一个手机号继续？[Y/n]（也可直接粘贴下一个手机号）: ").strip()
                if answer.lower() == "n":
                    raise
                # 兼容直接把下一个号码粘贴到确认提示的情况，避免号码被当成 Y/N 丢掉。
                if answer.startswith("+") or (answer.isdigit() and len(answer) >= 8):
                    pending_phone = answer
                record("保留当前邮箱、密码和 2FA 会话，只重新输入手机号。")
        save_token(token, output_path); record(f"Token saved to {output_path}"); return
    state = secrets.token_urlsafe(32); verifier, challenge = create_pkce()
    url = authorization_url(state, challenge)
    print("Open this URL to authorize:\n\n" + url)
    if not args.no_browser: webbrowser.open(url)
    print("\nWaiting for callback on http://127.0.0.1:1455/auth/callback ...")
    token = exchange_code(wait_for_callback(state, args.timeout), verifier)
    save_token(token, args.output or Path("data/token.json"))
    print(f"Token saved to {args.output or Path('data/token.json')}")

if __name__ == "__main__": main()
