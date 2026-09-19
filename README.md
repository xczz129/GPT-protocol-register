# GPT Register FP

ChatGPT 账号批量注册 / 管理 / 续期面板：纯 HTTP 协议引擎（curl_cffi 模拟 Chrome）+ 指纹浏览器双引擎，多渠道邮箱池与代理池，账号库、测活、重登、接码、Codex OAuth 授权与中转同步一体化。

## 功能总览

| 模块 | 能力 |
|------|------|
| 注册 | 协议模式（curl_cffi + PKCE + Sentinel/PoW）或指纹浏览器（Local Chrome / Camoufox / Obscura）；发现邮箱已注册时可"打野"接管已有账号（新密码 + TOTP 2FA） |
| 登录方式 | **密码 + TOTP 2FA**；**邮箱验证码登录（passwordless）**——无密码账号凭邮箱池收 OTP 完成重登与接码授权 |
| 邮箱池 | Outlook（Graph/IMAP）、iCloud Hide My Email、SMSBower 临时邮箱（主号 + `+split` 子号）、OnlineDisposableMail、Fastmail、Maddy |
| 接码 | SMSBower 手机验证：按国家×档位轮转取号、失败换号重试、会话失效重建、停止时号码取消确认 |
| 账号管理 | SQLite WAL 主库（约 7000 账号级）、批量测活（alive/limited/invalid/reauth_required）、协议重登、安全修复（改密/改 2FA）、邮箱换绑 |
| 授权与中转 | Codex OAuth 重授权拿 RT、批量接码任务、凭据同步 cliproxy 中转、chatgpt2api 上传 |
| 试用/支付 | Plus 免费试用资格检测（v4 / nerver 双渠道、直连/资格代理）、Kakao 提链工作台（KR checkout、专用代理池） |
| Cloudflare 对抗 | CSRF / authorize / 认证步骤全链路 clearance 处理：轻量算法恢复（抖动 / 域名预热 / 持久轮换 TLS 指纹）+ FlareSolverr 两轮升级 |
| 运维 | 双 UI（旧 Vue + 新 Vue3/TS）、备份恢复、任务断点续跑、操作审计日志、SSE 实时日志 |

## 架构

```text
gpt-register-fp/
  cli.py                 # CLI 入口（register / login / health / web）
  web_app.py             # FastAPI 主应用：JOB 任务系统、维护、关机协调
  web/routes/            # API 路由：账号 / 接码 / 重登 / 换绑 / 运维 / 中转
  web/static/vue/        # 前端构建产物（双入口：index.html 旧版 / new.html 新版）
  web-ui/                # 前端源码（Vue 3.5 + Vite 8 + TypeScript + Pinia）
  register/              # 注册/登录核心
    protocol_registrar.py  # PlatformRegistrar：协议登录/注册/改密/TOTP/OTP 全流程
    protocol.py            # register_one / 批量调度 / CF 冷却 / 打野编排
    protocol_ops.py        # 单账号操作：login_one / login_with_password_totp_one / login_email_code_one 等
    protocol_context.py    # contextvars、邮箱分配、验证码等待
    sentinel_vm/           # Node Sentinel 运行脚本
  codex_oauth/           # Codex OAuth 客户端、导入任务、配额、接码 SMSBower
  mail/                  # 邮箱池：多渠道分配 / 收码 / 边界标记 / 结算
  common/                # 账号库 account_store、测活 account_health、会话刷新、中转恢复、检查点
  sentinel/              # Codex OAuth Node runner 与 SDK
  local_browser.py       # Local Chrome / Camoufox / Obscura 浏览器引擎
  deploy/                # systemd 部署单元
  data/                  # 运行数据（不入 Git）
```

## 快速开始

### 依赖

