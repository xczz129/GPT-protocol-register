<script setup>
import { computed, nextTick, ref, watch } from "vue";
import { KeyRound, Shuffle, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  count: { type: Number, default: 0 },
});
const emit = defineEmits(["confirm", "cancel"]);

const mode = ref("custom");
const newPassword = ref("");
const confirmPassword = ref("");
const submitted = ref(false);
const newPasswordInput = ref(null);

const normalizedPassword = computed(() => newPassword.value.trim());
const normalizedConfirmation = computed(() => confirmPassword.value.trim());
const customPasswordValid = computed(
  () => normalizedPassword.value.length >= 12
    && normalizedConfirmation.value === normalizedPassword.value,
);
// Keep the action available once the user has started typing so short or
// mismatched values produce an actionable validation message in the dialog.
const hasCustomInput = computed(() => !!normalizedPassword.value || !!normalizedConfirmation.value);
const canSubmit = computed(() => mode.value === "random"
  || hasCustomInput.value);
const validationMessage = computed(() => {
  if (mode.value !== "custom") return "";
  if (!submitted.value && !newPassword.value && !confirmPassword.value) return "";
  if (normalizedPassword.value.length < 12) return "新密码至少需要 12 个字符";
  if (!normalizedConfirmation.value) return "请再次输入新密码";
  if (normalizedConfirmation.value !== normalizedPassword.value) return "两次输入的新密码不一致";
  return "";
});

watch(() => props.open, async (open) => {
  if (!open) {
    // Do not retain a custom credential in the dialog after it closes.
    newPassword.value = "";
    confirmPassword.value = "";
    submitted.value = false;
    return;
  }
  mode.value = "custom";
  newPassword.value = "";
  confirmPassword.value = "";
  submitted.value = false;
  await nextTick();
  newPasswordInput.value?.focus();
});

watch(mode, async (value) => {
  submitted.value = false;
  if (value === "random") {
    newPassword.value = "";
    confirmPassword.value = "";
    return;
  }
  await nextTick();
  newPasswordInput.value?.focus();
});

function submit() {
  submitted.value = true;
  if (mode.value === "custom" && !customPasswordValid.value) return;
  if (mode.value === "random") {
    emit("confirm", { passwordMode: "random" });
    return;
  }
  emit("confirm", {
    passwordMode: "custom",
    newPassword: normalizedPassword.value,
    confirmPassword: normalizedConfirmation.value,
  });
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('cancel')">
        <section class="app-dialog password-change-dialog" role="dialog" aria-modal="true" aria-label="修改账号密码" @keyup.esc="emit('cancel')">
          <div class="app-dialog-head">
            <div><span class="app-dialog-kicker">账号安全</span><h2>修改账号密码</h2></div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('cancel')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <p>为 {{ count }} 个账号设置新密码。密码不会显示在进度记录里；成功后会保存到账号记录。</p>

          <div class="password-mode" role="group" aria-label="密码生成方式">
            <button type="button" :class="{ active: mode === 'random' }" :aria-pressed="mode === 'random'" @click="mode = 'random'"><Shuffle :size="14" aria-hidden="true" />随机生成</button>
            <button type="button" :class="{ active: mode === 'custom' }" :aria-pressed="mode === 'custom'" @click="mode = 'custom'"><KeyRound :size="14" aria-hidden="true" />自定义</button>
          </div>

          <div v-if="mode === 'random'" class="password-random-note">
            <Shuffle :size="17" aria-hidden="true" />
            <span><b>每个账号生成独立密码</b><small>后端将分别生成 16 字符强密码，完成后可通过“导出账号+密码+2FA”查看。</small></span>
          </div>
          <form v-else class="password-custom-form" @submit.prevent="submit">
            <label>新密码（至少 12 个字符）<input ref="newPasswordInput" v-model="newPassword" type="password" minlength="12" maxlength="512" autocomplete="new-password" /></label>
            <label>确认新密码<input v-model="confirmPassword" type="password" minlength="12" maxlength="512" autocomplete="new-password" /></label>
            <small class="password-custom-hint">自定义密码将应用到全部 {{ count }} 个账号。</small>
            <small v-if="validationMessage" class="password-dialog-error" role="alert">{{ validationMessage }}</small>
          </form>

          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="emit('cancel')">取消</button>
            <button class="btn primary" type="button" :disabled="!canSubmit" @click="submit">开始修改</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
