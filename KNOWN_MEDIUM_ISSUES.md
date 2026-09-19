# 代码审查：中危问题清单（已全部处理）

> 2026-09-19 全量代码审查产出。高危 7 项已全部修复（见提交 `6ae2831`）。
> **2026-09-19 深夜更新：中危 19 项已全部处理**，全量测试 2326 passed 后重启生效。
>
> 处理方式：
> - **已修复（补丁落地）**：#1 接码进度 casefold、#2 max_price 校验、#3 activation_id 持久化+重启补 cancel、
>   #4 收码边界 fail-closed+毫秒、#5 ODM datetime、#7 settlement 显式清理入口（reset-failed?include_pending_settlements=true）、
>   #8 session_refresh 乐观锁、#9 update_account_records 身份守卫（user_id 回填迁移同 fields 版规则）、
>   #10 测活口径（写入缺 AT→reauth_required；读时对带 token 拒绝特征的缺 AT invalid 归一）、
>   #12/#13 OTP 重发边界与错码刷新、#14 备份/配置挡全部批次（any_batch_running）、#15 cliproxy 线程池、
>   #16 自有邮箱失败释放、#17 PROGRESS 看门狗（30 分钟）、#18 私有 API 带降级、
>   #19 全部子项：a 停止补 stats、b IN_USE_STALE_SECONDS 15→30 分钟、c 取消不计失败、d 文案 4 段、
>   e 死代码删除、f 缺省 runtime deadline、g _epoch 按 UTC
> - **已消解（核实无需改代码）**：#6 release_all_in_use 无生产调用方（仅测试引用）；
>   #11 _mark_relay_synced 现状已带 expected_fields CAS（refresh_token）
>
> 各项详情保留在下方作历史参考；行号对应当时版本，以函数/描述定位为准。

## 一、资金与操作（建议优先修）

### 1. 接码进度按大小写丢失
- **位置**：`web/routes/codex_oauth.py`（`_process_one` 使用原始大小写 email，`_set_sms_item` 按 `==` 精确匹配 `_STATE["items"]` 中的小写 email）
- **问题**：账号库 email 含大写（导入行原样保留）时，运行中的 stage / attempt / wait_deadline（限流倒计时、取号进度）更新静默丢弃，仅终态由外层小写 email 写入成功；`_sms_counts_locked` 与 `_STATE["done"]` 计数不一致
- **附带**：中转文件名含大小写 email，与其他组件的小写文件名可产生双文件
- **修复建议**：`_set_sms_item` 匹配改 casefold，或批次入口统一小写

### 2. `max_price` 保存时不校验
- **位置**：`web/routes/codex_oauth.py`（`sms_config_update` 存盘不校验；运行时 `float(p)` 未捕获）
- **问题**：通过 API 直接存入非法值（如 `"abc"`，绕过前端校验）后，每次取号 `acquire()` 抛 ValueError，**所有账号** acquire_failed
- **修复建议**：保存时校验可解析为正浮点数

### 3. 进程重启丢失在租号码台账
- **位置**：`codex_oauth/import_jobs.py`（重启只把 job 标 failed，activation 仅内存持有）
- **问题**：已购号码无法补取消，只能等供应商侧过期；是否退款取决于 SMSBower 对未收码过期的策略
- **修复建议**：activation_id 持久化，启动时补 cancel

### 4. 验证码边界判定 fail-open
- **位置**：`mail/__init__.py`（`_entry_available` / boundary 检查，`received_at` 缺失或非 datetime 时一律判"不旧"；`_parse_received_at` 把毫秒 epoch 当秒，`fromtimestamp` 溢出返回 None 全部放行）
- **问题**：mailcode-api / iCloud 网关响应缺时间戳或返回毫秒时间戳时，重发后可能提交旧验证码 → wrong_email_otp_code，浪费 validate 次数
- **修复建议**：`received_at` 缺失按旧码处理（fail-closed）；epoch > 1e12 按毫秒换算

