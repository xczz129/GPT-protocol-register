<script setup>
import { computed, ref } from "vue";
import { Upload, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  busy: Boolean,
});
const emit = defineEmits(["close", "submit"]);

const text = ref("");

const lines = computed(() => String(text.value || "").split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith("#")));
const valid = computed(() => lines.value.filter((line) => {
  const parts = line.split("----");
  return parts.length === 3 && parts.every((part) => part.trim());
}));
const invalidCount = computed(() => lines.value.length - valid.value.length);

function submit() {
  if (!valid.value.length) return;
  emit("submit", text.value);
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('close')">
        <section class="app-dialog import-accounts-dialog" role="dialog" aria-modal="true" aria-label="导入账号" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div>
              <span class="app-dialog-kicker"><Upload :size="12" aria-hidden="true" />导入账号</span>
              <h2>邮箱----密码----2FA 协议登录</h2>
              <small>自动识别并协议登录,获取 AT/RT 后写入账号库</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <textarea v-model="text" class="import-accounts-textarea" placeholder="每行一条:邮箱----密码----2FA密钥(Base32)&#10;# 开头的行会被忽略" spellcheck="false"></textarea>
          <p class="hint">已识别 <b>{{ valid.length }}</b> 个账号<template v-if="invalidCount"> · {{ invalidCount }} 行格式不符</template></p>
          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="emit('close')">取消</button>
            <button class="primary" type="button" :disabled="!valid.length || busy" @click="submit">开始导入</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
