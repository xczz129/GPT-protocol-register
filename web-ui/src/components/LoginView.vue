<script setup>
import { ref } from "vue";
import { KeyRound, LogIn, UserRound } from "@lucide/vue";
import { api, jsonBody } from "../api.js";

const username = ref("");
const password = ref("");
const error = ref("");
const busy = ref(false);

async function submit() {
  if (busy.value) return;
  if (!username.value.trim() || !password.value) {
    error.value = "请输入用户名和密码";
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    await api("/api/auth/login", { method: "POST", body: jsonBody({ username: username.value.trim(), password: password.value }) });
    location.replace("/");
  } catch (err) {
    error.value = err?.message || "登录失败，请稍后重试";
    busy.value = false;
  }
}
</script>

<template>
  <div class="login-stage">
    <form class="login-card" @submit.prevent="submit">
      <div class="login-brand">
        <span class="login-mark"><KeyRound :size="20" aria-hidden="true" /></span>
        <div>
          <div class="login-eyebrow">GPT REGISTER FP · CONSOLE</div>
          <h1>登录运维面板</h1>
        </div>
      </div>
      <label class="login-field">
        <span class="login-label"><UserRound :size="13" aria-hidden="true" /> 用户名</span>
        <input v-model="username" type="text" autocomplete="username" spellcheck="false" placeholder="面板用户名" />
      </label>
      <label class="login-field">
        <span class="login-label"><KeyRound :size="13" aria-hidden="true" /> 密码</span>
        <input v-model="password" type="password" autocomplete="current-password" placeholder="面板密码" />
      </label>
      <p v-if="error" class="login-error" role="alert">{{ error }}</p>
      <button class="login-submit" type="submit" :disabled="busy">
        <LogIn :size="15" aria-hidden="true" />
        {{ busy ? "正在登录…" : "登录" }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login-stage {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background:
    radial-gradient(60% 40% at 50% 0%, var(--primary-600), transparent 70%),
    var(--bg-0);
}

.login-card {
  width: min(380px, 100%);
  display: grid;
  gap: 16px;
  padding: 28px;
  border-radius: 14px;
  background: var(--card);
  border: 1px solid var(--line);
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.4);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.login-mark {
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  border-radius: 11px;
  color: #fff;
  background: linear-gradient(135deg, var(--primary-500), var(--primary-700));
  box-shadow: 0 6px 18px rgba(109, 120, 242, 0.35);
}

.login-eyebrow {
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--ink-500);
}

.login-brand h1 {
  margin: 2px 0 0;
  font-size: 17px;
  font-weight: 650;
  color: var(--ink-900);
}

.login-field {
  display: grid;
  gap: 7px;
}

.login-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 550;
  color: var(--ink-600);
}

.login-field input {
  height: 40px;
  padding: 0 13px;
  border-radius: 9px;
  border: 1px solid var(--line-2);
  background: var(--bg-inset);
  color: var(--ink-900);
  font-size: 14px;
  outline: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.login-field input::placeholder {
  color: var(--ink-400);
}

.login-field input:focus {
  border-color: var(--primary-500);
  box-shadow: 0 0 0 3px rgba(129, 140, 248, 0.18);
}

.login-error {
  margin: 0;
  padding: 9px 12px;
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--accent-rose);
  background: var(--accent-rose-light);
}

.login-submit {
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 0;
  border-radius: 9px;
  background: var(--primary-600);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease, transform 0.15s ease;
}

.login-submit:hover:not(:disabled) {
  background: var(--primary-500);
}

.login-submit:active:not(:disabled) {
  transform: translateY(1px);
}

.login-submit:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}
</style>
