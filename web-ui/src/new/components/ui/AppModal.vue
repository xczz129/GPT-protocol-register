<script setup lang="ts">
/* --- 通用模态框：玻璃面板 + 标题栏 + 关闭按钮，插槽分发内容/底部 --- */
import { X } from "@lucide/vue";

withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    subtitle?: string;
    width?: string;
  }>(),
  { width: "560px", subtitle: "" },
);

const emit = defineEmits<{ close: [] }>();
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="modal-overlay" role="presentation" @click.self="emit('close')">
        <section class="modal glass-panel" role="dialog" aria-modal="true" :aria-label="title" :style="{ width: `min(${width}, 94vw)` }" @keyup.esc="emit('close')">
          <header class="modal-head">
            <div class="modal-title-wrap">
              <span class="modal-icon" aria-hidden="true"><slot name="icon" /></span>
              <div>
                <h3 class="modal-title">{{ title }}</h3>
                <small v-if="subtitle" class="modal-subtitle">{{ subtitle }}</small>
              </div>
            </div>
            <button class="modal-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')">
              <X :size="16" />
            </button>
          </header>
          <div class="modal-body">
            <slot />
          </div>
          <footer v-if="$slots.footer" class="modal-footer">
            <slot name="footer" />
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(30, 41, 59, 0.18);
  backdrop-filter: blur(6px);
}

.modal {
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  padding: 20px;
  animation: fade-in-up 0.25s ease;
}

.modal-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.modal-title-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}

.modal-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--accent-bg);
  color: var(--accent);
  flex-shrink: 0;
}

.modal-icon:empty {
  display: none;
}

.modal-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
}

.modal-subtitle {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.modal-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition-fast);
  flex-shrink: 0;
}

.modal-close:hover {
  background: var(--glass-bg-hover);
  color: var(--text-primary);
}

.modal-body {
  overflow-y: auto;
  flex: 1;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line-subtle);
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
</style>
