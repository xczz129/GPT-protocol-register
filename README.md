# GPT Register (Protocol + Fingerprint Browser)

合并 **reg-factory 指纹浏览器注册流** + **邮箱池（Outlook / iCloud / SMSBower）/ 代理池**，
**仅 ChatGPT 注册**。

本目录独立于 `~/outlook/chatgpt-register-sub2api`，不会修改你的原项目。

## 流程

1. 从邮箱池取号（Outlook、iCloud Hide My Email 或 SMSBower 临时邮箱）
2. 按 `registration.engine` 选择纯 HTTP 协议或指纹浏览器
3. 协议模式使用 `curl_cffi`、PKCE、Sentinel/PoW；浏览器模式使用 Playwright
4. 完成邮箱/密码与 6 位 OTP 验证
5. 完成 about-you onboarding
6. 保存 cookie / session token 到 `cookies/`、`tokens/`、`data/registered_accounts.sqlite3`

## 邮箱提供商

### Outlook（`type: outlook_token`）

```text
user@outlook.com----邮箱密码----client_id----refresh_token
```

取码：Microsoft Graph 或 IMAP（推荐 `mode: imap`）。

### iCloud（`type: icloud_api`）

基于可配置的 Hide My Email 取码 API。

```text
mailbox@example.com----YOUR_ALIAS_KEY
```

| 接口 | 说明 |
|------|------|
| `GET /api/v1/health` | 健康检查 |
| `GET /api/v1/code?key=...` | 获取最新验证码 |
| `GET /api/v1/code/wait?key=...&timeout=60` | 长轮询等待 |
| `POST /api/v1/code` | JSON 长轮询（本项目默认用这个） |

配置：

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

> 注意：API 服务端需在其「设置」里配置 iCloud 主邮箱 + App 专用密码，否则会返回
> `请先在设置里填写 iCloud 邮箱和 App 专用密码`。

### SMSBower 临时邮箱（`type: smsbower_mail`）

SMSBower 可按需购买 Gmail 或 iCloud 临时邮箱。`domains` 的排列顺序就是尝试顺序，
因此可以只选一种，也可以同时选择并决定优先使用哪一种。示例默认关闭，且不包含真实密钥：

```yaml
mail:
  providers:
    - type: smsbower_mail
      enable: false
      label: SMSBower Mail
      api_base: https://smsbower.page
      # 可直接填写，或留空并设置进程环境变量 SMSBOWER_MAIL_API_KEY
      api_key: ""
      service: dr
      # 主号成功后再注册一个共享收件源的 +split 子号
      register_child: true
      domains:
        - gmail.com
        - icloud.com
      # 单个邮箱最高价；留空表示不限价
      max_price: ""
```

面板邮箱池支持启用、选择 Gmail/iCloud、调整优先顺序和设置最高价格。购买接口不会自动
重试；响应不确定或终态回收失败时会阻止继续购买，避免重复扣费。API 密钥不会出现在
`/api/config` 响应或邮箱状态文件中。`mail.smsbower_wait_timeout` 默认且最低为 180 秒，
不会受其他邮箱池默认 90 秒等待时间影响。

该渠道先走密码注册分支；确认进入已有账号的登录、密码重置或 MFA 分支时，将邮箱记为
已注册并跳过，不登录原账号、不重置密码，也不继续读取其验证码。未知认证分支会报错，
不会被误记为“已注册”。

邮箱池中的“注册子号”默认开启。每组只购买一个邮箱，先完成主号，再使用同一收件源的
`+split` 地址注册子号；主号未完成则子号不启动。两者共享一个激活记录，结束后只结算一次。
选择 SMSBower 时并发至少为 2 且必须是偶数；开启子号时，总数表示主子号合计的账号名额，
也必须是至少 2 的偶数。每组占两个调度名额，多组可以同时进行，组内顺序取码避免串码。
关闭子号时仅注册主号。停止任务会等待正在执行的购买或认证操作退出，再回收邮箱。

运维页显示该渠道的尝试数、已注册、未收到验证码、成功、其他失败、取消及进行中数量，
并支持主号/子号及 Gmail/iCloud 分组。比例以实际开始的账号尝试为分母，未启动子号不计入。
验证码平均、最短、最长耗时按“开始等待到收到有效格式验证码”计算；超时单列，不进入均值。
统计从启用此功能后的任务开始积累，存储于 `data/mail_registration_metrics.sqlite3`，
不会保存邮箱地址、验证码或密钥，重启与备份恢复均保留统计。

### Fastmail（`type: fastmail_api`）

基于自建 **mailcode-api** 服务（Fastmail 主账号 + 服务端别名池）。服务端负责
分配别名（acquire）和取码（服务端长轮询）；当前部署永久保留已分配别名，不自动回收。

```yaml
mail:
  providers:
    - type: fastmail_api
      enable: true
      label: Fastmail Pool
      # mailcode-api 服务地址（http://服务器IP:8686）
      api_base: http://127.0.0.1:8686
      # 管理密钥（X-API-Key），或留空用环境变量 FASTMAIL_ADMIN_KEY
      admin_key: ""
      # Fastmail 主账号，每行一个（密码保存在 mailcode-api 服务内）
      accounts: |
        mailbox-owner@example.com
      # 服务不可达时的容量回退值
      capacity: 615
```

