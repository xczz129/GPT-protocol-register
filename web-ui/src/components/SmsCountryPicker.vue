<script setup>
import { computed, ref } from "vue";
import { Globe, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  provider: { type: String, default: "smsbower" },
  items: { type: Array, default: () => [] },
  selected: { type: Array, default: () => [] },
  loading: Boolean,
  error: { type: String, default: "" },
});
const emit = defineEmits(["close", "toggle"]);

const search = ref("");

const filtered = computed(() => {
  const needle = String(search.value || "").trim().toLowerCase();
  if (!needle) return props.items;
  return props.items.filter((item) => {
    const name = String(item.name || "").toLowerCase();
    const calling = String(item.calling || "").toLowerCase();
    const id = String(item.id || "");
    return name.includes(needle) || calling.includes(needle) || id === needle;
  });
});

const selectedCount = computed(() => props.selected.length);
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('close')">
        <section class="app-dialog sms-country-dialog" role="dialog" aria-modal="true" aria-label="选择国家" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div>
              <span class="app-dialog-kicker"><Globe :size="12" aria-hidden="true" />选择国家</span>
              <h2>接码国家顺序</h2>
              <small>{{ provider === "firefox" ? "火狐狸" : "SMSBower" }} · 点击切换选中,按选择顺序取号</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <div class="sms-country-toolbar">
            <input v-model="search" class="sms-country-search" type="text" placeholder="搜索国家 / 区号 / ID,如 +44 或 印尼" />
            <span class="sms-country-count">已选 {{ selectedCount }} 个</span>
          </div>
          <div class="sms-country-list">
            <div v-if="loading" class="sms-country-empty">正在获取国家列表…</div>
            <div v-else-if="error" class="sms-country-empty sms-country-error">{{ error }}</div>
            <div v-else-if="!filtered.length" class="sms-country-empty">没有匹配的国家</div>
            <button
              v-for="item in filtered"
              :key="item.id"
              :class="['sms-country-row', selected.includes(item.id) ? 'on' : '']"
              type="button"
              @click="emit('toggle', item.id)"
            >
              <span class="sms-country-order">{{ selected.includes(item.id) ? selected.indexOf(item.id) + 1 : "" }}</span>
              <span class="sms-country-name">{{ item.name }}</span>
              <span v-if="item.calling" class="sms-country-calling">{{ item.calling }}</span>
              <span v-if="item.count" class="sms-country-stock">库存 {{ item.count }}</span>
              <span v-else-if="item.id && !item.calling" class="sms-country-id">#{{ item.id }}</span>
            </button>
          </div>
          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="emit('close')">完成</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
