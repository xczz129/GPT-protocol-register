<script setup lang="ts">
/* --- 占位页：页面按 P1→P4 计划逐页迁移 --- */
import { computed } from "vue";
import { useRoute } from "vue-router";
import { Hammer } from "@lucide/vue";
import AppEmpty from "../components/ui/AppEmpty.vue";

const route = useRoute();
const title = computed(() => String(route.meta.title || "页面"));
</script>

<template>
  <section class="stub glass-panel">
    <AppEmpty :title="`${title} · 重构中`" hint="该页面将按重构计划逐页迁移，当前可继续使用旧版界面">
      <template #icon>
        <Hammer :size="48" :stroke-width="1" />
      </template>
    </AppEmpty>
  </section>
</template>

<style scoped>
.stub {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
