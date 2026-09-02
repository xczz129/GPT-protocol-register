<script setup>
import { nextTick, ref, watch } from "vue";

const props = defineProps({ dialog: { type: Object, required: true } });
const emit = defineEmits(["confirm", "cancel"]);
const inputValue = ref("");
const inputEl = ref(null);

watch(() => props.dialog.open, async (open) => {
  if (!open) return;
  inputValue.value = props.dialog.value || "";
  if (props.dialog.input) {
    await nextTick();
    inputEl.value?.focus();
    inputEl.value?.select();
  }
});

function submit() {
  if (props.dialog.input && !inputValue.value.trim()) return;
  emit("confirm", props.dialog.input ? inputValue.value.trim() : true);
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="dialog.open" class="app-dialog-mask" role="presentation" @click.self="emit('cancel')">
        <section class="app-dialog" role="dialog" aria-modal="true" :aria-label="dialog.title" @keyup.esc="emit('cancel')">
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
