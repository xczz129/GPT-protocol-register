<script setup lang="ts">
/* --- 账号页工具栏：搜索、筛选、导入、刷新、分组管理 + 不选中的全量批量入口 --- */
import { ref } from "vue";
import { Activity, Download, FolderPlus, LogIn, RefreshCw, Search, Settings2, Sparkles, Trash2, Upload, Zap } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useAccountsStore } from "../../stores/accounts";
import { useGroupsStore } from "../../stores/groups";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import { exportGroupCredentials } from "../../api/groups";
import { downloadAccountsJson } from "../../api/accounts";

const emit = defineEmits<{
  import: [];
  healthAll: [];
  refreshAtAll: [];
  reauthAll: [];
  trialAll: [];
}>();

const accounts = useAccountsStore();
const groups = useGroupsStore();
const batch = useBatchOpsStore();
const ui = useUiStore();
const confirm = useConfirm();

const groupPanelOpen = ref(false);
const newGroupName = ref("");

async function refresh() {
  await Promise.all([accounts.load(), groups.load(true)]);
  ui.notify("账号列表已刷新", "success", 1800);
}

const downloading = ref(false);

/** 下载全部账号 JSON（与旧版"下载全部"一致） */
async function downloadAll() {
  if (downloading.value) return;
  downloading.value = true;
  try {
    await downloadAccountsJson([], true);
    ui.notify("已开始下载全部账号 JSON", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "下载失败", "error");
  } finally {
    downloading.value = false;
  }
}

/** 删除测活失效账号（与旧版"删除失效"一致） */
async function removeInvalidAccounts() {
  const n = accounts.invalidKeys.length;
  if (!n) return;
  if (!(await confirm(`删除 ${n} 个测活失效的账号？将移入回收站。`, { title: "删除失效账号", confirmText: "删除", danger: true }))) return;
  try {
    const result = await accounts.removeInvalid(accounts.invalidKeys);
    ui.notify(`已删除 ${result.removed ?? n} 个失效账号`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "删除失败", "error");
  }
}

const deletingAll = ref(false);

/** 全部删除：完整数量预检、独立输入确认，整个过程只允许一个操作。 */
async function removeAllAccounts() {
  if (deletingAll.value || accounts.loading) return;
  deletingAll.value = true;
  try {
    await accounts.load();
    if (accounts.loadError || accounts.loading || accounts.rows.length !== accounts.total) {
      ui.notify("账号列表加载不完整，无法确认删除数量，请刷新后重试", "error");
      return;
    }
    const count = accounts.total;
    if (!count) return;
    if (!(await confirm(`删除全部 ${count} 个账号？将移入回收站。`, { title: "全部删除", confirmText: "继续", danger: true }))) return;
    if (!(await confirm(`即将删除全部 ${count} 个账号，影响所有分组。请核对数量，再输入“删除全部”确认。`, {
      title: "确认删除全部账号", confirmText: `删除全部 ${count} 个账号`, danger: true, requiredInput: "删除全部",
    }))) return;
    await accounts.load();
    if (accounts.loadError || accounts.loading || accounts.total !== count || accounts.rows.length !== count) {
      ui.notify("账号列表已变化或加载不完整，未执行删除，请重新确认", "error");
      return;
    }
    const result = await accounts.remove([], true);
    ui.notify(`已删除 ${result.removed ?? "全部"} 个账号`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "删除失败", "error");
  } finally {
    deletingAll.value = false;
  }
}

async function addGroup() {
  const name = newGroupName.value.trim();
  if (!name) return;
  try {
    await groups.create(name);
    newGroupName.value = "";
    ui.notify(`分组「${name}」已创建`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "创建失败", "error");
  }
}

async function renameOne(id: string, oldName: string) {
  const name = window.prompt("新的分组名称", oldName)?.trim();
  if (!name || name === oldName) return;
  try {
    await groups.rename(id, name);
    ui.notify("分组已重命名", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "重命名失败", "error");
  }
}