- 取号：`POST /api/v1/accounts/{account}/pool/acquire`（按可用数选主账号，同可用数轮转）
- 取码：`GET /api/v1/code?account=&email=&key=&wait=60`（服务端长轮询，客户端分块保持可停止）
- 面板 #mail 页可编辑配置、刷新池状态（`/api/fastmail/accounts`）、查看别名收件箱
- 别名：`acquire` 即在服务端标记已用；当前部署不会自动删除或停用已分配别名；
  本地验证码超时只释放本地预约，不会删除服务端别名

多个 provider 可同时启用；指定邮箱注册时会自动选中对应池。

## 依赖

- Python 3.10+
- Node.js：前端锁文件中的 Vite / Vue 插件要求 `^20.19.0 || >=22.12.0`；可使用 Node 22.12+。协议登录的 Sentinel 脚本运行时也需要 `node` 在 `PATH` 中。
- **免费默认**：本机 Chrome/Chromium + Playwright stealth（`fingerprint.browser: local`）
- **免费增强**：Camoufox 开源反检测 Firefox（`camoufox`）
- **免费轻量**：Obscura Rust 无头浏览器 + CDP（`obscura`，内存约 30MB 级）

```bash
# 在克隆后的项目根目录执行（以下为 Linux / macOS 命令）
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
node --version
```

### 指纹浏览器怎么选（免费优先）

| 类型 | 费用 | 说明 |
|------|------|------|
| **`local`**（默认） | 免费 | 本机 Chrome/Chromium + stealth，无需账号 |
| **`camoufox`** | 免费 | 开源反检测 Firefox，需先 `python3 -m camoufox fetch` |
| **`obscura`** | 免费 | 轻量 Rust headless + CDP，约 30MB 级内存；二进制在 `bin/obscura` |

### Camoufox 指纹参数

面板「指纹」页可直接调整以下参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| Headless | 开 | 无头运行，适合服务器批量任务 |
| OS | Windows | 可选 Windows / macOS / Linux / 随机桌面系统 |
| Locale | 自动 | 留空时按出口 GeoIP 同步语言、时区和地理位置 |
| GeoIP | 开 | 同步出口地区并伪装 WebRTC 出口 IP；失败自动阻断 WebRTC 并回退 locale |
| Humanize | `0.15` | 鼠标移动最大延迟秒数；设为 `0` 可优先吞吐量 |

首次启用 GeoIP 可能下载约 66 MB 的本地数据库，之后直接复用缓存。

## 配置

```bash
cp config.example.yaml config.yaml
# 编辑 mail.providers 与 proxy / fingerprint
```

## 从源码构建前端

首次克隆或更新前端源码后，先安装锁定依赖、检查并构建，再启动面板：

```bash
cd web-ui
npm ci
npm run check
npm run build
cd ..
```

`web-ui/vite.config.js` 同时构建两套 Vue 入口：

| 源码入口 | 构建产物 / 访问路径 |
|------|------|
| `web-ui/index.html` → `src/main.js`（旧界面） | `web/static/vue/index.html` / `/static/vue/index.html` |
| `web-ui/new.html` → `src/new/main.ts`（新界面） | `web/static/vue/new.html` / `/static/vue/new.html` |

构建会清空并重新生成 `web/static/vue/`，包括两套入口共用的 `assets/`。HTML 和完整资源一并纳入 Git 同步，避免克隆后入口引用的 JS/CSS 缺失；修改前端源码后仍需重新构建并提交本次完整产物。不要在正被访问的生产静态目录内直接构建，先在独立目录构建并验证后再发布。

源码发布必须包含 `web-ui/` 的两份 HTML、全部 `src/`（含 `src/new/`）、`package.json`、`package-lock.json`、`vite.config.js`、`tsconfig.json`，以及 `web/static/` 中非构建生成的资源。协议流程还直接依赖 `register/sentinel_vm/runtime_worker.js`、`sentinel/sentinel-runner.js`、`sentinel/sdk.js`；这些 JS 是运行源码，不能作为缓存忽略。

静态检查可运行 `python cli.py --help` 和以下命令，它们不会启动账号任务：

```bash
node --check register/sentinel_vm/runtime_worker.js
node --check sentinel/sentinel-runner.js
```

不要用不带子命令的 `python cli.py` 做启动检查，它默认执行注册。主面板按下面的 `web` 子命令启动后，可访问 `/api/health` 检查 HTTP 服务，访问 `/login` 检查页面资源。

## 运行

### 网页面板（推荐）

```bash
python3 cli.py web
# 默认 http://127.0.0.1:8787 （仅本机）
# 打开 /login 用账号密码登录
```

面板默认绑定 `127.0.0.1`，访问 `/login` 登录。
账号密码写在 `config.yaml`：

```yaml
web:
  username: admin
  password: your-password
```

