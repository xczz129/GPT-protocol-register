<script setup lang="ts">
/* --- 顶栏：页面标题 + 运行状态 + 账户操作 --- */
import { computed } from "vue";
import { useRoute } from "vue-router";
import { LogOut, Menu, RefreshCw } from "@lucide/vue";
import AppTag from "../ui/AppTag.vue";
import AppButton from "../ui/AppButton.vue";
import { useAuthStore } from "../../stores/auth";
import { useStatusStore } from "../../stores/status";
import { useUiStore } from "../../stores/ui";

const route = useRoute();
const auth = useAuthStore();
const status = useStatusStore();
const ui = useUiStore();

const title = computed(() => String(route.meta.title || ""));
const subtitle = computed(() => String(route.meta.subtitle || ""));

async function refresh() {
  await status.load(true);
  ui.notify("数据已刷新", "success", 1800);
}
</script>

<template>
  <header class="topbar glass-panel">
    <div class="topbar-left">
      <button class="menu-button" type="button" aria-label="打开导航" @click="ui.openSidebar()">
        <Menu :size="19" />
      </button>
      <div class="page-heading">
        <h1>{{ title }}</h1>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
    </div>

    <div class="topbar-actions">
      <AppTag :kind="status.running ? 'success' : 'default'" dot>
        {{ status.running ? "运行中" : "空闲" }}
      </AppTag>
      <span v-if="auth.username" class="badge">{{ auth.username }}</span>
      <AppButton variant="ghost" size="sm" title="刷新数据" aria-label="刷新数据" @click="refresh">
        <RefreshCw :size="14" /><span>刷新</span>
      </AppButton>
      <AppButton variant="ghost" size="sm" title="退出登录" aria-label="退出登录" @click="auth.logout()">
        <LogOut :size="14" /><span>退出</span>
      </AppButton>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 22px;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.menu-button {
  display: none;
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 4px;
}

.page-heading h1 {
  font-size: 1.18rem;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.3;
}

.page-heading p {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 1px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

@media (max-width: 768px) {
  .topbar {
    padding: 11px 14px;
  }

  .menu-button {
    display: block;
  }

  .topbar-actions :deep(.btn span) {
    display: none;
  }

  .page-heading p {
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 1;
    -webkit-box-orient: vertical;
  }
}
</style>
