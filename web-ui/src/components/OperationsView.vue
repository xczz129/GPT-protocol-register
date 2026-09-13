<script setup>
import { computed, ref } from "vue";
import { apiUrl } from "../api.js";
import MailRegistrationStats from "./MailRegistrationStats.vue";

const props = defineProps({
  operations: { type: Object, default: () => ({}) },
  trash: { type: Array, default: () => [] },
  form: { type: Object, required: true },
  maddyAutoForm: { type: Object, required: true },
  cliproxyQuotaForm: { type: Object, required: true },
  backupScheduleForm: { type: Object, required: true },
  smsForm: { type: Object, required: true },
  smsConfig: { type: Object, default: () => ({}) },
  smsCountries: { type: Array, default: () => [] },
  smsStats: { type: Object, default: () => null },
  smsStatsLoading: Boolean,
  groups: { type: Array, default: () => [] },
  security: { type: Object, required: true },
  busy: Boolean,
  maintenanceRunning: Boolean,
  maddyAutoRunning: Boolean,
});
const emit = defineEmits(["refresh", "save", "run", "save-cliproxy-quota", "save-maddy-auto", "run-maddy-auto", "test-webhook", "backup", "save-backup-schedule", "save-sms-config", "test-sms", "pick-countries", "clear-docking-history", "restore-backup", "restore-trash", "change-password", "revoke-sessions", "refresh-sms-stats"]);
const dockingHistory = computed(() => {
  const list = (props.smsConfig && props.smsConfig.firefox_docking_code_history) || [];
  const cur = String(props.smsForm?.firefoxDockingCode || "").trim();
  const seen = new Set();
  const out = [];
  for (const item of list) {
    const code = String(item || "").trim();
    if (!code || seen.has(code)) continue;
    seen.add(code);
    out.push(code);
  }
  if (cur && !seen.has(cur)) out.unshift(cur);
  return out.slice(0, 20);
});
const selectedTrash = ref([]);
const canChangePassword = computed(() => props.security.currentPassword && props.security.newPassword.length >= 8 && props.security.newPassword === props.security.confirmPassword);
const successRate = computed(() => Math.max(0, Math.min(100, Number(props.operations.metrics?.success_rate || 0))));
const mailRate = computed(() => props.operations.mail?.total ? Math.round(Number(props.operations.mail.available || 0) * 100 / Number(props.operations.mail.total)) : 0);
const healthyAccounts = computed(() => Number(props.operations.accounts?.alive || 0) + Number(props.operations.accounts?.limited || 0));
const accountScope = computed(() => Math.max(0, Number(props.operations.accounts?.total || 0) - Number(props.operations.accounts?.deactivated || 0)));
const accountRate = computed(() => accountScope.value ? Math.round(healthyAccounts.value * 100 / accountScope.value) : 0);
const failureMax = computed(() => Math.max(1, ...(props.operations.metrics?.failure_reasons || []).map((item) => Number(item.count || 0))));
const smsStatsView = ref("country");
const smsStatsSummary = computed(() => props.smsStats?.summary || { total: 0, success: 0, failed: 0, rate: 0 });
const smsStatsRows = computed(() => {
  const source = smsStatsView.value === "tier" ? props.smsStats?.tiers : props.smsStats?.countries;
  return (source || []).map((row) => ({
    name: row.name,
    total: Number(row.total || 0),
    ok: Number(row.ok || 0),
    fail: Number(row.fail || 0),
    rate: Number(row.rate || 0),
    cost: Number(row.cost || 0),
    price: Number(row.price || 0),
  }));
});
const smsStatsMaxTotal = computed(() => Math.max(1, ...smsStatsRows.value.map((row) => row.total)));
function smsRateLevel(rate) {
  if (rate >= 20) return "good";
  if (rate >= 8) return "mid";
  return "low";
}
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
          <div class="failure-chart-title"><b>失败原因排行</b><span class="failure-total">{{ operations.metrics?.failed || 0 }} 次失败</span></div>
          <div v-if="!operations.metrics?.failure_reasons?.length" class="dashboard-empty">暂无累计失败明细</div>
          <ol v-else class="failure-list">
            <li v-for="(item, rank) in operations.metrics?.failure_reasons || []" :key="item.reason" class="failure-item">
              <span class="failure-rank">{{ rank + 1 }}</span>
              <span class="failure-reason" :title="item.reason">{{ item.reason }}</span>
              <b class="failure-count">{{ item.count }}</b>
              <div class="failure-track"><i :style="{ width: `${Math.max(4, item.count * 100 / failureMax)}%` }"></i></div>
            </li>
          </ol>
        </div>

        <div class="failure-chart sms-stats-dashboard">
          <div class="failure-chart-title">
            <b>接码成功率</b>
            <span class="failure-total">{{ smsStatsSummary.success }} 通过 / {{ smsStatsSummary.total }} 取号</span>
          </div>
          <div class="sms-stats-toolbar">
            <div class="seg-controls sms-stats-seg">
              <button type="button" class="seg-btn" :class="{ active: smsStatsView === 'country' }" @click="smsStatsView = 'country'">按国家</button>
              <button type="button" class="seg-btn" :class="{ active: smsStatsView === 'tier' }" @click="smsStatsView = 'tier'">按档位</button>
            </div>
            <strong :class="['sms-stats-overall', `rate-${smsRateLevel(smsStatsSummary.rate)}`]">{{ smsStatsSummary.rate }}%</strong>
            <button class="btn secondary sm" type="button" :disabled="smsStatsLoading" @click="emit('refresh-sms-stats')">{{ smsStatsLoading ? "统计中…" : "刷新" }}</button>
          </div>
          <div v-if="!smsStatsRows.length" class="dashboard-empty">{{ smsStatsLoading ? "统计中…" : "暂无接码记录" }}</div>
          <div v-else class="sms-stats-list">
            <div v-for="(row, index) in smsStatsRows" :key="row.name" class="sms-stats-row">
              <span class="sms-stats-rank">{{ index + 1 }}</span>
              <div class="sms-stats-main">
                <div class="sms-stats-head">
                  <b class="sms-stats-name" :title="row.name">{{ row.name }}</b>
                  <span class="sms-stats-nums">{{ row.ok }}/{{ row.total }}</span>
                  <strong :class="['sms-stats-rate', `rate-${smsRateLevel(row.rate)}`]">{{ row.rate }}%</strong>
                </div>
                <div class="sms-stats-track"><i :style="{ width: `${Math.max(2, row.total * 100 / smsStatsMaxTotal)}%` }"></i></div>
                <small class="sms-stats-meta">拒 {{ row.fail }} · ${{ row.cost.toFixed(3) }}<template v-if="row.price"> · 单价 ${{ row.price.toFixed(3) }}</template></small>
              </div>
            </div>
          </div>
        </div>
      </div>
      <MailRegistrationStats :stats="operations.mail_registration" :loading="busy" />
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
      <p class="hint">最近测活：{{ formatTime(operations.state?.last_health_at) }}</p>
    </section>

    <section class="card">
          <section class="card">
      <div class="card-h"><div><h2>中转账号额度</h2><p class="subtle">后台定时刷新中转账号（cliproxy）的额度缓存，无需手动加载。</p></div></div>
      <div class="form two">
        <label class="check"><input v-model="cliproxyQuotaForm.enabled" type="checkbox" />启用自动刷新</label>
        <label>刷新间隔（秒）<input v-model.number="cliproxyQuotaForm.intervalSeconds" type="number" min="30" max="3600" /></label>
      </div>
      <div class="actions"><button class="primary" :disabled="busy" @click="emit('save-cliproxy-quota')">保存额度刷新设置</button></div>
      <p class="hint">保存后后台任务按新间隔生效；间隔下限 30 秒，上限 3600 秒。</p>
    </section>

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
      <div class="card-h"><div><h2>接码设置</h2><p class="subtle">Codex OAuth 补发 RT 用的手机验证接码渠道。</p></div></div>
      <div class="form two">
        <label class="check"><input v-model="smsForm.enabled" type="checkbox" />启用自动接码</label>
        <label>接码渠道<select v-model="smsForm.provider"><option value="smsbower">SMSBower</option><option value="firefox">火狐狸 Firefox</option></select></label>
        <template v-if="smsForm.provider !== 'firefox'">
          <label class="full">SMSBower API Key<input v-model="smsForm.apiKey" type="password" :placeholder="smsConfig.has_api_key ? `已保存 ${smsConfig.api_key_masked}` : 'smsbower API key'" autocomplete="off" /></label>
        </template>
        <template v-else>
          <label class="full">火狐狸用户名(ApiName)<input v-model="smsForm.firefoxApiName" type="text" placeholder="ApiName" autocomplete="off" /></label>
          <label class="full">火狐狸 Token(我的密钥)<input v-model="smsForm.firefoxToken" type="password" :placeholder="smsConfig.has_firefox_token ? `已保存 ${smsConfig.firefox_api_token_masked}` : 'firefox token'" autocomplete="off" /></label>
          <label class="full">对接码(一次锁定国家+项目)<input v-model="smsForm.firefoxDockingCode" type="text" placeholder="格式: eng-1096-DEMO01(在火狐狸接码页复制)" autocomplete="off" />
            <div v-if="dockingHistory.length" class="sms-country-field"><div class="sms-country-chips"><button v-for="code in dockingHistory" :key="code" type="button" class="sms-country-chip dock-chip" :class="{ active: code === smsForm.firefoxDockingCode }" :title="'使用 ' + code" @click="smsForm.firefoxDockingCode = code">{{ code }}</button></div><div class="dock-actions"><small class="dock-hint">点击历史对接码快速填入</small><button class="btn secondary sm" type="button" :disabled="busy" @click="emit('clear-docking-history')">清空历史</button></div></div>
          </label>
        </template>
        <label v-if="smsForm.provider !== 'firefox'" class="full">国家顺序(按顺序取号)<div class="sms-country-field"><div v-if="smsCountries.length" class="sms-country-chips"><span v-for="(name, index) in smsCountries" :key="name + index" class="sms-country-chip">{{ index + 1 }}. {{ name }}</span></div><span v-else class="sms-country-none">未选择</span><button class="btn secondary sm" type="button" @click="emit('pick-countries')">选择国家</button></div></label>
        <label>等码超时(秒)<input v-model.number="smsForm.timeoutSeconds" type="number" min="30" max="1800" /></label>
        <label>取号次数上限（0 为不限）<input v-model.number="smsForm.maxPhoneAttempts" type="number" min="0" step="1" /><small>同号码重建会话不增加次数，可随时停止。</small></label>
      </div>
      <div class="actions"><button class="primary" :disabled="busy" @click="emit('save-sms-config')">保存接码设置</button><button class="btn secondary" :disabled="busy" @click="emit('test-sms')">查看余额</button></div>
      <p class="hint">{{ smsConfig.configured ? (smsConfig.enabled ? `已启用 ${smsConfig.provider_label || ''}` : `已配置 ${smsConfig.provider_label || ''} 但未启用`) : "未配置接码凭证" }}</p>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>备份与恢复</h2><p class="subtle">备份数据库、配置、分组和邮箱状态。</p></div><button class="btn primary" :disabled="busy" @click="emit('backup')">立即备份</button></div>
      <div class="form two">
        <label class="check"><input v-model="backupScheduleForm.enabled" type="checkbox" />启用定时备份</label>
        <label>每天备份时间<input v-model="backupScheduleForm.time" type="time" /></label>
        <label>保留天数<input v-model.number="backupScheduleForm.retentionDays" type="number" min="1" max="90" /></label>
      </div>
      <div class="actions"><button class="primary" :disabled="busy" @click="emit('save-backup-schedule')">保存定时设置</button></div>
      <p class="hint">定时:{{ backupScheduleForm.enabled ? `每天 ${backupScheduleForm.time}` : "未启用" }} · 保留 {{ backupScheduleForm.retentionDays }} 天 · 最近自动备份:{{ formatTime(operations.state?.backup_last_at) }}<template v-if="operations.state?.backup_last_error"> · <span class="danger-text">{{ operations.state.backup_last_error }}</span></template></p>
      <div class="ops-list"><div v-if="!operations.backups?.length" class="empty-card">暂无备份</div><div v-for="item in operations.backups" :key="item.name" class="ops-row"><div><b>{{ item.name }}<em v-if="item.auto" class="backup-auto-tag">自动</em></b><small>{{ formatTime(item.created_at) }} · {{ formatSize(item.size) }}</small></div><div class="row-btns"><a class="btn secondary sm" :href="apiUrl(`/api/backups/${encodeURIComponent(item.name)}`)">下载</a><button class="btn danger sm" :disabled="busy" @click="emit('restore-backup', item.name)">恢复</button></div></div></div>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>账号回收站</h2><p class="subtle">网页删除的账号可在这里恢复，共 {{ trash.length }} 条。</p></div></div>
      <div class="ops-list trash-list"><div v-if="!trash.length" class="empty-card">回收站为空</div><label v-for="item in trash.slice(0, 50)" :key="item.trash_id" class="ops-row selectable trash-row"><input v-model="selectedTrash" type="checkbox" :value="item.trash_id" /><div class="trash-info"><b>{{ item.account?.email || item.account?.account_id || '未知账号' }}</b><small><span class="trash-reason">{{ item.reason }}</span><span class="trash-time">{{ formatTime(item.deleted_at) }}</span></small></div></label></div>
      <div class="actions"><button class="btn secondary" :disabled="!selectedTrash.length || busy" @click="emit('restore-trash', selectedTrash); selectedTrash = []">恢复选中</button></div>
    </section>

    <section class="card">
      <div class="card-h"><div><h2>面板安全</h2><p class="subtle">登录失败自动限速；修改密码会使其他会话失效。</p></div></div>
      <div class="form"><label>当前密码<input v-model="security.currentPassword" type="password" autocomplete="current-password" /></label><label>新密码<input v-model="security.newPassword" type="password" minlength="8" autocomplete="new-password" /></label><label>确认新密码<input v-model="security.confirmPassword" type="password" minlength="8" autocomplete="new-password" /></label></div>
      <div class="actions"><button class="primary" :disabled="!canChangePassword || busy" @click="emit('change-password')">修改密码</button><button class="btn danger" :disabled="busy" @click="emit('revoke-sessions')">退出其他会话</button></div>
    </section>
  </div>
</template>
