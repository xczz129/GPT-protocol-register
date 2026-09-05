<script setup>
import { computed, ref, watch } from "vue";
import { Upload, Users, Zap, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  busy: Boolean,
});
const emit = defineEmits(["close", "submit"]);

const CONCURRENCY_OPTIONS = [1, 2, 3, 4, 5, 6, 8];

const text = ref("");
const accountType = ref("personal");
const workspace = ref("");
const concurrency = ref(1);

watch(() => props.open, (value) => {
  if (value) { accountType.value = "personal"; workspace.value = ""; concurrency.value = 1; }
});

const lines = computed(() => String(text.value || "").split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith("#")));
const valid = computed(() => lines.value.filter((line) => {
  const parts = line.split("----");
  return parts.length === 3 && parts.every((part) => part.trim());
}));
const invalidCount = computed(() => lines.value.length - valid.value.length);
const workspaceMissing = computed(() => accountType.value === "team" && !workspace.value.trim());

function setType(type) { accountType.value = type; }
function setConcurrency(value) { concurrency.value = value; }

function submit() {
  if (!valid.value.length || workspaceMissing.value) return;
  emit("submit", { text: text.value, accountType: accountType.value, workspace: workspace.value.trim(), concurrency: concurrency.value });
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
              <small>协议登录(密码 + TOTP)写入账号库,套餐由登录后自动识别</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <textarea v-model="text" class="import-accounts-textarea" placeholder="每行一条:邮箱----密码----2FA密钥(Base32)&#10;# 开头的行会被忽略" spellcheck="false"></textarea>
          <p class="hint">已识别 <b>{{ valid.length }}</b> 个账号<template v-if="invalidCount"> · {{ invalidCount }} 行格式不符</template></p>
          <div class="import-accounts-plan">
            <div class="import-accounts-field">
              <span class="import-accounts-label">账号类型</span>
              <div class="segmented" role="group" aria-label="账号类型">
                <button type="button" :class="{ active: accountType === 'personal' }" @click="setType('personal')">个人</button>
                <button type="button" :class="{ active: accountType === 'team' }" @click="setType('team')"><Users :size="12" aria-hidden="true" />Team</button>
              </div>
            </div>
            <div class="import-accounts-field">
              <span class="import-accounts-label"><Zap :size="11" aria-hidden="true" /> 并发</span>
              <div class="segmented" role="group" aria-label="并发数">
                <button v-for="option in CONCURRENCY_OPTIONS" :key="option" type="button" :class="{ active: concurrency === option }" @click="setConcurrency(option)">{{ option }}</button>
              </div>
            </div>
            <div v-if="accountType === 'team'" class="import-accounts-field import-accounts-field-grow">
              <span class="import-accounts-label">工作区名称</span>
              <input v-model="workspace" class="import-accounts-workspace" type="text" placeholder="如 miaomiao" spellcheck="false" autocomplete="off" />
              <small v-if="workspaceMissing" class="import-accounts-warn">Team 账号需要填写工作区名称</small>
            </div>
          </div>
          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="emit('close')">取消</button>
            <button class="primary" type="button" :disabled="!valid.length || busy || workspaceMissing" @click="submit">开始导入</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
