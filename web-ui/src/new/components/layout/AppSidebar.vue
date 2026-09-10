<script setup lang="ts">
/* --- 侧栏导航：分区 --- */
import type { Component } from "vue";
import { RouterLink } from "vue-router";
import {
  Activity, Cable, CloudCog, Contact, FileDown, Gauge, List, LogIn, Mail,
  Network, ShieldCheck, SquareTerminal, ArrowLeftRight,
  Waypoints, X,
} from "@lucide/vue";
import { navSections } from "../../router";
import { useUiStore } from "../../stores/ui";
import { useStatusStore } from "../../stores/status";

const ui = useUiStore();
const status = useStatusStore();

const icons: Record<string, Component> = {
  gauge: Gauge,
  terminal: SquareTerminal,
  mail: Mail,
  network: Network,
  activity: Activity,
  contact: Contact,
  "file-down": FileDown,
  swap: ArrowLeftRight,
  login: LogIn,
  shield: ShieldCheck,
  relay: Waypoints,
  cable: Cable,
  list: List,
  "cloud-cog": CloudCog,
};
</script>

<template>
  <aside :class="['sidebar glass-panel', { 'is-open': ui.sidebarOpen }]">
    <div class="brandbox">
      <span class="brand-mark" aria-hidden="true">G</span>
      <span class="brand-copy">
        <b>GPT Register</b>
        <small>FP Console</small>
      </span>
      <button class="sidebar-close" type="button" aria-label="关闭导航" @click="ui.closeSidebar()">
        <X :size="18" />
      </button>
    </div>

    <nav class="nav" aria-label="主导航">
      <section v-for="section in navSections" :key="section.label" class="nav-section">
        <p class="nav-section-title">{{ section.label }}</p>
        <RouterLink
          v-for="item in section.items"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          active-class="is-active"
        >
          <span class="nav-icon" aria-hidden="true">
            <component :is="icons[item.icon]" :size="17" />
          </span>
          <span class="nav-text">
            <span class="nav-label">{{ item.label }}</span>
            <span class="nav-desc">{{ item.desc }}</span>
          </span>
          <small v-if="item.to === '/accounts' && status.panel.accounts_count != null" class="nav-count">
            {{ status.panel.accounts_count }}
          </small>
        </RouterLink>
      </section>
    </nav>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 264px;
  min-width: 264px;
  margin: 20px 0 20px 20px;
  padding: 18px 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: calc(100vh - 40px);
  position: sticky;
  top: 20px;
  align-self: flex-start;
}

.brandbox {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 2px 4px;
}

.brand-mark {
  width: 36px;
  height: 36px;
  border-radius: 11px;
  background: linear-gradient(135deg, var(--accent-light), var(--accent-2));
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 1.05rem;
  font-weight: 700;
  box-shadow: 0 4px 12px var(--accent-glow);
}

.brand-copy {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}

.brand-copy b {
  font-size: 0.88rem;
  color: var(--text-primary);
}

.brand-copy small {
  font-size: 0.7rem;
  color: var(--text-muted);
}

.sidebar-close {
  display: none;
  margin-left: auto;
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px;
}

.nav {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-right: 2px;
}

.nav-section-title {
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  padding: 0 8px 5px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  border: 1px solid transparent;
  transition: var(--transition-fast);
  margin-bottom: 3px;
}

.nav-item:hover {
  background: var(--glass-bg-hover);
  color: var(--text-primary);
}

.nav-item.is-active {
  background: var(--accent-bg);
  border-color: var(--accent-border);
  color: var(--accent);
  font-weight: 600;
}

.nav-icon {
  display: inline-flex;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  align-items: center;
  justify-content: center;
  background: var(--accent-bg);
  color: var(--accent);
  opacity: 0.75;
  flex-shrink: 0;
}

.nav-item.is-active .nav-icon {
  opacity: 1;
}

.nav-text {
  flex: 1;
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  min-width: 0;
}

.nav-label {
  font-size: 0.87rem;
}

.nav-desc {
  font-size: 0.68rem;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nav-count {
  font-size: 0.7rem;
  color: var(--text-muted);
}

/* --- 移动端抽屉 --- */
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 50;
    margin: 0;
    border-radius: 0 var(--radius-lg) var(--radius-lg) 0;
    max-height: none;
    transform: translateX(-105%);
    transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  }

  .sidebar.is-open {
    transform: translateX(0);
  }

  .sidebar-close {
    display: block;
  }
}
</style>
