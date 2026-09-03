<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  operations: { type: Object, default: () => ({}) },
  trash: { type: Array, default: () => [] },
  form: { type: Object, required: true },
  maddyAutoForm: { type: Object, required: true },
  groups: { type: Array, default: () => [] },
  security: { type: Object, required: true },
  busy: Boolean,
  maintenanceRunning: Boolean,
  maddyAutoRunning: Boolean,
});
const emit = defineEmits(["refresh", "save", "run", "save-maddy-auto", "run-maddy-auto", "test-webhook", "backup", "restore-backup", "create-account-backup", "restore-account-backup", "delete-account-backup", "restore-trash", "change-password", "revoke-sessions"]);
const selectedTrash = ref([]);
const accountBackup = ref({ groupId: "", status: "", restoreMode: "merge" });
const canChangePassword = computed(() => props.security.currentPassword && props.security.newPassword.length >= 8 && props.security.newPassword === props.security.confirmPassword);
const successRate = computed(() => Math.max(0, Math.min(100, Number(props.operations.metrics?.success_rate || 0))));
const mailRate = computed(() => props.operations.mail?.total ? Math.round(Number(props.operations.mail.available || 0) * 100 / Number(props.operations.mail.total)) : 0);
const healthyAccounts = computed(() => Number(props.operations.accounts?.alive || 0) + Number(props.operations.accounts?.limited || 0));
const accountScope = computed(() => Math.max(0, Number(props.operations.accounts?.total || 0) - Number(props.operations.accounts?.deactivated || 0)));
const accountRate = computed(() => accountScope.value ? Math.round(healthyAccounts.value * 100 / accountScope.value) : 0);
const failureMax = computed(() => Math.max(1, ...(props.operations.metrics?.failure_reasons || []).map((item) => Number(item.count || 0))));
const maddyAuto = computed(() => props.operations.maddy_auto_register || {});
const maddyDomainsText = computed(() => (maddyAuto.value.domains || []).join("、") || "-");
function formatSize(value) { const size = Number(value || 0); return size > 1048576 ? `${(size / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`; }
function formatTime(value) { if (!value) return "-"; try { const date = new Date(value); return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false }); } catch { return "-"; } }
</script>