### 5. ODM `received_at` 类型不符
- **位置**：`mail/__init__.py`（`fetch_recent_messages` 构造字符串类型 `received_at`，消费方要求 datetime）
- **问题**：通用路径上永远绕过 boundary 校验；当前主等待路径不经过此函数，属埋雷
- **修复建议**：构造时转为 datetime

### 6. `release_all_in_use(include_failed=True)` 无条件清空
- **位置**：`mail/__init__.py`
- **问题**：多批次 / 多进程共享邮箱池状态文件时，停止批次 A 会释放批次 B 正在使用的邮箱 → 双分配
- **修复建议**：按批次标识过滤，仅释放本批预约

### 7. SMSBower pending settlement 永久占用
- **位置**：`mail/__init__.py`（`_has_pending_smsbower_settlement` 条目被 `release_all_in_use` / `release_failed_mailboxes` / `release_stale_in_use` 全部跳过，仅靠同 provider 下一次购买触发 `_recover_pending_settlements`）
- **问题**：该 provider 被禁用或删配置后，条目永久 in_use，池容量永久缩水
- **修复建议**：面板释放操作对 pending settlement 提供显式清理入口

## 二、数据一致性

### 8. `session_refresh` 写回无乐观锁
- **位置**：`common/session_refresh.py`（两处 `update_account_record_fields` 不带 `expected_fields`）
- **问题**：批次快照与写回之间若发生邮箱换绑（已提交新凭据），旧 session 刷出的 token 会覆盖新凭据并强制 `health_status="alive"`，换绑成果丢失
- **修复建议**：传期望值（原 access_token / session_token / user_id），过期即跳过

### 9. `update_account_records` 无身份/占用防护
- **位置**：`common/account_store.py`（与 `update_account_record_fields` 不对称：fields 版有 prepare_update 守卫，records 版没有）
- **问题**：updates 含身份字段时键漂移 → 老行残留 + 目标键他行被整 payload 覆盖。当前调用方仅传元数据，属暴露的坑
- **修复建议**：复用 fields 版的守卫逻辑

### 10. 测活分类口径不一致
- **位置**：`common/account_health.py`（缺 AT 的 security_pending 账号归为 `invalid` 而非 `reauth_required`；SQLite `health_status` 列 / public_payload 冻结写入时归一值，读时 `normalized_health_status` 再映射）
- **问题**：token 过期后两套统计口径长期打架
- **修复建议**：缺 AT → reauth_required；读时统一重归一

### 11. `_mark_relay_synced` 无 CAS 盲写
- **位置**：`web/routes/codex_oauth.py`（不带 `expected_fields`，对比 `_persist_sms_tokens` 带 CAS）
- **问题**：`_persist_sms_tokens` 成功后、`_mark_relay_synced` 执行前，并发更新（如 session_refresh）会把新记录的 `relay_sync_pending` 误清，下次运行放弃 sync_only 走重新授权（多花登录/接码）。窗口小
- **修复建议**：带 `expected_fields`（写回时的 refresh_token）

## 三、流程健壮性

### 12. 注册 OTP 重发结果被忽略 + 无条件推进收码边界
- **位置**：`register/protocol_registrar.py`（`_wait_registration_otp_rounds`：`_resend_email_otp` 返回 bool 从不抛 RuntimeError，`except RuntimeError` 为死代码；无论重发成败都 `mark_mailbox_code_boundary(mailbox, grace_seconds=0)`）
- **问题**：与登录侧（`_wait_nextauth_login_otp`，仅重发成功才推进）不一致；resend 实际失败时可能作废已送达但未被轮询到的有效验证码 → 整单以"等待注册验证码超时"终态失败
- **修复建议**：改为 `if self._resend_email_otp(index): mark_mailbox_code_boundary(...)`

### 13. 补密码 / 密码重置的错码重试不刷新 challenge
- **位置**：`register/protocol_registrar.py`（`_complete_post_registration_password`、`_complete_loggedin_password_reset`，错码后仅 `continue` 等新邮件）
- **问题**：错码消耗 challenge 的 max_check_attempts，不刷新则大概率等不到新码 → 90s 超时 → 事务重试浪费（`register()` 主流程已有正确处理，这两处未对齐）
- **修复建议**：错码后先 `_resend_email_otp` 刷新 challenge 再等新码