- Python 3.10+（生产为 3.12）
- Node.js 22.12+（前端构建与 Sentinel 脚本运行时均需要 `node` 在 `PATH`）

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip check
```

### 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `mail.providers`（见下文邮箱池）、`proxy`、`fingerprint` 与 `web` 登录账号。

### 构建前端

```bash
cd web-ui
npm ci
npm run check
npm run build
```

### 启动

```bash
python3 cli.py web          # 面板 http://127.0.0.1:8787 （仅本机）
```

面板账号密码写在 `config.yaml`（密码留空时首启自动生成并打印）。登录后 HttpOnly Cookie 会话 7 天。局域网访问用 `--host 0.0.0.0` 并务必保管密码。

### 生产部署（systemd）

```bash
cp deploy/gpt-register-fp-8787.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gpt-register-fp-8787
```

服务绑定 `127.0.0.1:8787`，建议 Caddy/Nginx 反代并加 TLS。关机时自动协调停止注册 / 重登 / 接码 / 换绑等全部后台批次。

## 登录方式

### 密码 + TOTP（有凭据账号）

账号库中同时保存密码与 2FA 密钥的账号走标准凭据登录：`authorize/continue → password/verify → mfa → [add-phone 接码] → workspace → token`。

### 邮箱验证码登录（passwordless 账号）

无密码、无 2FA 的账号（接码注册、导入的 iCloud 号等），只要邮箱在邮箱池中有可收码绑定（`provider_type` + `mailbox_provider_ref`），自动改走验证码登录：

- **重新登录（NextAuth 流）**：`csrf → signin → authorize（302 email-verification，服务端自动发码）→ email-otp/validate → callback → 会话读取`。等码两轮共 180s、每 45s 一段无码时经 `email-otp/resend` 刷新 challenge；错码一次后刷新 challenge 等新码重试
- **接码授权（Codex OAuth 流）**：`authorize/continue 提交邮箱 → email-otp/validate → workspace/select → oauth2/auth 换 code`。收码 4 段×45s 重发；OTP 已自动发送时 409 容忍

两条路线均适用于：账号列表"重新登录"、批量重登、接码/中转任务。Team 工作区账号仍要求密码 + TOTP。

## 邮箱池

多个 provider 可同时启用；指定邮箱注册/登录时自动选中对应池。

### Outlook（`type: outlook_token`）

```text
user@outlook.com----邮箱密码----client_id----refresh_token
```

取码：Microsoft Graph 或 IMAP（推荐 `mode: imap`）。

### iCloud（`type: icloud_api`）

基于可配置的 Hide My Email 取码 API：

```text
mailbox@example.com----YOUR_ALIAS_KEY
```

| 接口 | 说明 |
|------|------|
| `GET /api/v1/health` | 健康检查 |
| `GET /api/v1/code?key=...` | 获取最新验证码 |
| `GET /api/v1/code/wait?key=...&timeout=60` | 长轮询等待 |
| `POST /api/v1/code` | JSON 长轮询（默认） |

```yaml
mail:
  providers:
    - type: icloud_api
      enable: true
      label: iCloud Pool
      api_base: https://mail-api.example.invalid
      mailboxes: |
        mailbox@example.com----YOUR_ALIAS_KEY
```

> API 服务端需在其「设置」里配置 iCloud 主邮箱 + App 专用密码。

### SMSBower 临时邮箱（`type: smsbower_mail`）

按需购买 Gmail / iCloud 临时邮箱，`domains` 顺序即尝试顺序：

```yaml
mail:
  providers:
    - type: smsbower_mail
      enable: false
      api_base: https://smsbower.page
      api_key: ""          # 或环境变量 SMSBOWER_MAIL_API_KEY
      service: dr
      register_child: true # 主号收码后注册 +split 子号
      domains: [gmail.com, icloud.com]
      max_price: ""