<template>
  <div class="ops-grid">
    <section class="card span-all">
      <div class="card-h"><div><h2>运行统计</h2><p class="subtle">累计保存所有注册批次的任务结果和资源状态。</p></div><button class="btn secondary" :disabled="busy" @click="emit('refresh')">刷新</button></div>
      <div class="dashboard-grid">
        <div class="dashboard-score">
          <div class="score-ring" :style="{ '--score': `${successRate * 3.6}deg` }"><div><strong>{{ successRate }}%</strong><span>成功率</span></div></div>
          <div class="score-caption"><b>{{ operations.metrics?.success || 0 }} 成功</b><span>{{ operations.metrics?.failed || 0 }} 失败 · 平均 {{ operations.metrics?.average_seconds || 0 }}s</span></div>
        </div>
        <div class="dashboard-resources">
          <div class="resource-progress"><div><b>邮箱可用率</b><span>{{ operations.mail?.available || 0 }} / {{ operations.mail?.total || 0 }}</span></div><div class="resource-track"><i :style="{ width: `${mailRate}%` }"></i></div><small>已使用 {{ operations.mail?.used || 0 }} · 冷却 {{ operations.mail?.cooldown || 0 }} · 失败 {{ operations.mail?.failed || 0 }}</small></div>
          <div class="resource-progress account-health"><div><b>账号健康率</b><span>{{ healthyAccounts }} / {{ accountScope }}</span></div><div class="resource-track"><i :style="{ width: `${accountRate}%` }"></i></div><small>正常 {{ operations.accounts?.alive || 0 }} · 限流 {{ operations.accounts?.limited || 0 }} · 待续期 {{ operations.accounts?.reauth_required || 0 }} · 其它失效 {{ operations.accounts?.invalid_other || 0 }} · 错误 {{ operations.accounts?.error || 0 }} · 已封号留档 {{ operations.accounts?.deactivated || 0 }} · 未检测 {{ operations.accounts?.unknown || 0 }}</small></div>
          <div class="dashboard-kpis"><div><strong>{{ operations.mail?.available ?? 0 }}</strong><span>可用邮箱</span></div><div><strong>{{ operations.accounts?.invalid_other ?? 0 }}</strong><span>其它失效账号</span></div><div><strong>{{ operations.trash_count ?? 0 }}</strong><span>回收站</span></div></div>
        </div>
        <div class="failure-chart">
          <div class="failure-chart-title"><b>失败原因排行</b><span>{{ operations.metrics?.failed || 0 }} 次失败</span></div>
          <div v-if="!operations.metrics?.failure_reasons?.length" class="dashboard-empty">暂无累计失败明细</div>
          <div v-for="item in operations.metrics?.failure_reasons || []" :key="item.reason" class="failure-bar"><div><span>{{ item.reason }}</span><b>{{ item.count }}</b></div><div class="failure-track"><i :style="{ width: `${Math.max(8, item.count * 100 / failureMax)}%` }"></i></div></div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>自动维护</h2><p class="subtle">定时测活、清理失效账号和资源预警。</p></div></div>
      <div class="form two">
        <label class="check"><input v-model="form.enabled" type="checkbox" />启用定时测活</label>
        <label class="check"><input v-model="form.autoDeleteInvalid" type="checkbox" />自动移入回收站</label>
        <label>检测间隔（小时）<input v-model.number="form.intervalHours" type="number" min="1" max="168" /></label>
        <label>测活并发<input v-model.number="form.workers" type="number" min="1" max="20" /></label>
        <label class="full">Webhook<input v-model="form.webhookUrl" type="password" :placeholder="operations.maintenance?.has_webhook ? `已保存 ${operations.maintenance.webhook_masked}` : 'https://...'" autocomplete="off" /></label>
        <label class="check"><input v-model="form.notifyOnComplete" type="checkbox" />任务完成通知</label>
        <label class="check"><input v-model="form.notifyOnPoolLow" type="checkbox" />邮箱不足通知</label>
        <label>邮箱预警阈值<input v-model.number="form.poolLowThreshold" type="number" min="0" /></label>
      </div>
      <div class="actions"><button class="primary" :disabled="busy" @click="emit('save')">保存维护设置</button><button class="btn secondary" :disabled="busy || maintenanceRunning" @click="emit('run')">{{ maintenanceRunning ? "测活进行中…" : "立即测活" }}</button><button class="btn secondary" :disabled="busy" @click="emit('test-webhook')">测试 Webhook</button></div>
      <p class="hint">最近测活：{{ formatTime(operations.state?.last_health_at) }} · CDK 替换 {{ operations.state?.last_health_result?.cdk_replaced || 0 }} · 待补位 {{ operations.state?.last_health_result?.cdk_unresolved || 0 }}</p>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>Maddy 自动注册</h2><p class="subtle">按域名轮流启动独立注册批次。</p></div></div>
      <div class="form two">
        <label class="check"><input v-model="maddyAutoForm.enabled" type="checkbox" />启用</label>
        <label class="check"><input v-model="maddyAutoForm.refreshDomains" type="checkbox" />自动刷新域名</label>
        <label>间隔（小时）<input v-model.number="maddyAutoForm.intervalHours" type="number" min="1" max="168" /></label>
        <label>每域名数量<input v-model.number="maddyAutoForm.perDomain" type="number" min="1" max="500" /></label>
        <label>注册并发<input v-model.number="maddyAutoForm.concurrency" type="number" min="1" max="20" /></label>
        <label>注册引擎<select v-model="maddyAutoForm.engine"><option value="protocol">Protocol</option><option value="hybrid">Hybrid</option><option value="browser">Browser</option></select></label>
        <label class="check"><input v-model="maddyAutoForm.keepOnFail" type="checkbox" />失败保留浏览器</label>
        <label>目标分组<select v-model="maddyAutoForm.groupId"><option value="">默认分组</option><option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}</option></select></label>
      </div>
      <div class="actions"><button class="primary" :disabled="busy" @click="emit('save-maddy-auto')">保存 Maddy 策略</button><button class="btn secondary" :disabled="busy || maddyAutoRunning" @click="emit('run-maddy-auto')">{{ maddyAutoRunning ? "执行中…" : "立即触发" }}</button></div>
      <p class="hint">域名：{{ maddyDomainsText }} · 当前：{{ maddyAuto.current_domain || "-" }} · 最近：{{ maddyAuto.last_domain || "-" }} / {{ formatTime(maddyAuto.last_at) }}</p>
      <p v-if="maddyAuto.last_error" class="hint danger-text">最近错误：{{ maddyAuto.last_error }}</p>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>备份与恢复</h2><p class="subtle">备份配置、账号、分组和邮箱状态。</p></div><button class="btn primary" :disabled="busy" @click="emit('backup')">创建备份</button></div>
      <div class="ops-list"><div v-if="!operations.backups?.length" class="empty-card">暂无备份</div><div v-for="item in operations.backups" :key="item.name" class="ops-row"><div><b>{{ item.name }}</b><small>{{ formatTime(item.created_at) }} · {{ formatSize(item.size) }}</small></div><div class="row-btns"><a class="btn secondary sm" :href="`/api/backups/${encodeURIComponent(item.name)}`">下载</a><button class="btn danger sm" :disabled="busy" @click="emit('restore-backup', item.name)">恢复</button></div></div></div>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>账号备份</h2><p class="subtle">仅备份账号数据（含登录凭据），可按分组或状态筛选，保留最近 20 份。</p></div><button class="btn primary" :disabled="busy" @click="emit('create-account-backup', { groupId: accountBackup.groupId, status: accountBackup.status })">创建账号备份</button></div>
      <div class="form two">
        <label>分组范围<select v-model="accountBackup.groupId"><option value="">全部分组</option><option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}</option></select></label>
        <label>状态筛选<select v-model="accountBackup.status"><option value="">全部状态</option><option value="正常">正常</option><option value="已封号">已封号</option><option value="异常">异常</option></select></label>
      </div>
      <div class="actions backup-mode-row">
        <span class="backup-mode-label">恢复模式</span>
        <button type="button" :class="['btn', 'sm', accountBackup.restoreMode === 'merge' ? 'primary' : 'secondary']" :disabled="busy" @click="accountBackup.restoreMode = 'merge'">合并（只补缺）</button>
        <button type="button" :class="['btn', 'sm', accountBackup.restoreMode === 'replace' ? 'primary' : 'secondary']" :disabled="busy" @click="accountBackup.restoreMode = 'replace'">覆盖全部</button>
      </div>
      <div class="ops-list"><div v-if="!operations.account_backups?.length" class="empty-card">暂无账号备份</div><div v-for="item in operations.account_backups" :key="item.name" class="ops-row"><div><b>{{ item.name }}</b><small>{{ formatTime(item.created_at) }} · {{ formatSize(item.size) }} · {{ item.count || 0 }} 个账号</small></div><div class="row-btns"><a class="btn secondary sm" :href="`/api/account-backups/${encodeURIComponent(item.name)}`">下载</a><button class="btn secondary sm" :disabled="busy" @click="emit('restore-account-backup', { name: item.name, mode: accountBackup.restoreMode })">恢复</button><button class="btn danger sm" :disabled="busy" @click="emit('delete-account-backup', item.name)">删除</button></div></div></div>
      <p class="hint">合并恢复只补充当前缺少的账号，不改动现有账号；覆盖恢复会替换全部账号数据。备份文件包含访问凭据，请妥善保管。</p>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>账号回收站</h2><p class="subtle">网页删除的账号可在这里恢复。</p></div></div>
      <div class="ops-list"><div v-if="!trash.length" class="empty-card">回收站为空</div><label v-for="item in trash.slice(0, 50)" :key="item.trash_id" class="ops-row selectable"><input v-model="selectedTrash" type="checkbox" :value="item.trash_id" /><div><b>{{ item.account?.email || item.account?.account_id || '未知账号' }}</b><small>{{ item.reason }} · {{ formatTime(item.deleted_at) }}</small></div></label></div>
      <div class="actions"><button class="btn secondary" :disabled="!selectedTrash.length || busy" @click="emit('restore-trash', selectedTrash); selectedTrash = []">恢复选中</button></div>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>面板安全</h2><p class="subtle">登录失败自动限速；修改密码会使其他会话失效。</p></div></div>
      <div class="form"><label>当前密码<input v-model="security.currentPassword" type="password" autocomplete="current-password" /></label><label>新密码<input v-model="security.newPassword" type="password" minlength="8" autocomplete="new-password" /></label><label>确认新密码<input v-model="security.confirmPassword" type="password" minlength="8" autocomplete="new-password" /></label></div>
      <div class="actions"><button class="primary" :disabled="!canChangePassword || busy" @click="emit('change-password')">修改密码</button><button class="btn danger" :disabled="busy" @click="emit('revoke-sessions')">退出其他会话</button></div>
    </section>
  </div>
</template>
