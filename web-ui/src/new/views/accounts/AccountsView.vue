<script setup lang="ts">
/* --- 账号列表页：工具栏 + 测活进度 + 表格/卡片 + 批量操作条 + 全部批任务弹窗 --- */
import { inject, onMounted, ref, watch } from "vue";
import { useAccountsStore } from "../../stores/accounts";
import { useGroupsStore } from "../../stores/groups";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import AccountToolbar from "./AccountToolbar.vue";
import AccountTable from "./AccountTable.vue";
import AccountCardList from "./AccountCardList.vue";
import AccountBatchBar from "./AccountBatchBar.vue";
import HealthProgress from "./HealthProgress.vue";
import HealthStartDialog from "./HealthStartDialog.vue";
import AccountLogDialog from "./AccountLogDialog.vue";
import DeviceInfoDialog from "./DeviceInfoDialog.vue";
import ImportAccountsDialog from "./ImportAccountsDialog.vue";
import BatchStartDialog, { type BatchKind } from "./BatchStartDialog.vue";
import BatchJobDialog from "./BatchJobDialog.vue";
import AppEmpty from "../../components/ui/AppEmpty.vue";
import AppPager from "../../components/ui/AppPager.vue";
import { Contact } from "@lucide/vue";
import type { Account } from "../../api/accounts";

const accounts = useAccountsStore();
const groups = useGroupsStore();
const batch = useBatchOpsStore();
const ui = useUiStore();
const confirm = useConfirm();

const logDialogKey = ref("");
const logDialogOpen = ref(false);
const importDialogOpen = ref(false);
const showImportHistory = inject<() => void>("open-import-history");
function openImportHistory() {
  importDialogOpen.value = false;
  showImportHistory?.();
}

/* 设备信息弹窗 */
const deviceDialogOpen = ref(false);
const deviceDialogKey = ref("");
const deviceDialogEmail = ref("");

function openDevice(account: Account) {
  deviceDialogKey.value = account.delete_key;
  deviceDialogEmail.value = account.email;
  deviceDialogOpen.value = true;
}

/** 行内单账号刷新 AT：单账号默认并发 1，启动后打开批次进度弹窗 */
async function refreshAtOne(account: Account) {
  try {
    await batch.startRefreshAt([account.delete_key], false, 1);
    refreshAtDialogOpen.value = true;
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "刷新 AT 启动失败", "error");
  }
}

/** 单条与批量共用预检表单，默认补取缺少的 RT。 */
function codexOne(account: Account) {
  batch.openCodexStart([account.email]);
}

/* 批任务启动弹窗状态 */
const startDialogOpen = ref(false);
const startDialogKind = ref<BatchKind>("reauth");
const startDialogKeys = ref<string[]>([]);
const startDialogAll = ref(false);

/* 测活启动弹窗：选中账号和全量测活共用，默认并发从服务端设置读取。 */
const healthStartDialogOpen = ref(false);
const healthStartKeys = ref<string[]>([]);
const healthStartAll = ref(false);

/** 测活全部（不选中）：跳过已封号 */
async function healthAll() {
  await accounts.load();
  if (accounts.loadError) {
    ui.notify("账号列表加载不完整，请刷新后重试", "error");
    return;
  }
  if (accounts.health.running || !accounts.activeCount) return;
  healthStartKeys.value = [];
  healthStartAll.value = true;
  healthStartDialogOpen.value = true;
}

function healthSelected(keys: string[]) {
  if (accounts.health.running || !keys.length) return;
  healthStartKeys.value = [...keys];
  healthStartAll.value = false;
  healthStartDialogOpen.value = true;
}

function onHealthStarted() {
  accounts.clearSelection();
}

/** 刷新AT全部（不选中）：服务端按"测活失败 ∪ AT过期/缺失"筛选 */
function refreshAtAll() {
  startDialogKind.value = "refreshAt";
  startDialogKeys.value = [];
  startDialogAll.value = true;
  startDialogOpen.value = true;
}

/** 重新登录全部（不选中）：前端按旧版口径筛待处理账号，协议方式需密码+2FA */
function reauthAll() {
  const keys = accounts.protocolReauthKeys;
  if (!keys.length) {
    ui.notify("待处理账号中没有同时保存密码和 TOTP 2FA 的记录，无法走协议重新登录", "warning");
    return;
  }
  const skipped = accounts.atRiskKeys.length - keys.length;
  if (skipped > 0) ui.notify(`${skipped} 个待处理账号缺少密码或 2FA，已自动跳过`, "warning", 4000);
  startDialogKind.value = "reauth";
  startDialogKeys.value = keys;
  startDialogAll.value = false;
  startDialogOpen.value = true;
}

/** 检测全部账号的试用资格 */
function trialAll() {
  startDialogKind.value = "trial";
  startDialogKeys.value = [];
  startDialogAll.value = true;
  startDialogOpen.value = true;
}

/* 各批任务进度弹窗 */
const reauthJobDialogOpen = ref(false);
const refreshAtDialogOpen = ref(false);
const trialDialogOpen = ref(false);

function openLogs(key: string) {
  logDialogKey.value = key;
  logDialogOpen.value = true;
}

async function removeOne(key: string, email: string) {
  if (!(await confirm(`删除账号 ${email}？将移入回收站。`, { title: "删除账号", confirmText: "删除", danger: true }))) return;
  try {
    await accounts.remove([key]);
    ui.notify("账号已删除", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "删除失败", "error");
  }
}

