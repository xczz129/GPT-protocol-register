<script setup lang="ts">
/* --- 账号表格（桌面端）：凭据芯片 + 代理/时间 + 彩色套餐 + 行内操作 --- */
import { computed } from "vue";
import { Check, Clipboard, ClipboardList, KeyRound, Laptop, Monitor, RefreshCw, Smartphone, Trash2 } from "@lucide/vue";
import AppTag from "../../components/ui/AppTag.vue";
import { useAccountsStore } from "../../stores/accounts";
import {
  formatShortTime, formatTime, healthChip, lastInvalidAt, methodChip, planChip, proxyDisplay,
  smsChip, trialChip, useAccountActions,
} from "./useAccountActions";
import type { Account } from "../../api/accounts";

const emit = defineEmits<{
  logs: [key: string];
  delete: [key: string, email: string];
  device: [account: Account];
  "refresh-at": [account: Account];
  codex: [account: Account];
}>();

const accounts = useAccountsStore();
const {
  copiedKey, copyCredential, copyEmail, copyAccountId, copyField, copyAccessToken, copySessionToken,
  totpDisplay, handleTotpClick,
} = useAccountActions();

const pageKeys = computed(() => accounts.pageRows.map((a) => a.delete_key));
const allChecked = computed(() => pageKeys.value.length > 0 && pageKeys.value.every((k) => accounts.selectedSet.has(k)));
</script>

