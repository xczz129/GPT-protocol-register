<script setup>
import { CheckCircle2, Mail, MailPlus, Network, Users, XCircle } from "@lucide/vue";

defineProps({ status: { type: Object, default: () => ({}) } });
</script>

<template>
  <section class="metrics" aria-label="运行概览">
    <article class="metric mail-ready"><span class="metric-icon"><Mail :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.mail?.available ?? "-" }}</div><div class="label">邮箱可用</div></div></article>
    <article class="metric alias-ready"><span class="metric-icon"><MailPlus :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.mail?.split_available ?? "-" }}</div><div class="label">别名邮箱可用</div></div></article>
    <article class="metric gray"><span class="metric-icon"><Mail :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.mail?.total ?? "-" }}</div><div class="label">邮箱总数</div></div></article>
    <article class="metric"><span class="metric-icon"><Users :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.accounts?.usable ?? "-" }}</div><div class="label">账号可用</div><small class="metric-note">记录 {{ status.accounts?.total ?? status.accounts_count ?? "-" }}</small></div></article>
    <article class="metric gray"><span class="metric-icon"><XCircle :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.accounts?.deactivated ?? "-" }}</div><div class="label">已封号留档</div></div></article>
    <article class="metric blue"><span class="metric-icon"><CheckCircle2 :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.job?.success_count ?? 0 }}</div><div class="label">本轮成功</div></div></article>
    <article class="metric red"><span class="metric-icon"><XCircle :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.job?.fail_count ?? 0 }}</div><div class="label">本轮失败</div></div></article>
    <article class="metric gray"><span class="metric-icon"><XCircle :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.job?.skipped_count ?? 0 }}</div><div class="label">本轮跳过</div></div></article>
    <article class="metric gray"><span class="metric-icon"><Network :size="17" aria-hidden="true" /></span><div><div class="num">{{ status.proxy?.enabled ? (status.proxy?.pool_count || 1) + " 条" : "关闭" }}</div><div class="label">代理</div></div></article>
  </section>
</template>
