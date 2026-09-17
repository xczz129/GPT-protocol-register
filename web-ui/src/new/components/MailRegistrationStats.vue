<script setup lang="ts">
import { computed, ref } from "vue";
import {
  formatOtpSeconds, formatRegistrationRate,
  type MailRegistrationOutcome, type MailRegistrationRole, type MailRegistrationStats,
} from "../../shared/mailRegistration";

const props = defineProps<{ stats?: MailRegistrationStats | null; loading?: boolean }>();
const role = ref<"all" | MailRegistrationRole>("all");
const scopes = [
  { value: "all" as const, label: "全部账号" },
  { value: "primary" as const, label: "主号" },
  { value: "child" as const, label: "子号" },
];
const outcomes: { key: MailRegistrationOutcome; label: string; tone: string }[] = [
  { key: "success", label: "注册成功", tone: "success" },
  { key: "already_registered", label: "已被注册 · 跳过", tone: "muted" },
  { key: "otp_timeout", label: "未收到验证码", tone: "warning" },
  { key: "other_failed", label: "其他失败", tone: "danger" },
  { key: "cancelled", label: "取消", tone: "muted" },
];
const summary = computed(() => role.value === "all"
  ? props.stats
  : props.stats?.by_role?.find((item) => item.role === role.value));
const domainRows = computed(() => role.value === "all"
  ? props.stats?.by_domain || []
  : (props.stats?.groups || []).filter((item) => item.role === role.value));
const updatedTime = computed(() => {
  if (!props.stats?.updated_at) return "";
  const date = new Date(props.stats.updated_at);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { hour12: false });
});

function domainLabel(domain: string) {
  return domain === "gmail.com" ? "Gmail" : domain === "icloud.com" ? "iCloud" : domain;
}
</script>

