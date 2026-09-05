<script setup>
import { nextTick, ref, watch } from "vue";

const props = defineProps({ dialog: { type: Object, required: true } });
const emit = defineEmits(["confirm", "cancel"]);
const inputValue = ref("");
const inputEl = ref(null);
const dialogEl = ref(null);
let returnFocusEl = null;

watch(() => props.dialog.open, async (open) => {
  if (!open) return;
  returnFocusEl = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  inputValue.value = props.dialog.value || "";
  await nextTick();
  if (props.dialog.input) {
    inputEl.value?.focus();
    inputEl.value?.select();
  } else {
    dialogEl.value?.focus();
  }
});

watch(() => props.dialog.open, (open, previous) => {
  if (open || previous !== true || !returnFocusEl) return;
  returnFocusEl.focus({ preventScroll: true });
  returnFocusEl = null;
});

function handleKeydown(event) {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("cancel");
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...dialogEl.value?.querySelectorAll(
    "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]",
  ) || []];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function submit() {
  if (props.dialog.input && !inputValue.value.trim()) return;
  emit("confirm", props.dialog.input ? inputValue.value.trim() : true);
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="dialog.open" class="app-dialog-mask" role="presentation" @click.self="emit('cancel')">
          <section ref="dialogEl" class="app-dialog" role="dialog" aria-modal="true" :aria-label="dialog.title" tabindex="-1" @keydown="handleKeydown">
          <div class="app-dialog-head">
            <div><span class="app-dialog-kicker">操作确认</span><h2>{{ dialog.title }}</h2></div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('cancel')">×</button>
          </div>
          <p>{{ dialog.message }}</p>
          <input v-if="dialog.input" ref="inputEl" v-model="inputValue" :type="dialog.inputType || 'text'" :autocomplete="dialog.inputType === 'password' ? 'new-password' : 'off'" :maxlength="dialog.maxlength || 80" @keyup.enter="submit" />
          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="emit('cancel')">取消</button>
            <button :class="['btn', dialog.danger ? 'danger' : 'primary']" type="button" :disabled="dialog.input && !inputValue.trim()" @click="submit">{{ dialog.confirmText || "确认" }}</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
