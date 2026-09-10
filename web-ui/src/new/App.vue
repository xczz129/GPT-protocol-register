<script setup lang="ts">
import { onMounted } from "vue";
import AppShell from "./components/layout/AppShell.vue";
import AppToast from "./components/ui/AppToast.vue";
import AppDialog from "./components/ui/AppDialog.vue";
import { useAuthStore } from "./stores/auth";

const auth = useAuthStore();

onMounted(async () => {
  await auth.fetchStatus();
});
</script>

<template>
  <AppShell v-if="auth.authenticated" />
  <div v-else class="boot-splash">
    <span class="boot-spinner" aria-hidden="true"></span>
    正在载入控制台
  </div>
  <AppToast />
  <AppDialog />
</template>

<style scoped>
.boot-splash {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-muted);
  font-size: 0.9rem;
}

.boot-spinner {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--accent-lighter);
  border-top-color: var(--accent);
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
