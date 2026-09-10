<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: "primary" | "accent" | "ghost" | "danger";
    size?: "md" | "sm";
    full?: boolean;
    disabled?: boolean;
    loading?: boolean;
    type?: "button" | "submit";
  }>(),
  { variant: "ghost", size: "md", type: "button" },
);
</script>

<template>
  <button
    :type="type"
    :class="['btn', `btn-${variant}`, `btn-${size}`, { 'btn-full': full, 'is-loading': loading }]"
    :disabled="disabled || loading"
  >
    <span v-if="loading" class="btn-spinner" aria-hidden="true"></span>
    <slot v-else />
  </button>
</template>

<style scoped>
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 9px 18px;
  border-radius: var(--radius-sm);
  border: none;
  font-size: 0.82rem;
  font-weight: 500;
  cursor: pointer;
  transition: var(--transition);
  white-space: nowrap;
  outline: none;
  color: var(--text-secondary);
}

.btn:active { transform: scale(0.97); }

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none !important;
}

.btn-primary {
  background: linear-gradient(135deg, var(--accent-light), var(--accent));
  color: #fff;
  box-shadow: 0 2px 10px var(--accent-glow);
}

.btn-primary:hover {
  box-shadow: 0 4px 18px var(--accent-glow);
  transform: translateY(-1px);
}

.btn-accent {
  background: linear-gradient(135deg, var(--accent-2), #0284c7);
  color: #fff;
  box-shadow: 0 2px 10px var(--accent-2-glow);
}

.btn-accent:hover {
  box-shadow: 0 4px 18px var(--accent-2-glow);
  transform: translateY(-1px);
}

.btn-ghost {
  background: var(--glass-bg);
  color: var(--text-secondary);
  border: 1px solid var(--field-border);
}

.btn-ghost:hover {
  background: var(--glass-bg-hover);
  color: var(--text-primary);
}

.btn-danger {
  background: var(--error-bg);
  color: var(--error);
  border: 1px solid transparent;
}

.btn-danger:hover {
  border-color: var(--error);
}

.btn-sm {
  padding: 6px 12px;
  font-size: 0.75rem;
}

.btn-full { width: 100%; }

.btn-spinner {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  border: 2px solid currentColor;
  border-top-color: transparent;
  opacity: 0.8;
  animation: spin 0.7s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