async function removeOne(id: string, name: string) {
  if (!(await confirm(`删除分组「${name}」？组内账号将移入默认分组。`, { title: "删除分组", confirmText: "删除", danger: true }))) return;
  try {
    await groups.remove(id);
    if (accounts.groupFilter === id) accounts.groupFilter = "all";
    await accounts.load();
    ui.notify("分组已删除", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "删除失败", "error");
  }
}
</script>

<template>
  <section class="toolbar glass-panel">
    <div class="toolbar-row">
      <div class="search-box">
        <Search :size="15" class="search-icon" />
        <input v-model="accounts.query" class="input search-input" type="text" placeholder="搜索邮箱 / 账号 ID / 代理" />
      </div>

      <select v-model="accounts.statusFilter" class="input filter-select" aria-label="状态筛选">
        <option value="all">全部状态</option>
        <option value="usable">可用</option>
        <option value="limited">限流</option>
        <option value="invalid">失效</option>
        <option value="error">异常/待续期</option>
        <option value="unchecked">未测活</option>
        <option value="deactivated">已封号</option>
      </select>

      <select v-model="accounts.planFilter" class="input filter-select" aria-label="套餐筛选">
        <option value="all">全部套餐</option>
        <option value="free">Free</option>
        <option value="plus">Plus</option>
        <option value="team">Team</option>
        <option value="pro">Pro</option>
      </select>

      <select v-model="accounts.trialFilter" class="input filter-select" aria-label="试用资格筛选">
        <option value="all">全部试用状态</option>
        <option value="eligible">可试用</option>
        <option value="discount">有折扣</option>
        <option value="ineligible">不符合</option>
        <option value="pending">待检测</option>
      </select>

      <select v-model="accounts.smsFilter" class="input filter-select" aria-label="接码筛选">
        <option value="all">全部接码状态</option>
        <option value="sms">持有 RT</option>
        <option value="no-sms">缺少 RT</option>
      </select>

      <select v-model="accounts.groupFilter" class="input filter-select" aria-label="分组筛选">
        <option value="all">全部分组</option>
        <option v-for="group in groups.items" :key="group.id" :value="group.id">
          {{ group.name }}<template v-if="group.count != null">（{{ group.count }}）</template>
        </option>
      </select>

      <AppButton variant="primary" size="sm" title="粘贴 邮箱----密码----2FA 导入新账号" @click="emit('import')">
        <Upload :size="14" /><span>导入账号</span>
      </AppButton>
      <AppButton variant="ghost" size="sm" title="下载全部账号 JSON" :loading="downloading" @click="downloadAll">
        <Download :size="14" /><span>下载全部</span>
      </AppButton>
      <AppButton variant="ghost" size="sm" title="刷新" @click="refresh">
        <RefreshCw :size="14" /><span>刷新</span>
      </AppButton>
      <AppButton variant="ghost" size="sm" title="分组管理" @click="groupPanelOpen = !groupPanelOpen">
        <Settings2 :size="14" /><span>分组</span>
      </AppButton>
    </div>

    <!-- 不选中的全量批量入口（选中账号时请用底部批量操作条） -->
    <div class="toolbar-row batch-row">
      <span class="batch-row-label">批量任务</span>
      <AppButton
        variant="accent" size="sm"
        :disabled="!accounts.activeCount || accounts.loading || accounts.health.running"
        :title="`检查账号是否可用（共 ${accounts.activeCount} 个未封号，跳过已封号）`"
        @click="emit('healthAll')"
      >
        <Activity :size="14" /><span>{{ accounts.health.running ? "测活中…" : `测活全部(${accounts.activeCount})` }}</span>
      </AppButton>
      <AppButton
        variant="accent" size="sm"
        :disabled="!accounts.atRiskKeys.length || accounts.loading || batch.refreshAt.running"
        :title="`刷新全部待处理账号的 AT（测活失败或 AT 过期/缺失共 ${accounts.atRiskKeys.length} 个，无 Session 自动跳过）`"
        @click="emit('refreshAtAll')"
      >
        <Zap :size="14" /><span>{{ batch.refreshAt.running ? "刷新AT中…" : `刷新AT全部(${accounts.atRiskKeys.length})` }}</span>
      </AppButton>
      <AppButton
        variant="accent" size="sm"
        :disabled="!accounts.atRiskKeys.length || accounts.loading || batch.reauth.running"
        :title="`重新登录全部待处理账号（共 ${accounts.atRiskKeys.length} 个，协议方式需已保存密码和 2FA）`"
        @click="emit('reauthAll')"
      >
        <LogIn :size="14" /><span>{{ batch.reauth.running ? "登录中…" : `重新登录全部(${accounts.atRiskKeys.length})` }}</span>
      </AppButton>
      <AppButton
        variant="accent" size="sm"
        :disabled="!accounts.activeCount || accounts.loading || batch.trial.running"
        title="检测全部账号的 Plus 免费试用资格"
        @click="emit('trialAll')"
      >
        <Sparkles :size="14" /><span>{{ batch.trial.running ? "试用资格检测中…" : "试用资格（全部）" }}</span>
      </AppButton>

      <span class="batch-row-spacer" aria-hidden="true"></span>

      <AppButton
        variant="danger" size="sm"
        :disabled="!accounts.invalidKeys.length || accounts.loading"
        title="删除测活失效（invalid）且未封号的账号"
        @click="removeInvalidAccounts"
      >
        <Trash2 :size="14" /><span>删除失效({{ accounts.invalidKeys.length }})</span>
      </AppButton>
      <AppButton
        variant="danger" size="sm"
        :disabled="!accounts.rows.length || accounts.loading || deletingAll"
        :loading="deletingAll"
        title="删除全部账号（需输入确认，移入回收站）"
        @click="removeAllAccounts"
      >
        <Trash2 :size="14" /><span>全部删除</span>
      </AppButton>
    </div>

    <div v-if="groupPanelOpen" class="group-panel">
      <div class="group-add">
        <input v-model="newGroupName" class="input" type="text" maxlength="64" placeholder="新分组名称" @keyup.enter="addGroup" />
        <AppButton variant="primary" size="sm" @click="addGroup"><FolderPlus :size="14" /><span>创建</span></AppButton>
      </div>
      <div class="group-list">
        <div v-for="group in groups.items" :key="group.id" class="group-row">
          <span class="group-name">{{ group.name }}</span>
          <span class="group-count">{{ group.count ?? "-" }} 个账号</span>
          <span class="group-actions">
            <button type="button" @click="exportGroupCredentials(group.id)">导出凭据</button>
            <button type="button" @click="renameOne(group.id, group.name)">重命名</button>
            <button v-if="!group.is_default" type="button" class="danger" @click="removeOne(group.id, group.name)">删除</button>
          </span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.toolbar {
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.toolbar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.search-box {
  position: relative;
  flex: 1;
  min-width: 200px;
  display: flex;
  align-items: center;
}

.search-icon {
  position: absolute;
  left: 12px;
  color: var(--text-muted);
  pointer-events: none;
}

.search-input {
  padding-left: 36px;
}

.filter-select {
  width: auto;
  min-width: 108px;
  cursor: pointer;
}

/* --- 全量批量入口行 --- */
.batch-row {
  border-top: 1px solid var(--line-subtle);
  padding-top: 10px;
}

.batch-row-label {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-right: 2px;
  white-space: nowrap;
}

.batch-row-spacer {
  flex: 1;
}

/* --- 分组管理面板 --- */
.group-panel {
  border-top: 1px solid var(--line-subtle);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.group-add {
  display: flex;
  gap: 8px;
  max-width: 380px;
}

.group-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.group-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  font-size: 0.82rem;
}

.group-name {
  font-weight: 600;
  color: var(--text-primary);
}

.group-count {
  color: var(--text-muted);
  font-size: 0.75rem;
  flex: 1;
}

.group-actions {
  display: flex;
  gap: 10px;
}

.group-actions button {
  background: none;
  border: none;
  color: var(--accent);
  font-size: 0.75rem;
  cursor: pointer;
  padding: 2px;
}

.group-actions button:hover {
  text-decoration: underline;
}

.group-actions button.danger {
  color: var(--error);
}

@media (max-width: 768px) {
  .filter-select {
    flex: 1;
    min-width: calc(50% - 4px);
  }
}
</style>