<template>
  <div class="table-wrap">
    <table class="account-table">
      <thead>
        <tr>
          <th class="col-check">
            <input type="checkbox" :checked="allChecked" aria-label="全选本页" @change="accounts.toggleAll(pageKeys)" />
          </th>
          <th>邮箱</th>
          <th>状态</th>
          <th>套餐</th>
          <th>分组</th>
          <th>凭据</th>
          <th>代理 / 时间</th>
          <th class="col-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="account in accounts.pageRows" :key="account.delete_key" :class="{ selected: accounts.selectedSet.has(account.delete_key), deactivated: account.is_deactivated }">
          <td class="col-check">
            <input
              type="checkbox"
              :checked="accounts.selectedSet.has(account.delete_key)"
              :aria-label="`选择 ${account.email}`"
              @change="accounts.toggle(account.delete_key)"
            />
          </td>
          <td class="col-email">
            <button
              class="email email-copy" type="button"
              :title="`点击复制 ${account.email}`"
              @click="copyEmail(account)"
            >
              <Check v-if="copiedKey === `email-${account.delete_key}`" :size="12" class="copied" />
              <span class="email-text">{{ account.email }}</span>
            </button>
            <span class="email-sub">
              <span class="method-tag" :class="methodChip(account).cls">{{ methodChip(account).text }}</span>
              · AT<template v-if="account.has_access_token">{{ account.at_expired ? " 过期" : " ✓" }}</template><template v-else> 无</template>
              <button
                v-if="account.account_id"
                class="id-copy" type="button"
                :title="`点击复制 Account ID: ${account.account_id}`"
                @click="copyAccountId(account)"
              >
                <Check v-if="copiedKey === `accid-${account.delete_key}`" :size="10" class="copied" />
                <template v-else>ID</template>
              </button>
            </span>
          </td>
          <td>
            <div class="status-tags">
              <AppTag :kind="healthChip(account).kind">{{ healthChip(account).text }}</AppTag>
              <AppTag :kind="smsChip(account).kind" :title="account.has_refresh_token ? '账号库持有 RT；中转是否齐全请打开接码与中转预检' : '账号库缺少 RT；可通过授权与中转任务补齐'">
                {{ smsChip(account).text }}
              </AppTag>
              <AppTag :kind="trialChip(account).kind">{{ trialChip(account).text }}</AppTag>
            </div>
          </td>
          <td>
            <span class="plan-tag" :style="{ color: planChip(account).color, background: planChip(account).bg }">
              {{ planChip(account).text }}
            </span>
          </td>
          <td class="col-group">{{ account.group_name || "默认分组" }}</td>

          <!-- 凭据芯片：点按复制密码 / 2FA 密钥 / 实时验证码 -->
          <td class="col-cred">
            <div class="cred-chips">
              <button
                class="cred-chip" type="button" :disabled="!account.has_password"
                :title="account.has_password ? '点击复制完整密码' : '未保存密码'"
                @click="copyField(account, 'password')"
              >
                <Check v-if="copiedKey === `password-${account.delete_key}`" :size="11" class="copied" />
                密码
              </button>
              <button
                class="cred-chip" type="button" :disabled="!account.has_totp"
                :title="account.has_totp ? '点击复制完整 2FA 密钥' : '未保存 2FA 密钥'"
                @click="copyField(account, 'totp_secret')"
              >
                <Check v-if="copiedKey === `totp_secret-${account.delete_key}`" :size="11" class="copied" />
                2FA
              </button>
              <button
                class="cred-chip code" type="button" :disabled="!account.has_totp"
                title="点击获取并复制 6 位实时 2FA 验证码"
                @click="handleTotpClick(account)"
              >
                <Check v-if="copiedKey === `totp_code-${account.delete_key}`" :size="11" class="copied" />
                {{ account.has_totp ? totpDisplay(account) : "-" }}
              </button>
            </div>
          </td>

          <!-- 代理 / 时间 -->
          <td class="col-meta">
            <div class="meta-block">
              <span class="meta-proxy" :title="account.proxy || '本地·美国'">{{ proxyDisplay(account.proxy).label }}</span>
              <span class="meta-time" :title="`注册时间：${formatTime(account.registered_at || account.updated_at)}`">
                <em>注册</em>{{ formatShortTime(account.registered_at || account.updated_at) }}
              </span>
              <span class="meta-time" :title="`最近刷新：${formatTime(account.health_checked_at)}`">
                <em>刷新</em>{{ formatShortTime(account.health_checked_at) }}
              </span>
              <span :class="['meta-time', { 'is-failed': lastInvalidAt(account) }]" :title="lastInvalidAt(account) ? `最后失效：${formatTime(lastInvalidAt(account))}` : '账号当前状态有效'">
                <em>失效</em>{{ lastInvalidAt(account) ? formatShortTime(lastInvalidAt(account)) : "-" }}
              </span>
            </div>
          </td>

          <td class="col-actions">
            <div class="row-actions-grid">
              <button
                class="row-action" type="button"
                title="刷新 Access Token（通过 Session 重签 AT）"
                :disabled="!account.has_session_token || account.is_deactivated"
                @click="emit('refresh-at', account)"
              >
                <RefreshCw :size="14" />
                <span>刷新AT</span>
              </button>
              <button
                class="row-action" type="button"
                :title="account.has_refresh_token ? '检查中转、补同步或重新授权' : '授权并补齐中转'"
                :aria-label="`设置 ${account.email} 的接码任务`"
                :disabled="account.is_deactivated"
                @click="emit('codex', account)"
              >
                <Smartphone :size="14" />
                <span>接码</span>
              </button>
              <button class="row-action" type="button" title="查看设备信息（注册环境 / 指纹）" @click="emit('device', account)">
                <Laptop :size="14" />
                <span>设备</span>
              </button>
              <button class="row-action" type="button" title="复制凭据（邮箱----密码----2FA）" @click="copyCredential(account)">
                <Check v-if="copiedKey === `cred-${account.delete_key}`" :size="14" class="copied" />
                <Clipboard v-else :size="14" />
                <span>凭据</span>
              </button>
              <button class="row-action" type="button" title="复制 Access Token" :disabled="!account.has_access_token" @click="copyAccessToken(account)">
                <Check v-if="copiedKey === `at-${account.delete_key}`" :size="14" class="copied" />
                <KeyRound v-else :size="14" />
                <span>AT</span>
              </button>
              <button class="row-action" type="button" title="复制 Session Token" :disabled="!account.has_session_token" @click="copySessionToken(account)">
                <Check v-if="copiedKey === `session_token-${account.delete_key}`" :size="14" class="copied" />
                <Monitor v-else :size="14" />
                <span>Session</span>
              </button>
              <button class="row-action" type="button" title="操作日志" @click="emit('logs', account.delete_key)">
                <ClipboardList :size="14" />
                <span>日志</span>
              </button>
              <button class="row-action danger" type="button" title="删除账号" @click="emit('delete', account.delete_key, account.email)">
                <Trash2 :size="14" />
                <span>删除</span>
              </button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-wrap {
  overflow-x: auto;
}

.account-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.83rem;
}

.account-table th {
  text-align: left;
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--text-muted);
  padding: 10px 12px;
  border-bottom: 1px solid var(--line-subtle);
  white-space: nowrap;
  position: sticky;
  top: 0;
  background: var(--glass-bg-strong);
  backdrop-filter: var(--glass-blur);
  z-index: 1;
}

