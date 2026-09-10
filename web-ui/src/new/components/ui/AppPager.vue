<script setup lang="ts">
/* --- 分页器 --- */
defineProps<{
  page: number;
  pages: number;
  total: number;
}>();

const emit = defineEmits<{
  "update:page": [page: number];
}>();
</script>

<template>
  <div class="pager">
    <button class="pager-btn" type="button" :disabled="page <= 1" @click="emit('update:page', page - 1)">上一页</button>
    <span class="pager-info">第 {{ total ? page : 0 }} / {{ pages }} 页 · 共 {{ total }} 条</span>
    <button class="pager-btn" type="button" :disabled="page >= pages" @click="emit('update:page', page + 1)">下一页</button>
  </div>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 12px 0 4px;
}

.pager-btn {
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--glass-bg);
  color: var(--text-secondary);
  font-size: 0.78rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.pager-btn:hover:not(:disabled) {
  background: var(--glass-bg-hover);
  color: var(--text-primary);
}

.pager-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.pager-info {
  font-size: 0.75rem;
  color: var(--text-muted);
}
</style>
