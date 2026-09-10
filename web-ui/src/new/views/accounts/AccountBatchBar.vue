<script setup lang="ts">
/* --- 批量操作条：选中账号后底部吸附（含接码/刷新AT/试用/重登/安全修复/改密/改2FA） --- */
import { computed, ref } from "vue";
import {
  Activity, Copy, Download, FileDown, FolderInput, KeyRound, LogIn, ShieldCheck,
  ShieldEllipsis, Smartphone, Sparkles, Trash2, X, Zap,
} from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useAccountsStore } from "../../stores/accounts";
import { useGroupsStore } from "../../stores/groups";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import {
  downloadAccountsJson, exportAccessTokens, exportSecurityCredentials, fetchAccessTokens,
} from "../../api/accounts";
import { useCopy } from "../../composables/useCopy";
import type { BatchKind } from "./BatchStartDialog.vue";

const emit = defineEmits<{
  startBatch: [kind: BatchKind];
  codex: [];
}>();

const accounts = useAccountsStore();
const groups = useGroupsStore();
const batch = useBatchOpsStore();
const ui = useUiStore();
const confirm = useConfirm();
const { copyText } = useCopy();

const count = computed(() => accounts.selectedKeys.length);
const busy = ref(false);

async function run(action: () => Promise<unknown>) {
  if (busy.value) return;
  busy.value = true;
  try {
    await action();
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "操作失败", "error");
  } finally {
    busy.value = false;
  }
}

function healthSelected() {
  run(async () => {
    await accounts.startHealth(accounts.selectedKeys, false, 20);
    ui.notify(`已开始测活 ${count.value} 个账号`, "success");
    accounts.clearSelection();
  });
}

function copyTokens() {
  run(async () => {
    const result = await fetchAccessTokens(accounts.selectedKeys);
    const ok = await copyText(result.tokens.join("\n"), "batch-at");
    if (ok) {
      ui.notify(
        `已复制 ${result.exported} 条 AT${result.missing ? `，${result.missing} 条缺失` : ""}`,
        result.missing ? "warning" : "success",
      );
    }
  });
}

function downloadJson() {
  run(async () => {
    await downloadAccountsJson(accounts.selectedKeys);
    ui.notify("已开始下载 JSON", "success");
  });
}

function exportSecurity() {
  run(async () => {
    const result = await exportSecurityCredentials(accounts.selectedKeys);
    ui.notify(`已导出 ${result.exported} 条凭据${result.missing ? `，${result.missing} 条缺失` : ""}`, "success");
  });
}

function exportTokens() {
  run(async () => {
    const result = await exportAccessTokens(accounts.selectedKeys);
    ui.notify(`已导出 ${result.exported} 条 AT`, "success");
  });
}

function moveTo(groupId: string) {
  if (!groupId) return;
  run(async () => {
    const name = groups.nameOf(groupId);
    await groups.move(groupId, accounts.selectedKeys);
    ui.notify(`已移动 ${count.value} 个账号到「${name}」`, "success");
    accounts.clearSelection();
    await accounts.load();
  });
}

function removeSelected() {
  run(async () => {
    if (!(await confirm(`删除选中的 ${count.value} 个账号？将移入回收站。`, { title: "批量删除", confirmText: "删除", danger: true }))) return;
    const result = await accounts.remove(accounts.selectedKeys);
    ui.notify(`已删除 ${result.removed} 个账号`, "success");
  });
}

/* --- 接码：选中邮箱直接启动 codex 批次（默认并发 3） --- */
function codexSelected() {
  run(async () => {
    const emails = accounts.pageRows
      .filter((a) => accounts.selectedSet.has(a.delete_key))
      .map((a) => a.email)
      .filter(Boolean);
    if (!emails.length) return;
    await batch.runCodex(emails, 3);
    emit("codex");
  });
}

/* --- 打开启动参数弹窗的批次 --- */
function openStart(kind: BatchKind) {
  emit("startBatch", kind);
}
</script>

<template>
  <Transition name="batch">
    <div v-if="count" class="batch-bar glass-panel">
      <span class="batch-count">已选 <b>{{ count }}</b> 个账号</span>

      <div class="batch-actions">
        <AppButton variant="primary" size="sm" :disabled="busy" @click="healthSelected">
          <Activity :size="14" /><span>测活</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.codex.running" title="对选中账号接码并查看批量日志" @click="codexSelected">
          <Smartphone :size="14" /><span>接码</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" title="复制选中账号的 Access Token" @click="copyTokens">
          <Copy :size="14" /><span>复制 AT</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" title="导出邮箱----密码----2FA" @click="exportSecurity">
          <ShieldCheck :size="14" /><span>导出凭据</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" @click="exportTokens">
          <FileDown :size="14" /><span>导出 AT</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy" @click="downloadJson">
          <Download :size="14" /><span>下载 JSON</span>
        </AppButton>

        <span class="batch-divider" aria-hidden="true"></span>

        <AppButton variant="ghost" size="sm" :disabled="busy || batch.refreshAt.running" title="通过 Session 刷新选中账号的 Access Token" @click="openStart('refreshAt')">
          <Zap :size="14" /><span>刷新AT</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.trial.running" title="检测选中账号的 Plus 免费试用资格" @click="openStart('trial')">
          <Sparkles :size="14" /><span>试用检测</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="使用已保存的密码和 2FA 重新登录" @click="openStart('reauth')">
          <LogIn :size="14" /><span>重新登录</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="补全缺失的密码和 TOTP 2FA" @click="openStart('security_repair')">
          <ShieldEllipsis :size="14" /><span>安全修复</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="修改选中账号的登录密码" @click="openStart('password_change')">
          <KeyRound :size="14" /><span>改密</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" :disabled="busy || batch.reauth.running" title="更换选中账号的 TOTP 2FA 因子" @click="openStart('totp_change')">
          <FolderInput :size="14" /><span>改2FA</span>
        </AppButton>

        <span class="batch-divider" aria-hidden="true"></span>

        <select class="input move-select" :value="''" aria-label="移动到分组" @change="moveTo(($event.target as HTMLSelectElement).value); ($event.target as HTMLSelectElement).value = ''">
          <option value="" disabled selected>移动到分组…</option>
          <option v-for="group in groups.items" :key="group.id" :value="group.id">{{ group.name }}</option>
        </select>

        <AppButton variant="danger" size="sm" :disabled="busy" @click="removeSelected">
          <Trash2 :size="14" /><span>删除</span>
        </AppButton>
        <AppButton variant="ghost" size="sm" title="清除选择" @click="accounts.clearSelection()">
          <X :size="14" />
        </AppButton>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.batch-bar {
  position: sticky;
  bottom: 12px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 16px;
  flex-wrap: wrap;
}

.batch-count {
  font-size: 0.85rem;
  color: var(--text-secondary);
  white-space: nowrap;
}

.batch-count b {
  color: var(--accent);
}

.batch-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.move-select {
  width: auto;
  min-width: 128px;
  padding: 6px 10px;
  font-size: 0.78rem;
  cursor: pointer;
}

.batch-divider {
  width: 1px;
  height: 20px;
  background: var(--line-subtle);
  margin: 0 2px;
}

.batch-enter-active,
.batch-leave-active {
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}

.batch-enter-from,
.batch-leave-to {
  opacity: 0;
  transform: translateY(12px);
}

@media (max-width: 768px) {
  .batch-bar {
    bottom: 8px;
  }

  .batch-actions :deep(.btn span) {
    display: none;
  }

  .move-select {
    min-width: 110px;
  }
}
</style>
