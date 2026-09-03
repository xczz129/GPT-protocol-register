<script setup>
import { nextTick, ref, watch } from "vue";
import {
  Activity,
  ArrowLeftRight,
  Cable,
  ClipboardList,
  CloudCog,
  Contact,
  FileDown,
  Globe2,
  LogIn,
  Mail,
  Moon,
  Network,
  ShieldCheck,
  SquareTerminal,
  Sun,
  X,
} from "@lucide/vue";

const props = defineProps({ active: String, accountCount: [Number, String], open: Boolean });
const emit = defineEmits(["navigate", "close"]);
const nav = ref(null);

const theme = ref(document.documentElement.dataset.theme === "light" ? "light" : "dark");
function setTheme(value) {
  theme.value = value;
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem("ui-theme", value); } catch { /* private mode */ }
}

const sections = [
  {
    label: "注册",
    items: [
      ["task", SquareTerminal, "任务"],
      ["fingerprint", Activity, "指纹"],
      ["mail", Mail, "邮箱池"],
      ["proxy", Network, "代理"],
    ],
  },
  {
    label: "资格与连接",
    items: [
      ["trial-proxy", Globe2, "资格代理"],
      ["plus-eligibility", ShieldCheck, "Plus 资格"],
      ["chatgpt2api", Cable, "chatgpt2api"],
      ["logs", ClipboardList, "日志"],
    ],
  },
  {
    label: "账号管理",
    items: [
      ["accounts", Contact, "账号"],
      ["rebind", ArrowLeftRight, "换绑"],
      ["credential-export", FileDown, "凭据导出"],
      ["protocol-login", LogIn, "协议登录"],
      ["operations", CloudCog, "运维"],
    ],
  },
];

function itemCount(item) {
  return item[0] === "accounts" ? props.accountCount ?? "-" : "";
}

function select(item) {
  emit("navigate", item[0]);
}

watch(() => props.active, async (value) => {
  await nextTick();
  nav.value?.querySelector(`[data-view="${value}"]`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}, { immediate: true });
</script>

<template>
  <aside :class="['side', { 'is-open': open }]">
    <div class="brandbox">
      <span class="brand-mark" aria-hidden="true">G</span>
      <span class="brand-copy"><b>GPT Register</b><small>FP Console</small></span>
      <button class="sidebar-close" type="button" aria-label="关闭导航" @click="emit('close')"><X :size="18" aria-hidden="true" /></button>
    </div>
    <nav ref="nav" class="nav" aria-label="主导航">
      <section v-for="section in sections" :key="section.label" class="nav-section">
        <p class="nav-section-title">{{ section.label }}</p>
        <a
          v-for="item in section.items"
          :key="item[0]"
          :href="`#${item[0]}`"
          :data-view="item[0]"
          :class="{ active: active === item[0] }"
          @click.prevent="select(item)"
        >
          <span class="nav-icon" aria-hidden="true"><component :is="item[1]" :size="16" /></span>
          <span class="nav-label">{{ item[2] }}</span>
          <small v-if="itemCount(item)">{{ itemCount(item) }}</small>
        </a>
      </section>
    </nav>
    <div class="side-theme">
      <div class="side-theme-options" role="group" aria-label="界面风格">
        <button type="button" :class="{ active: theme === 'dark' }" @click="setTheme('dark')">
          <Moon :size="13" aria-hidden="true" /><span>黑色</span>
        </button>
        <button type="button" :class="{ active: theme === 'light' }" @click="setTheme('light')">
          <Sun :size="13" aria-hidden="true" /><span>白色</span>
        </button>
      </div>
    </div>
  </aside>
</template>
