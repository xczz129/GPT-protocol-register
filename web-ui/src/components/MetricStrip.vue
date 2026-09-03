<script setup>
import { CheckCircle2, Mail, MailPlus, Network, Users, XCircle, Zap } from "@lucide/vue";

defineProps({ status: { type: Object, default: () => ({}) } });
</script>

<template>
  <section class="metrics" aria-label="运行概览">
    <article class="metric mail-ready">
      <div class="metric-name">
        <span>邮箱可用</span>
        <Mail :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value">{{ status.mail?.available ?? "-" }}</div>
    </article>

    <article class="metric alias-ready">
      <div class="metric-name">
        <span>别名邮箱</span>
        <MailPlus :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value">{{ status.mail?.split_available ?? "-" }}</div>
    </article>

    <article class="metric">
      <div class="metric-name">
        <span>邮箱总数</span>
        <Mail :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value">{{ status.mail?.total ?? "-" }}</div>
    </article>

    <article class="metric highlight">
      <div class="metric-name">
        <span>账号可用</span>
        <Users :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value">{{ status.accounts?.usable ?? "-" }}</div>
    </article>

    <article class="metric">
      <div class="metric-name">
        <span>已封号留档</span>
        <XCircle :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value">{{ status.accounts?.deactivated ?? "-" }}</div>
    </article>

    <article class="metric">
      <div class="metric-name">
        <span>本轮成功</span>
        <CheckCircle2 :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value is-ok">{{ status.job?.success_count ?? 0 }}</div>
    </article>

    <article class="metric">
      <div class="metric-name">
        <span>本轮失败</span>
        <XCircle :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value is-err">{{ status.job?.fail_count ?? 0 }}</div>
    </article>

    <article class="metric">
      <div class="metric-name">
        <span>代理状态</span>
        <Network :size="15" aria-hidden="true" />
      </div>
      <div class="metric-value is-small">{{ status.proxy?.enabled ? (status.proxy?.pool_count || 1) + " 条" : "关闭" }}</div>
    </article>
  </section>
</template>