密码留空时首次启动会自动生成并打印到终端。登录后使用 HttpOnly Cookie 会话（7 天）。
若需局域网访问：`python3 cli.py web --host 0.0.0.0`，并务必保管密码。


面板功能：

- 账号密码登录，HttpOnly Cookie 会话（7 天）
- 配置 API 默认脱敏返回，保存配置时保留已保存密钥
- 账号列表默认隐藏密码/Token；下载 JSON 才包含完整导出字段
- 启动 / 停止注册任务
- Outlook + iCloud + 域名邮箱 + Maddy + Fastmail + SMSBower 六套邮箱池分别编辑
- 配置 Local Chrome / Camoufox / Obscura
- 代理池、SSE 实时日志、已注册账号列表
- 侧边栏 Kakao 提链工作台：按账号/全部账号批量执行、每账号重试次数、后台日志与结果
- 提链方式可选择 `Kakao 提链` 或 `0元资格测试`；资格测试使用 KR checkout、TR 活动更新、KR Stripe 核验，不创建 Kakao PaymentMethod，也不执行 approve/redirect
- 支付链接显示生成时间与倒计时；优先读取链接参数中的到期时间，否则按 `KAKAO_LINK_TTL_SECONDS`（默认 3600 秒，即 1 小时）显示预计剩余时间
- 项目内置 Kakao 引擎和专用代理池编辑（不启动目录外脚本）

### 命令行

```bash
python3 cli.py init
python3 cli.py register -n 1
python3 cli.py register --engine protocol -n 5 -j 3
python3 cli.py login --email existing@outlook.com --mail-type outlook
python3 cli.py login --all-needed -j 4
python3 cli.py register -n 5 -j 2
python3 cli.py register --email mailbox@example.com
python3 cli.py health -j 5
```

## 输出

| 路径 | 内容 |
|------|------|
| `cookies/chatgpt/` | 全量 cookie + `accounts.txt` |
| `tokens/chatgpt/` | `*.session.json` + CPA 风格 auth json |
| `data/registered_accounts.sqlite3` | 账号实时主库（SQLite WAL、单行 UPSERT、按 email/account_id 去重；首次启动自动迁移旧 JSON） |
| `data/registered_accounts.json` | 旧数据迁移源与 JSON 兼容格式；面板下载和备份从实时主库按需导出完整数据 |
| `data/task_history.sqlite3` | 面板任务历史（SQLite WAL；首次读取时自动迁移旧 `task_history.json`） |
| `data/outlook_token_state.json` | 邮箱池状态（包括 SMSBower 待结算状态，不含其 API 密钥） |
| `data/mail_registration_metrics.sqlite3` | 渠道注册结果及取码耗时统计（不含邮箱、验证码或密钥） |
| `data/kakao_proxy_pools.json` | 按国家拆分的 Kakao sticky Seed 代理池（面板可新增国家并独立编辑） |
| `data/kakao_proxy_state.json` | Kakao 代理成功、冷却和移除状态 |
| `common/kakao_trial.py` | 免费 0 KRW 试用活动配置、Checkout/update Payload 与金额校验 |
| `screenshots/` | 失败截图 |
| `data/obscura_logs/` | Obscura 进程日志（选 obscura 时） |


## 安全与版本控制

Git 保存源码、依赖锁文件、脱敏配置样例和配套的前端 HTML/静态资源。`config.yaml`、`.env*`、账号数据库及 WAL/SHM、Cookie、Token、邮箱与代理状态、任务检查点、日志、截图和备份均属于本地运行数据，不得加入 Git；更换服务器时通过单独的数据备份恢复。前端 `node_modules/` 不提交，依赖使用上面的锁文件安装命令恢复。

`.gitignore` 默认排除本地敏感与运行态文件：

- `config.yaml` / `.env*`
- `cookies/` / `tokens/` / `screenshots/`
- `data/registered_accounts.sqlite3*` / `data/registered_accounts.json` / `data/outlook_token_state.json`
- `data/logs/` / `data/obscura_logs/` / `*.log` / `*.pid` / `*.lock`

Web 面板的 `/api/config` 会对 chatgpt2api key、Web session token、Web 密码做脱敏；`/api/accounts` 不返回原始密码、access token 或 session token。需要完整账号数据时使用面板的“下载 JSON”。

## 目录结构

```text
gpt-register-fp/
  cli.py              # 入口（register / init / web）
  web_app.py          # FastAPI 本地面板（账号密码登录 + HttpOnly Cookie 会话）
  web/static/         # 前端 HTML/CSS/JS
  web-ui/             # 旧 Vue 与新 Vue/TS 源码、锁文件、双入口构建配置
  register_gpt.py     # 兼容入口 → register/
  register/           # 注册流程拆分（flow / page / onboarding / batch）
  register/sentinel_vm/ # Node 协议运行脚本
  sentinel/           # Codex OAuth Node runner 与 SDK
  local_browser.py     # Local Chrome / Camoufox / Obscura
  mail/               # Outlook / iCloud / SMSBower 邮箱池与取码
  common/
  config.yaml / config.example.yaml
```
