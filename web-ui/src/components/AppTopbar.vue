<script setup>
import { LogOut, Menu, RefreshCw } from "@lucide/vue";

defineProps({
  title: { type: String, default: "" },
  subtitle: { type: String, default: "" },
  username: { type: String, default: "" },
  running: Boolean,
  sidebarOpen: Boolean,
});

const emit = defineEmits(["open-sidebar", "refresh", "logout"]);
</script>

<template>
  <header class="hero">
    <div class="hero-title-row">
      <button
        class="header-menu-button"
        type="button"
        aria-label="打开导航"
        :aria-expanded="sidebarOpen"
        @click="emit('open-sidebar')"
      >
        <Menu :size="19" aria-hidden="true" />
      </button>
      <div class="page-heading">
        <div class="eyebrow">GPT REGISTER FP · CONSOLE</div>
        <h1>{{ title }}</h1>
        <p>{{ subtitle }}</p>
      </div>
    </div>

    <div class="hero-actions">
      <span
        :class="['badge', running ? 'run' : 'idle']"
        role="status"
        aria-live="polite"
      >
        <i aria-hidden="true"></i>{{ running ? "运行中" : "空闲" }}
      </span>
      <span v-if="username" class="tag account-tag">{{ username }}</span>
      <button
        class="btn secondary icon-command refresh-command"
        type="button"
        title="刷新数据"
        aria-label="刷新数据"
        @click="emit('refresh')"
      >
        <RefreshCw :size="15" aria-hidden="true" /><span>刷新</span>
      </button>
      <button
        class="btn secondary icon-command logout-command"
        type="button"
        title="退出登录"
        aria-label="退出登录"
        @click="emit('logout')"
      >
        <LogOut :size="15" aria-hidden="true" /><span>退出</span>
      </button>
    </div>
  </header>
</template>
