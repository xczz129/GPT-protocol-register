<script setup>
import { nextTick, ref, watch } from "vue";
import { ClipboardCopy, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  title: { type: String, default: "复制内容" },
  value: { type: String, default: "" },
});
const emit = defineEmits(["close", "copied"]);
const textArea = ref(null);
const copyError = ref("");

function selectValue() {
  textArea.value?.focus();
  textArea.value?.select();
  textArea.value?.setSelectionRange(0, props.value.length);
}

watch(() => props.open, async (open) => {
  copyError.value = "";
  if (!open) return;
  await nextTick();
  selectValue();
});

async function copyValue() {
  if (!props.value) return;
  selectValue();
  try {
    if (document.execCommand("copy")) {
      emit("copied");
      return;
    }
  } catch {}
  try {
    await navigator.clipboard.writeText(props.value);
    emit("copied");
  } catch {
    copyError.value = "剪贴板权限未开启";
    await nextTick();
    selectValue();
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('close')">
        <section class="app-dialog clipboard-dialog" role="dialog" aria-modal="true" :aria-label="title" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div><span class="app-dialog-kicker">复制内容</span><h2>{{ title }}</h2></div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <textarea ref="textArea" :value="value" readonly spellcheck="false" @focus="selectValue"></textarea>
          <p v-if="copyError" class="clipboard-dialog-error">{{ copyError }}</p>
          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="emit('close')">关闭</button>
            <button class="btn primary" type="button" :disabled="!value" @click="copyValue"><ClipboardCopy :size="15" aria-hidden="true" />复制</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
