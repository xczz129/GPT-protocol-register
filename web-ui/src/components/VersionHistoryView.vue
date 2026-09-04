<script setup>
import { GitCommitHorizontal, RefreshCw, Upload } from "@lucide/vue";

defineProps({
  history: { type: Object, default: () => ({}) },
  busy: Boolean,
});
const emit = defineEmits(["refresh", "push"]);

function fmtCommit(msg) {
  const first = String(msg || "").split("\n")[0];
  return first.length > 120 ? `${first.slice(0, 120)}…` : first;
}
function repoUrl(remote) {
  return String(remote || "")
    .replace(/^git@github\.com:/, "https://github.com/")
    .replace(/\.git$/, "");
}
</script>

<template>
  <section class="card">
    <div class="card-h">
      <div>
        <h2><GitCommitHorizontal :size="16" aria-hidden="true" /> GitHub 版本历史</h2>
        <p class="subtle">面板代码的提交记录，与 GitHub 远程仓库同步。</p>
      </div>
      <div class="row-btns">
        <button class="btn secondary" :disabled="busy" @click="emit('push')">
          <Upload :size="14" aria-hidden="true" /> 推送到 GitHub
        </button>
        <button class="btn secondary" :disabled="busy" @click="emit('refresh')">
          <RefreshCw :size="14" :class="{ spin: busy }" aria-hidden="true" /> 刷新
        </button>
      </div>
    </div>

    <p v-if="history.remote" class="hint">
      远程仓库：<a :href="repoUrl(history.remote)" target="_blank" rel="noopener">{{ repoUrl(history.remote) }}</a>
      <template v-if="history.ahead != null">
        · 本地领先 {{ history.ahead }} 个提交
        <template v-if="history.behind"> · 落后 {{ history.behind }} 个提交</template>
        <template v-if="!history.ahead && !history.behind"> · 已同步</template>
      </template>
      <span v-if="history.uncommitted_changes" class="danger-text"> · 有未提交的本地改动</span>
    </p>

    <div class="ops-list version-list">
      <div v-if="busy && !history.commits?.length" class="empty-card">加载中…</div>
      <div v-else-if="!history.commits?.length" class="empty-card">暂无版本记录</div>
      <div v-for="(item, index) in history.commits || []" :key="item.hash" class="ops-row version-row">
        <span class="version-dot" :class="{ latest: index === 0 }" aria-hidden="true"></span>
        <div class="version-body">
          <b class="version-msg">{{ fmtCommit(item.message) }}</b>
          <small class="version-meta">
            <code class="version-hash">{{ item.short }}</code>
            {{ item.date }} · {{ item.author }}
            <span v-if="index === 0" class="version-tag">当前</span>
            <span v-else-if="history.ahead != null && index < history.ahead" class="version-tag push-pending">待推送</span>
          </small>
        </div>
      </div>
    </div>
  </section>
</template>
