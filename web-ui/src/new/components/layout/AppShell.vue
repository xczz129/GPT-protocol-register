<script setup lang="ts">
/* --- 应用外壳：氛围背景 + 侧栏 + 顶栏 + 内容区 --- */
import { watch } from "vue";
import { useRoute } from "vue-router";
import AppSidebar from "./AppSidebar.vue";
import AppTopbar from "./AppTopbar.vue";
import { useUiStore } from "../../stores/ui";
import { useStatusStore } from "../../stores/status";

const ui = useUiStore();
const status = useStatusStore();
const route = useRoute();

status.load();

watch(
  () => route.fullPath,
  () => {
    ui.closeSidebar();
    status.load();
  },
);
</script>

<template>
  <div class="bg-animation" aria-hidden="true">
    <div class="bg-orb bg-orb-1"></div>
    <div class="bg-orb bg-orb-2"></div>
    <div class="bg-orb bg-orb-3"></div>
  </div>

  <div class="shell">
    <button
      v-if="ui.sidebarOpen"
      class="sidebar-scrim"
      type="button"
      aria-label="关闭导航"
      @click="ui.closeSidebar()"
    ></button>
    <AppSidebar />
    <div class="shell-main">
      <AppTopbar />
      <main class="shell-content">
        <RouterView v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" :key="route.fullPath" />
          </Transition>
        </RouterView>
      </main>
      <footer class="shell-foot">gpt-register-fp · Vue 3</footer>
    </div>
  </div>
</template>

<style scoped>
.shell {
  position: relative;
  z-index: 1;
  display: flex;
  min-height: 100vh;
}

.shell-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px 20px 12px;
}

.shell-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.shell-foot {
  text-align: center;
  font-size: 0.72rem;
  color: var(--text-muted);
  padding: 4px 0;
}

.sidebar-scrim {
  display: none;
}

.page-enter-active,
.page-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.page-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.page-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* --- 移动端：侧栏改抽屉 --- */
@media (max-width: 768px) {
  .shell-main {
    padding: 12px 12px 8px;
    gap: 10px;
  }

  .sidebar-scrim {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 40;
    border: none;
    background: rgba(30, 41, 59, 0.25);
    backdrop-filter: blur(4px);
  }
}
</style>