/** 点"导入账号"：后台有 codex/导入批次在跑时直接重连实时进度，否则打开表单 */
async function openImport() {
  const attached = await batch.openCodexDialogIfRunning();
  if (!attached) importDialogOpen.value = true;
}

function openStartDialog(kind: BatchKind) {
  startDialogKind.value = kind;
  startDialogKeys.value = [...accounts.selectedKeys];
  startDialogAll.value = false;
  startDialogOpen.value = true;
}

function onBatchStarted(kind: BatchKind) {
  accounts.clearSelection();
  if (kind === "refreshAt") refreshAtDialogOpen.value = true;
  else if (kind === "trial") trialDialogOpen.value = true;
  else reauthJobDialogOpen.value = true;
}

/* 筛选变化时回到第一页 */
watch(
  () => [accounts.query, accounts.statusFilter, accounts.planFilter, accounts.trialFilter, accounts.smsFilter, accounts.groupFilter, accounts.pageSize],
  () => accounts.resetPage(),
);

onMounted(async () => {
  batch.setOnFinished(() => accounts.load());
  await Promise.all([groups.load(), accounts.load(), accounts.refreshHealth()]);
  /* 页面刷新后接管所有后台批次，避免刷新 AT/试用检测继续运行但进度窗口丢失。 */
  await batch.restoreBackgroundBatches();
});
</script>

<template>
  <div class="accounts-page">
    <AccountToolbar
      @import="openImport"
      @health-all="healthAll"
      @refresh-at-all="refreshAtAll"
      @reauth-all="reauthAll"
      @trial-all="trialAll"
    />

    <HealthProgress />

    <section class="list-panel glass-panel">
      <div v-if="accounts.loadError" class="load-error">
        <AppEmpty title="账号加载失败" :hint="accounts.loadError">
          <template #icon><Contact :size="48" :stroke-width="1" /></template>
        </AppEmpty>
      </div>

      <template v-else>
        <div v-if="accounts.loading && !accounts.rows.length" class="list-loading">
          <span class="loading-spinner" aria-hidden="true"></span>
          正在加载账号列表
        </div>

        <AppEmpty
          v-else-if="!accounts.filtered.length"
          :title="accounts.rows.length ? '没有匹配筛选条件的账号' : '暂无账号'"
          :hint="accounts.rows.length ? '试试调整搜索或筛选条件' : '先去任务调度页创建注册任务'"
        >
          <template #icon><Contact :size="48" :stroke-width="1" /></template>
        </AppEmpty>

        <template v-else>
          <AccountTable class="only-desktop" @logs="openLogs" @delete="removeOne" @device="openDevice" @refresh-at="refreshAtOne" @codex="codexOne" />
          <AccountCardList class="only-mobile" @logs="openLogs" @delete="removeOne" @device="openDevice" @refresh-at="refreshAtOne" @codex="codexOne" />
          <AppPager
            :page="accounts.page"
            :pages="accounts.pageCount"
            :total="accounts.filtered.length"
            @update:page="accounts.page = $event"
          />
        </template>
      </template>

      <div v-if="accounts.loading && accounts.rows.length" class="bg-loading">
        后台加载中 {{ accounts.loadProgress.loaded }} / {{ accounts.loadProgress.total }}
      </div>
    </section>

    <AccountBatchBar @start-batch="openStartDialog" @start-health="healthSelected" @codex="() => {}" />
    <AccountLogDialog :account-key="logDialogKey" :open="logDialogOpen" @close="logDialogOpen = false" />
    <DeviceInfoDialog :open="deviceDialogOpen" :account-key="deviceDialogKey" :email="deviceDialogEmail" @close="deviceDialogOpen = false" />

    <!-- 批任务弹窗组 -->
    <ImportAccountsDialog :open="importDialogOpen" @close="importDialogOpen = false" @history="openImportHistory" />
    <HealthStartDialog :open="healthStartDialogOpen" :keys="healthStartKeys" :all="healthStartAll" @close="healthStartDialogOpen = false" @started="onHealthStarted" />
    <BatchStartDialog :open="startDialogOpen" :kind="startDialogKind" :keys="startDialogKeys" :all="startDialogAll" @close="startDialogOpen = false" @started="onBatchStarted" />
    <BatchJobDialog :open="reauthJobDialogOpen || batch.reauthDialogOpen" kind="reauth" @close="reauthJobDialogOpen = false; batch.reauthDialogOpen = false" />
    <BatchJobDialog :open="refreshAtDialogOpen || batch.refreshAtDialogOpen" kind="refreshAt" @close="refreshAtDialogOpen = false; batch.refreshAtDialogOpen = false" />
    <BatchJobDialog :open="trialDialogOpen || batch.trialDialogOpen" kind="trial" @close="trialDialogOpen = false; batch.trialDialogOpen = false" />
  </div>
</template>

<style scoped>
.accounts-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.list-panel {
  padding: 8px 8px 12px;
  display: flex;
  flex-direction: column;
  min-height: 320px;
}

.list-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 60px 0;
  color: var(--text-muted);
  font-size: 0.88rem;
}

.loading-spinner {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--accent-lighter);
  border-top-color: var(--accent);
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.bg-loading {
  text-align: center;
  font-size: 0.72rem;
  color: var(--text-muted);
  padding: 8px 0 2px;
}

.only-mobile {
  display: none;
}

@media (max-width: 768px) {
  .only-desktop {
    display: none;
  }

  .only-mobile {
    display: block;
  }
}
</style>
