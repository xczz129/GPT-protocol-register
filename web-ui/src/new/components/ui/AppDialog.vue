<script setup lang="ts">
/* --- 全局确认框（由 ui store 驱动） --- */
import { useUiStore } from "../../stores/ui";
import AppButton from "./AppButton.vue";

const ui = useUiStore();
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div v-if="ui.confirm.open" class="dialog-overlay" @click.self="ui.resolveConfirm(false)">
        <section class="dialog glass-panel" role="alertdialog" aria-modal="true">
          <h3 class="dialog-title">{{ ui.confirm.title || "确认操作" }}</h3>
          <p class="dialog-message">{{ ui.confirm.message }}</p>
          <label v-if="ui.confirm.requiredInput" class="dialog-input">
            <span>请输入“{{ ui.confirm.requiredInput }}”确认</span>
            <input
              v-model="ui.confirm.input"
              class="input"
              type="text"
              autocomplete="off"
              :spellcheck="false"
              :placeholder="ui.confirm.requiredInput"
            />
          </label>
          <div class="dialog-actions">
            <AppButton variant="ghost" @click="ui.resolveConfirm(false)">
              {{ ui.confirm.cancelText || "取消" }}
            </AppButton>
            <AppButton :variant="ui.confirm.danger ? 'danger' : 'primary'" :disabled="!ui.canConfirm" @click="ui.resolveConfirm(true)">
              {{ ui.confirm.confirmText || "确认" }}
            </AppButton>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-overlay {
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

.dialog {
  width: min(420px, 100%);
  padding: 22px;
  animation: fade-in-up 0.25s ease;
}

.dialog-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.dialog-message {
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.7;
  word-break: break-word;
  white-space: pre-wrap;
}

.dialog-input {
  display: grid;
  gap: 8px;
  margin-top: 16px;
  color: var(--text-secondary);
  font-size: 0.85rem;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}

.dialog-enter-active,
.dialog-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-enter-from,
.dialog-leave-to {
  opacity: 0;
}
</style>
