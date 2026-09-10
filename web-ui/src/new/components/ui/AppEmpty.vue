<script setup lang="ts">
defineProps<{
  title: string;
  hint?: string;
}>();
</script>

<template>
  <div class="empty-state">
    <slot name="icon" />
    <p class="empty-title">{{ title }}</p>
    <p v-if="hint" class="empty-hint">{{ hint }}</p>
    <slot />
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 20px;
  color: var(--text-muted);
  gap: 6px;
  text-align: center;
}

.empty-state :deep(svg) {
  opacity: 0.18;
  margin-bottom: 6px;
}

.empty-title {
  font-size: 0.85rem;
}

.empty-hint {
  font-size: 0.75rem;
}
</style>
