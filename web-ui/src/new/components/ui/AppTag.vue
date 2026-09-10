<script setup lang="ts">
withDefaults(
  defineProps<{
    kind?: "default" | "success" | "warning" | "error" | "info";
    dot?: boolean;
  }>(),
  { kind: "default" },
);
</script>

<template>
  <span :class="['tag', `tag-${kind}`]">
    <i v-if="dot" class="status-dot" aria-hidden="true"></i>
    <slot />
  </span>
</template>

<style scoped>
.tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 11px;
  border-radius: 20px;
  font-size: 0.75rem;
  font-weight: 500;
  background: var(--accent-bg);
  color: var(--accent);
  white-space: nowrap;
}

.tag-success { background: var(--success-bg); color: var(--success); }
.tag-warning { background: var(--warning-bg); color: var(--warning); }
.tag-error { background: var(--error-bg); color: var(--error); }
.tag-info { background: var(--info-bg); color: var(--info); }
</style>