.account-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line-subtle);
  color: var(--text-secondary);
  white-space: nowrap;
}

.account-table tr:last-child td {
  border-bottom: none;
}

.account-table tr.selected td {
  background: var(--accent-bg);
}

.account-table tr.deactivated td {
  opacity: 0.55;
}

.account-table tbody tr {
  transition: var(--transition-fast);
}

.account-table tbody tr:hover td {
  background: var(--glass-bg-hover);
}

.col-check {
  width: 36px;
}

.col-check input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  cursor: pointer;
}

.col-email {
  max-width: 230px;
}

.email {
  display: block;
  color: var(--text-primary);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* --- Account ID 复制小按钮 --- */
.id-copy {
  margin-left: 4px;
  padding: 1px 6px;
  border-radius: 6px;
  border: 1px solid var(--line-subtle);
  background: var(--glass-bg);
  color: var(--text-muted);
  font-size: 0.66rem;
  cursor: pointer;
  transition: var(--transition-fast);
  vertical-align: middle;
}

.id-copy:hover {
  color: var(--accent);
  border-color: var(--accent-border);
}

.id-copy .copied {
  color: var(--success);
}

/* --- 邮箱点击即复制 --- */
.email-copy {
  width: 100%;
  padding: 2px 4px;
  margin: -2px -4px;
  border: none;
  border-radius: 6px;
  background: none;
  font-size: inherit;
  text-align: left;
  cursor: copy;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: var(--transition-fast);
}

.email-copy:hover {
  background: var(--accent-bg);
  color: var(--accent);
}

.email-copy .copied {
  color: var(--success);
  flex-shrink: 0;
}

.email-text {
  overflow: hidden;
  text-overflow: ellipsis;
}

/* --- 注册方式徽标：协议（蓝）/ 浏览器（紫）一眼可辨 --- */
.method-tag {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border-radius: 20px;
  font-size: 0.66rem;
  font-weight: 600;
}

.method-tag.is-protocol {
  background: var(--accent-bg);
  color: var(--accent);
}

.method-tag.is-browser {
  background: rgba(124, 58, 237, 0.12);
  color: #7c3aed;
}

.method-tag.is-unknown {
  background: var(--glass-bg);
  color: var(--text-muted);
}

/* --- 状态列：测活 + 接码状态上下排列 --- */
.status-tags {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}

.email-sub {
  display: block;
  font-size: 0.68rem;
  color: var(--text-muted);
  margin-top: 1px;
}

.col-group {
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* --- 套餐彩色标签 --- */
.plan-tag {
  display: inline-flex;
  align-items: center;
  padding: 3px 11px;
  border-radius: 20px;
  font-size: 0.75rem;
  font-weight: 600;
  white-space: nowrap;
}

/* --- 凭据芯片 --- */
.cred-chips {
  display: flex;
  gap: 4px;
}

.cred-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 3px 9px;
  border-radius: 7px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg);
  color: var(--text-muted);
  font-size: 0.7rem;
  cursor: pointer;
  transition: var(--transition-fast);
  font-variant-numeric: tabular-nums;
}

.cred-chip:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent-border);
  background: var(--accent-bg);
}

.cred-chip:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.cred-chip.code {
  min-width: 86px;
  justify-content: center;
  font-weight: 600;
  color: var(--accent);
}

.cred-chip .copied {
  color: var(--success);
}

/* --- 代理 / 时间 --- */
.meta-block {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.meta-proxy {
  font-size: 0.75rem;
  color: var(--text-primary);
}

.meta-time {
  font-size: 0.68rem;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.meta-time em {
  font-style: normal;
  color: var(--text-faint, var(--text-muted));
  margin-right: 4px;
  opacity: 0.75;
}

.meta-time.is-failed {
  color: var(--error);
}

.col-actions {
  white-space: nowrap;
}

.row-actions-grid {
  display: grid;
  grid-template-columns: repeat(4, auto);
  gap: 4px 4px;
  justify-content: start;
}

.row-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 26px;
  padding: 0 8px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: none;
  color: var(--text-muted);
  font-size: 0.72rem;
  white-space: nowrap;
  cursor: pointer;
  transition: var(--transition-fast);
}

.row-action:hover:not(:disabled) {
  background: var(--accent-bg);
  color: var(--accent);
  border-color: var(--accent-border);
}

.row-action:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.row-action.danger:hover {
  background: var(--error-bg);
  color: var(--error);
  border-color: transparent;
}

.row-action .copied {
  color: var(--success);
}
</style>
