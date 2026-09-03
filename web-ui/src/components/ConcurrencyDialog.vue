<script setup>
import { ref, watch } from "vue";
import { Save, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  title: { type: String, default: "并发设置" },
  message: { type: String, default: "" },
  value: { type: Number, default: 10 },
});
const emit = defineEmits(["confirm", "cancel", "save"]);

const input = ref(10);
const savedFlash = ref(false);

watch(() => props.open, (open) => {
  if (!open) return;
  input.value = Math.max(1, Math.min(20, Math.floor(Number(props.value) || 1)));
  savedFlash.value = false;
});

function submit() {
  emit("confirm", Math.max(1, Math.min(20, Math.floor(Number(input.value) || 1))));
}

function save() {
  const value = Math.max(1, Math.min(20, Math.floor(Number(input.value) || 1)));
  emit("save", value);
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
          <p>{{ message }}</p>
          <label class="concurrency-field">
            <span>并发数</span>
            <input v-model.number="input" type="number" min="1" max="20" @keyup.enter="submit" />
          </label>
          <div class="app-dialog-actions concurrency-actions">
            <button class="btn secondary concurrency-save" type="button" :title="'保存当前并发数为下次默认值'" @click="save"><Save :size="13" aria-hidden="true" />{{ savedFlash ? "已保存" : "保存默认" }}</button>
            <span class="concurrency-spacer"></span>
            <button class="btn secondary" type="button" @click="emit('cancel')">取消</button>
            <button class="btn primary" type="button" @click="submit">开始</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>