<template>
  <section class="mail-registration-stats" aria-label="SMSBower 邮箱注册统计" :aria-busy="loading || false">
    <div class="mail-stats-head">
      <div>
        <h3>SMSBower 邮箱注册</h3>
        <p>按账号尝试累计，主号与子号各计一次；占比以已开始的尝试数为分母。</p>
      </div>
      <div class="mail-stats-scope" role="group" aria-label="统计账号范围">
        <button v-for="scope in scopes" :key="scope.value" type="button" :aria-pressed="role === scope.value" @click="role = scope.value">{{ scope.label }}</button>
      </div>
    </div>

    <p v-if="stats?.ok === false" class="mail-stats-empty is-danger" role="status">邮箱注册统计暂时无法读取，请刷新重试。</p>
    <p v-else-if="!stats" class="mail-stats-empty" role="status">{{ loading ? "正在加载邮箱注册统计…" : "暂无邮箱注册统计" }}</p>
    <template v-else>
      <div class="mail-stats-total">
        <strong>{{ summary?.totals.attempted ?? 0 }} <span>次尝试</span></strong>
        <span>进行中 {{ summary?.totals.in_progress ?? 0 }}</span>
        <small v-if="updatedTime">最近记录 {{ updatedTime }}</small>
      </div>

      <dl class="mail-outcomes">
        <div v-for="outcome in outcomes" :key="outcome.key" :class="`outcome-${outcome.tone}`">
          <dt>{{ outcome.label }}</dt>
          <dd><strong>{{ summary?.totals[outcome.key] ?? 0 }}</strong><span>{{ formatRegistrationRate(summary?.rates[outcome.key]?.value) }}</span></dd>
        </div>
      </dl>

      <div class="mail-otp-times">
        <div class="mail-otp-label"><b>收到验证码的用时</b><small>{{ summary?.otp_wait.received_count ?? 0 }} 次收到 · {{ summary?.otp_wait.timeout_count ?? 0 }} 次等码超时</small></div>
        <dl>
          <div><dt>平均</dt><dd>{{ formatOtpSeconds(summary?.otp_wait.average_seconds) }}</dd></div>
          <div><dt>最短</dt><dd>{{ formatOtpSeconds(summary?.otp_wait.min_seconds) }}</dd></div>
          <div><dt>最长</dt><dd>{{ formatOtpSeconds(summary?.otp_wait.max_seconds) }}</dd></div>
        </dl>
      </div>
      <p class="mail-stats-note">用时从开始等码到收到验证码，仅统计收到的样本，超时不计入平均；无样本显示 —。本功能启用后的记录会持续保留。</p>

      <div v-if="domainRows.length" class="mail-domain-table-wrap" tabindex="0" role="region" aria-label="按邮箱域名的注册与收码统计">
        <table class="mail-domain-table">
          <caption>按邮箱域名</caption>
          <thead><tr><th scope="col">邮箱</th><th scope="col">尝试</th><th scope="col">注册成功</th><th scope="col">已被注册</th><th scope="col">未收到验证码</th><th scope="col">平均 / 最短 / 最长收码</th></tr></thead>
          <tbody>
            <tr v-for="row in domainRows" :key="row.domain">
              <th scope="row">{{ domainLabel(row.domain) }}</th>
              <td>{{ row.totals.attempted }}</td>
              <td>{{ row.totals.success }} <small>{{ formatRegistrationRate(row.rates.success?.value) }}</small></td>
              <td>{{ row.totals.already_registered }} <small>{{ formatRegistrationRate(row.rates.already_registered?.value) }}</small></td>
              <td>{{ row.totals.otp_timeout }} <small>{{ formatRegistrationRate(row.rates.otp_timeout?.value) }}</small></td>
              <td>{{ formatOtpSeconds(row.otp_wait.average_seconds) }} / {{ formatOtpSeconds(row.otp_wait.min_seconds) }} / {{ formatOtpSeconds(row.otp_wait.max_seconds) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="mail-stats-empty">当前范围暂无注册尝试</p>
    </template>
  </section>
</template>

<style scoped>
.mail-registration-stats {
  --mail-stat-ink: var(--text-primary, var(--ink-800, #1d2936));
  --mail-stat-muted: var(--text-muted, var(--ink-500, #607080));
  --mail-stat-line: var(--line-subtle, var(--line, #dce2e8));
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid var(--mail-stat-line);
  color: var(--mail-stat-ink);
  font-size: 0.78rem;
}
.mail-stats-head { display: flex; justify-content: space-between; align-items: start; gap: 12px; flex-wrap: wrap; }
.mail-stats-head h3 { margin: 0; font-size: 0.9rem; font-weight: 650; }
.mail-stats-head p, .mail-stats-note { margin: 5px 0 0; color: var(--mail-stat-muted); font-size: 0.73rem; line-height: 1.6; }
.mail-stats-scope { display: flex; border: 1px solid var(--mail-stat-line); border-radius: 6px; overflow: hidden; }
.mail-stats-scope button { margin: 0; padding: 5px 12px; border: 0; border-radius: 0; background: transparent; color: var(--mail-stat-muted); cursor: pointer; font: inherit; white-space: nowrap; }
.mail-stats-scope button[aria-pressed="true"] { background: var(--accent-bg, #edf5f2); color: var(--accent, #127567); font-weight: 650; }
.mail-stats-scope button:focus-visible, .mail-domain-table-wrap:focus-visible { outline: 2px solid var(--accent, #127567); outline-offset: -2px; }
.mail-stats-total { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; padding: 14px 0 10px; font-variant-numeric: tabular-nums; }
.mail-stats-total strong { font-size: 1.25rem; }
.mail-stats-total strong span, .mail-stats-total > span { font-size: 0.76rem; color: var(--mail-stat-muted); font-weight: 400; }
.mail-stats-total small { margin-left: auto; color: var(--mail-stat-muted); font-size: 0.7rem; }
.mail-outcomes { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 0; }
.mail-outcomes > div { padding: 10px 12px; border: 1px solid var(--mail-stat-line); border-radius: 6px; }
.mail-outcomes dt { color: var(--mail-stat-muted); font-size: 0.72rem; }
.mail-outcomes dd { margin: 5px 0 0; display: flex; align-items: baseline; justify-content: space-between; gap: 6px; font-variant-numeric: tabular-nums; }
.mail-outcomes dd strong { font-size: 1.25rem; font-weight: 650; }
.mail-outcomes dd span { color: var(--mail-stat-muted); font-size: 0.72rem; }
.outcome-success dd strong { color: var(--success, #20745e); }
.outcome-warning dd strong { color: var(--warning, #9b661f); }
.outcome-danger dd strong, .is-danger { color: var(--error, #b74b49); }
.mail-otp-times { display: flex; align-items: center; gap: 20px; margin-top: 14px; padding: 12px 0; border-bottom: 1px solid var(--mail-stat-line); }
.mail-otp-label { display: flex; flex-direction: column; gap: 4px; }
.mail-otp-label small { color: var(--mail-stat-muted); font-size: 0.7rem; }
.mail-otp-times dl { display: flex; flex: 1; justify-content: flex-end; gap: clamp(20px, 5vw, 64px); margin: 0; }
.mail-otp-times dt { color: var(--mail-stat-muted); font-size: 0.7rem; }
.mail-otp-times dd { margin: 3px 0 0; font-size: 1rem; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.mail-domain-table-wrap { overflow-x: auto; margin-top: 12px; }
.mail-domain-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.74rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
.mail-domain-table caption { text-align: left; padding: 0 0 8px; color: var(--mail-stat-muted); font-size: 0.72rem; }
.mail-domain-table th, .mail-domain-table td { padding: 8px 12px 8px 0; border-bottom: 1px solid var(--mail-stat-line); }
.mail-domain-table th { font-weight: 550; }
.mail-domain-table thead th { color: var(--mail-stat-muted); font-size: 0.7rem; }
.mail-domain-table td small { color: var(--mail-stat-muted); margin-left: 4px; }
.mail-stats-empty { margin: 0; padding: 20px 0; color: var(--mail-stat-muted); text-align: center; }
.mail-stats-empty.is-danger { color: var(--error, #b74b49); }
@media (max-width: 760px) {
  .mail-outcomes { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .mail-otp-times { align-items: stretch; flex-direction: column; gap: 10px; }
  .mail-otp-times dl { justify-content: space-between; gap: 12px; }
  .mail-stats-total small { width: 100%; margin-left: 0; }
}
</style>
