<script setup lang="ts">
/* --- 账号卡片列表（移动端）：凭据芯片 + 代理/时间 + 彩色套餐 --- */
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
  copiedKey, copyCredential, copyEmail, copyField, copyAccessToken, copySessionToken,
  totpDisplay, handleTotpClick,
} = useAccountActions();
</script>

<template>
  <div class="card-list">
    <article
      v-for="account in accounts.pageRows"
      :key="account.delete_key"
      :class="['account-card', { selected: accounts.selectedSet.has(account.delete_key), deactivated: account.is_deactivated }]"
    >
      <div class="card-head">
        <input
          type="checkbox"
          :checked="accounts.selectedSet.has(account.delete_key)"
          :aria-label="`选择 ${account.email}`"
          @change="accounts.toggle(account.delete_key)"
        />
        <button
          class="card-email email-copy" type="button"
          :title="`点击复制 ${account.email}`"
          @click="copyEmail(account)"
        >
          <Check v-if="copiedKey === `email-${account.delete_key}`" :size="12" class="copied" />
          <span>{{ account.email }}</span>
        </button>
      </div>

      <div class="card-tags">
        <span class="method-tag" :class="methodChip(account).cls">{{ methodChip(account).text }}</span>
        <AppTag :kind="healthChip(account).kind">{{ healthChip(account).text }}</AppTag>
        <AppTag :kind="smsChip(account).kind">{{ smsChip(account).text }}</AppTag>
        <AppTag :kind="trialChip(account).kind">{{ trialChip(account).text }}</AppTag>
        <span class="plan-tag" :style="{ color: planChip(account).color, background: planChip(account).bg }">
          {{ planChip(account).text }}
        </span>
      </div>

      <!-- 凭据芯片：点按复制密码 / 2FA 密钥 / 实时验证码 -->
      <div class="cred-chips">
        <button class="cred-chip" type="button" :disabled="!account.has_password" title="点击复制完整密码" @click="copyField(account, 'password')">
          <Check v-if="copiedKey === `password-${account.delete_key}`" :size="11" class="copied" />
          密码
        </button>
        <button class="cred-chip" type="button" :disabled="!account.has_totp" title="点击复制完整 2FA 密钥" @click="copyField(account, 'totp_secret')">
          <Check v-if="copiedKey === `totp_secret-${account.delete_key}`" :size="11" class="copied" />
          2FA 密钥
        </button>
        <button class="cred-chip code" type="button" :disabled="!account.has_totp" title="点击获取并复制 6 位实时验证码" @click="handleTotpClick(account)">
          <Check v-if="copiedKey === `totp_code-${account.delete_key}`" :size="11" class="copied" />
          {{ account.has_totp ? totpDisplay(account) : "-" }}
        </button>
      </div>

      <div class="card-meta">
        <span>{{ account.group_name || "默认分组" }} · {{ proxyDisplay(account.proxy).label }}</span>
        <span>
          注册 {{ formatShortTime(account.registered_at || account.updated_at) }} ·
          刷新 {{ formatShortTime(account.health_checked_at) }} ·
          <em :class="{ 'is-failed': lastInvalidAt(account) }">失效 {{ lastInvalidAt(account) ? formatShortTime(lastInvalidAt(account)) : "-" }}</em>
        </span>
      </div>

      <div class="card-actions">
        <button type="button" :disabled="!account.has_session_token || account.is_deactivated" title="刷新 Access Token" @click="emit('refresh-at', account)">
          <RefreshCw :size="13" />刷AT
        </button>
        <button type="button" :disabled="account.is_deactivated" title="检查中转、补同步或重新授权" :aria-label="`设置 ${account.email} 的接码任务`" @click="emit('codex', account)">
          <Smartphone :size="13" />接码
        </button>
        <button type="button" title="查看设备信息" @click="emit('device', account)">
          <Laptop :size="13" />设备
        </button>
        <button type="button" @click="copyCredential(account)">
          <Check v-if="copiedKey === `cred-${account.delete_key}`" :size="13" class="copied" />
          <Clipboard v-else :size="13" />凭据
        </button>
        <button type="button" :disabled="!account.has_access_token" @click="copyAccessToken(account)">
          <Check v-if="copiedKey === `at-${account.delete_key}`" :size="13" class="copied" />
          <KeyRound v-else :size="13" />AT
        </button>
        <button type="button" :disabled="!account.has_session_token" @click="copySessionToken(account)">
          <Check v-if="copiedKey === `session_token-${account.delete_key}`" :size="13" class="copied" />
          <Monitor v-else :size="13" />ST
        </button>
        <button type="button" @click="emit('logs', account.delete_key)">
          <ClipboardList :size="13" />日志
        </button>
        <button type="button" class="danger" @click="emit('delete', account.delete_key, account.email)">
          <Trash2 :size="13" />删除
        </button>
      </div>
    </article>
  </div>
</template>

<style scoped>
.card-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 4px;
}

.account-card {
  padding: 12px 14px;
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.account-card.selected {
  border-color: var(--accent-border);
  background: var(--accent-bg);
}

.account-card.deactivated {
  opacity: 0.55;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-head input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  flex-shrink: 0;
}

.card-email {
  font-size: 0.86rem;
  font-weight: 500;
  color: var(--text-primary);
  overflow-wrap: anywhere;
  line-height: 1.35;
}

/* --- 邮箱点击即复制 --- */
.email-copy {
  border: none;
  background: none;
  padding: 2px 4px;
  margin: -2px -4px;
  border-radius: 6px;
  cursor: copy;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  text-align: left;
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

/* --- 注册方式徽标：协议（蓝）/ 浏览器（紫）一眼可辨 --- */
.method-tag {
  display: inline-flex;
  align-items: center;
  padding: 3px 11px;
  border-radius: 20px;
  font-size: 0.75rem;
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

.card-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}

.plan-tag {
  display: inline-flex;
  align-items: center;
  padding: 3px 11px;
  border-radius: 20px;
  font-size: 0.75rem;
  font-weight: 600;
}

.cred-chips {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
}

.cred-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 4px 10px;
  border-radius: 7px;
  border: 1px solid var(--glass-border);
  background: var(--field-bg);
  color: var(--text-muted);
  font-size: 0.72rem;
  cursor: pointer;
  transition: var(--transition-fast);
  font-variant-numeric: tabular-nums;
}

.cred-chip:disabled {
  opacity: 0.35;
}

.cred-chip.code {
  min-width: 92px;
  justify-content: center;
  font-weight: 600;
  color: var(--accent);
}

.cred-chip .copied {
  color: var(--success);
}

.card-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 0.72rem;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.card-meta em {
  font-style: normal;
}

.card-meta .is-failed {
  color: var(--error);
}

.card-actions {
  display: flex;
  gap: 6px;
  border-top: 1px solid var(--line-subtle);
  padding-top: 8px;
}

.card-actions button {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 6px 0;
  border-radius: 8px;
  border: 1px solid var(--field-border);
  background: var(--field-bg);
  color: var(--text-secondary);
  font-size: 0.75rem;
  cursor: pointer;
}

.card-actions button:disabled {
  opacity: 0.4;
}

.card-actions button.danger {
  color: var(--error);
}

.card-actions .copied {
  color: var(--success);
}
</style>