```

主子号说明：每组只购买一个邮箱，主号收码后继续持有激活，同一收件源的 `+split` 地址注册子号；两者取码结束后只结算一次。仅勾选子号时合计数量必须 ≥2 且为偶数。购买接口不自动重试，避免重复扣费。

### OnlineDisposableMail（`type: online_disposablemail`）

按需购买、`/api/latest/code` 轮询取码；订单无法客户端取消，超时由远端自动关闭。业务码 `42001` 继续轮询、`42002-42006` 终止等待；`data.code` 为空时从邮件 HTML 提取。详见 `config.example.yaml`。

### Fastmail（`type: fastmail_api`）

基于自建 mailcode-api 服务（主账号 + 服务端别名池）：`POST /api/v1/accounts/{account}/pool/acquire` 分配、`GET /api/v1/code?...` 服务端长轮询取码。当前部署永久保留已分配别名。

## 手机接码（SMSBower）

- `sms_code.max_phone_attempts`：`0` 不限次，正整数 = 每账号最多购买该数量号码
- 每个号码配一整轮完整登录；号码失败取消后重新登录走下一轮
- 认证返回 `signin_session_invalid` 时关闭旧客户端、退避后重建授权，优先复用仍有效且未发短信的号码；会话重建有次数上限（5 次），超限按失败收敛
- 停止任务会等已开始的购买/认证操作退出再回收号码；取消未获供应商确认时会告警并保留下次重试

## Cloudflare 清障

协议请求遇 CF 挑战（403 "Just a moment..."）时按以下策略恢复，全部失败才报"请更换 IP/代理"：

1. **轻量算法恢复**（不启动浏览器，三轮升级）：短抖动重试 → 长退避 + 域名根路径预热（刷新 `__cf_bm`）→ **持久轮换同家族 TLS 指纹**（JA4 变化，UA/Client Hints 同步重写，对后续请求保持生效）
2. **FlareSolverr**：轻量恢复后仍被拦时，强制 FlareSolverr 解 `cf_clearance`（设到 `.openai.com` / `auth.openai.com`）再重试
3. CSRF / 首页 / signin / authorize 的请求头在每次重试时按**当前指纹**重建，避免轮换指纹后仍携带旧 UA

批内 CF 连续拦截达到阈值时自动进入批次冷却（默认 300s），冷却期间暂停派发。

## 账号库与运维

| 路径 | 内容 |
|------|------|
| `data/registered_accounts.sqlite3` | 账号主库（WAL、单行 UPSERT、主键 `chatgpt_user_id` 优先防塌缩） |
| `data/task_history.sqlite3` | 任务历史 |
| `data/account_operation_logs.sqlite3` | 操作审计日志（测活/重登/注册/换绑） |
| `data/mail_registration_metrics.sqlite3` | 渠道注册统计（不含邮箱、验证码、密钥） |
| `cookies/` `tokens/` | 会话与凭据导出 |
| `data/backups/` | 自动备份（每日），面板可恢复；恢复后自动失效内存缓存并按 inode 重连日志 |

## 命令行

```bash
python3 cli.py init
python3 cli.py register -n 1
python3 cli.py register --engine protocol -n 5 -j 3
python3 cli.py register --email mailbox@example.com
python3 cli.py login --email existing@outlook.com --mail-type outlook
python3 cli.py login --all-needed -j 4   # 批量重登（成功后凭据写回账号库）
python3 cli.py health -j 5
python3 cli.py web
```

> 不要用不带子命令的 `python cli.py` 做启动检查——它默认执行注册。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

当前 2326 项测试全通过，覆盖：注册/登录流程、接码资金安全、账号塌缩回归、关机协调、邮箱池并发等。

## 安全与版本控制

`.gitignore` 默认排除本地敏感与运行态文件（`config.yaml`、`.env*`、数据库、Cookie、Token、日志、截图、备份）。`/api/config` 对密钥脱敏；`/api/accounts` 不返回原始密码与 token，完整导出走面板"下载 JSON"。

## 指纹浏览器

| 类型 | 费用 | 说明 |
|------|------|------|
| `local`（默认） | 免费 | 本机 Chrome/Chromium + Playwright stealth |
| `camoufox` | 免费 | 开源反检测 Firefox，先 `python3 -m camoufox fetch` |
| `obscura` | 免费 | 轻量 Rust headless + CDP，约 30MB 内存，二进制 `bin/obscura` |
