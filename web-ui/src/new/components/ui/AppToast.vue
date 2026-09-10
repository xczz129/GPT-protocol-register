<script setup lang="ts">
/* --- 全局通知（由 ui store 驱动） --- */
import { useUiStore } from "../../stores/ui";

const ui = useUiStore();
</script>

<template>
  <Teleport to="body">
    <div class="toast-container" aria-live="polite">
      <TransitionGroup name="toast">
        <div v-for="toast in ui.toasts" :key="toast.id" :class="['toast', toast.kind]">
          {{ toast.text }}
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-container {
  position: fixed;
  top: 18px;
  right: 18px;
  z-index: 200;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
}

.toast {
  min-width: 220px;
  max-width: 360px;
  padding: 11px 16px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg-strong);
  backdrop-filter: var(--glass-blur);
  border: 1px solid var(--glass-border);
  border-left: 3px solid var(--info);
  box-shadow: var(--shadow-md);
  font-size: 0.82rem;
  color: var(--text-primary);
  pointer-events: auto;
}

.toast.success { border-left-color: var(--success); }
.toast.error { border-left-color: var(--error); }
.toast.warning { border-left-color: var(--warning); }

.toast-enter-active,
.toast-leave-active {
  transition: all 0.25s ease;
}

.toast-enter-from {
  opacity: 0;
  transform: translateX(24px);
}

.toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