### 14. 恢复备份 / 保存配置只挡注册任务
- **位置**：`web/routes/ops.py`、`web_app.py`（仅检查 `JOB.running`，不检查 reauth / rebind / trial / health 批次）
- **问题**：批次运行中恢复备份 → SQLite 在活跃写事务下被替换，worker 用陈旧快照 upsert，可能"复活"已删账号或损坏库
- **修复建议**：统一走运行中批次检查

### 15. cliproxy 列表 / 下载在事件循环做同步 I/O
- **位置**：`web/routes/cliproxy.py`（`list_accounts` 逐个 `_read_auth` + `stat()`；`download_auths` 同步打 zip；同文件其他端点均用 `to_thread`）
- **问题**：auths 数量多时阻塞整个面板（SSE 心跳、其他请求停顿）
- **修复建议**：套 `run_in_threadpool`

### 16. passwordless 重登失败污染账号自有邮箱状态
- **位置**：`register/protocol_ops.py`（`login_email_code_one` 失败即 `mark_mailbox_result(success=False)`，把账号自有邮箱从 used 改标 failed）
- **问题**：非瞬态错误（OTP 校验失败、no_code）下，同批代理重试的 `create_mailbox(allow_used=True)` 找不到可用邮箱 → 重试自毁；面板"释放 failed"后该地址还可能被新注册复用造成身份冲突
- **修复建议**：固定地址（已有账号）的失败改用 `release_mailbox` 或跳过状态标记

### 17. PROGRESS 运行项永不淘汰
- **位置**：`web_app.py`（`OperationProgressState.snapshot()` 对 running 项无超时淘汰）
- **问题**：请求任务被取消时 finish 不执行，进度条永久显示"运行中"
- **修复建议**：加超时看门狗（如 30 分钟无更新自动置失败）

### 18. `ThreadPoolExecutor._max_workers` 私有 API hack
- **位置**：`codex_oauth/import_jobs.py`（`set_concurrency` 持 `_condition` → `_shutdown_lock` 调 `_adjust_thread_count`）
- **问题**：依赖 CPython 私有结构，当前无死锁，但 CPython 重构该内部接口时会 AttributeError → 设置接口 500
- **修复建议**：改为按配置上限重建 executor，或接受并发只增不减

### 19. 统计与时序杂项
- **位置 / 问题**：
  - `register/protocol.py`：注册成功 + 停止请求的分支提前 return，跳过 `stats["done"]/["success"]` 累计，平均耗时失真
  - `mail/__init__.py`：`IN_USE_STALE_SECONDS = 15min` vs 注册含重试可超 15 分钟 → 卡死任务期间邮箱被新批次判 stale 重复分配
  - `web/routes/codex_oauth.py`：停止期间 `acquire` 抛 `_SmsCancelled` 也被记 acquire_failed（取消计为失败）；passwordless 等码文案"两轮共180秒"实为 4 段×45s
  - `register/protocol_registrar.py`：`_is_passwordless_invalid_state` 为死代码（全仓库无调用）
  - `register/protocol_ops.py`：多个 `*_one` 操作用缺省 `ProtocolRuntimeSettings()` 无 deadline，最坏可挂 ~12 分钟（仅 `login_with_password_totp_one` / `recover_pending_registration_one` 有）
  - `common/account_store.py`：`_epoch` 对无时区串按本地时区解析，遗留数据排序偏差 8h

---

**已修复（高危，提交 `6ae2831`）**：SMSBower cancel 吞错、会话失效重试无上限（两处）、CLI 批量重登不写库、备份恢复不清缓存（含日志 inode 重连）、测活单账号异常炸整批、关机漏邮箱换绑、job 线程启动失败卡 running。

**已排除（不适用）**：mail 时区 `+0800` 解析问题——生产为 Python 3.12，`fromisoformat` 已支持，不触发。
