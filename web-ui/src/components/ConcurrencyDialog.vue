<script setup>
import { computed, ref, watch } from "vue";
import { Save, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  title: { type: String, default: "并发设置" },
  message: { type: String, default: "" },
  value: { type: Number, default: 10 },
  max: { type: Number, default: 20 },
  mode: { type: String, default: "protocol" },
  modeLabel: { type: String, default: "登录方式" },
  modeOptions: { type: Array, default: () => [] },
  modeMessages: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["confirm", "cancel", "save"]);

const input = ref(10);
const pickedMode = ref("protocol");
const savedFlash = ref(false);
const ceiling = computed(() => Math.max(1, Math.floor(Number(props.max) || 20)));
const hasMode = computed(() => Array.isArray(props.modeOptions) && props.modeOptions.length > 0);
const displayMessage = computed(() => {
  if (!hasMode.value) return props.message;
  const text = props.modeMessages?.[pickedMode.value];
  return typeof text === "string" && text ? text : props.message;
});

watch(() => props.open, (open) => {
  if (!open) return;
  input.value = Math.max(1, Math.min(ceiling.value, Math.floor(Number(props.value) || 1)));
  pickedMode.value = props.mode || props.modeOptions[0]?.value || "protocol";
  savedFlash.value = false;
});

function clamp(value) {
  return Math.max(1, Math.min(ceiling.value, Math.floor(Number(value) || 1)));
}

function submit() {
  emit("confirm", { workers: clamp(input.value), mode: pickedMode.value });
}

function save() {
  emit("save", { workers: clamp(input.value), mode: pickedMode.value });
  savedFlash.value = true;
  setTimeout(() => { savedFlash.value = false; }, 1500);
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('cancel')">
        <section class="app-dialog concurrency-dialog" role="dialog" aria-modal="true" :aria-label="title" @keyup.esc="emit('cancel')">
          <div class="app-dialog-head">
            <div><span class="app-dialog-kicker">并发设置</span><h2>{{ title }}</h2></div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('cancel')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <p>{{ displayMessage }}</p>
          <div v-if="hasMode" class="concurrency-mode" role="radiogroup" :aria-label="modeLabel">
            <span class="concurrency-mode-label">{{ modeLabel }}</span>
            <div class="seg-controls">
              <button v-for="opt in modeOptions" :key="opt.value" type="button" class="seg-btn" :class="{ active: pickedMode === opt.value }" @click="pickedMode = opt.value">{{ opt.label }}</button>
            </div>
          </div>
          <label class="concurrency-field">
            <span>并发数</span>
            <input v-model.number="input" type="number" min="1" :max="ceiling" @keyup.enter="submit" />
          </label>
          <div class="app-dialog-actions concurrency-actions">
            <button class="btn secondary concurrency-save" type="button" :title="'保存当前设置为下次默认值'" @click="save"><Save :size="13" aria-hidden="true" />{{ savedFlash ? "已保存" : "保存默认" }}</button>
            <span class="concurrency-spacer"></span>
            <button class="btn secondary" type="button" @click="emit('cancel')">取消</button>
            <button class="btn primary" type="button" @click="submit">开始</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
