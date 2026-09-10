<script setup lang="ts">
/* --- 接码国家选择弹窗：搜索 / 价格过滤 / 档位多选 / 顺序标记 --- */
import { computed, ref } from "vue";
import { Globe, RotateCcw, Search, X } from "@lucide/vue";
import type { SmsCountryItem } from "../../api/ops";

const props = withDefaults(
  defineProps<{
    open: boolean;
    provider?: string;
    items?: SmsCountryItem[];
    selected?: string[];
    providers?: Record<string, string>;
    loading?: boolean;
    error?: string;
  }>(),
  {
    provider: "smsbower",
    items: () => [],
    selected: () => [],
    providers: () => ({}),
    loading: false,
    error: "",
  },
);

const emit = defineEmits<{
  (e: "close"): void;
  (e: "toggle", id: string): void;
  (e: "set-provider", payload: { id: string; providerId: string }): void;
}>();

const search = ref("");
const minPrice = ref("");
const maxPrice = ref("");
const expanded = ref<string | null>(null);

const filtered = computed(() => {
  const needle = search.value.trim().toLowerCase();
  const min = Number(minPrice.value) || 0;
  const max = Number(maxPrice.value) || 0;
  return props.items.filter((item) => {
    if (needle) {
      const name = String(item.name || "").toLowerCase();
      const calling = String(item.calling || "").toLowerCase();
      const id = String(item.id || "");
      if (!name.includes(needle) && !calling.includes(needle) && id !== needle) return false;
    }
    const price = Number(item.price);
    if (minPrice.value && (!item.price || price < min)) return false;
    if (maxPrice.value && (!item.price || price > max)) return false;
    return true;
  });
});

const selectedCount = computed(() => props.selected.length);

function resetFilters() {
  search.value = "";
  minPrice.value = "";
  maxPrice.value = "";
}

function roundPrice(value: number): string {
  return String(Math.round(value * 1000) / 1000);
}

function fmtCount(value: unknown): string {
  const count = Number(value) || 0;
  return count >= 10000 ? `${Math.round(count / 1000)}k` : String(count);
}

function toggleExpand(item: SmsCountryItem) {
  expanded.value = expanded.value === item.id ? null : item.id;
}

function providerList(id: string): string[] {
  const raw = String(props.providers[id] || "").trim();
  // "|" 为档位分隔符；"," 兼容旧配置
  return raw ? raw.split(/[|,]/).map((pid) => pid.trim()).filter(Boolean) : [];
}

function providerRank(id: string, pid: string): number {
  const index = providerList(id).findIndex((item) => String(item) === String(pid));
  return index >= 0 ? index + 1 : 0;
}

function toggleTier(id: string, pid: string) {
  const list = providerList(id);
  const index = list.findIndex((item) => String(item) === String(pid));
  if (index >= 0) {
    list.splice(index, 1);
  } else {
    list.push(String(pid));
  }
  emit("set-provider", { id, providerId: list.join("|") });
}

function clearTiers(id: string) {
  emit("set-provider", { id, providerId: "" });
}

function tierSummary(id: string): string {
  const list = providerList(id);
  if (!list.length) return "价位";
  const head = list.map((pid) => `#${pid}`).slice(0, 2).join("·");
  return list.length > 2 ? `${head}…` : head;
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="picker-mask" role="presentation" @click.self="emit('close')">
        <section class="picker-dialog" role="dialog" aria-modal="true" aria-label="选择国家" @keyup.esc="emit('close')">
          <div class="picker-head">
            <div>
              <span class="picker-kicker"><Globe :size="12" aria-hidden="true" />选择国家</span>
              <h2>接码国家顺序</h2>
              <small>{{ provider === "firefox" ? "火狐狸" : "SMSBower" }} · 点行选中国家 · 点「价位」按顺序多选价格档位</small>
            </div>
            <button class="picker-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')">
              <X :size="16" aria-hidden="true" />
            </button>
          </div>

          <div class="picker-toolbar">
            <div class="picker-searchwrap">
              <Search :size="14" aria-hidden="true" />
              <input v-model="search" class="input picker-search" type="text" placeholder="搜索国家 / 区号 / ID，如 +44 或 印尼" />
            </div>
            <template v-if="provider !== 'firefox'">
              <label class="picker-price-filter">$ ≥<input v-model="minPrice" class="input picker-price-input" type="number" min="0" step="0.01" placeholder="0" /></label>
              <label class="picker-price-filter">$ ≤<input v-model="maxPrice" class="input picker-price-input" type="number" min="0" step="0.01" placeholder="不限" /></label>
            </template>
            <button class="picker-reset" type="button" title="清空搜索与价格过滤" @click="resetFilters">
              <RotateCcw :size="13" aria-hidden="true" />重置
            </button>
          </div>

          <div class="picker-list">
            <div v-if="loading" class="picker-empty">正在获取国家列表…</div>
            <div v-else-if="error" class="picker-empty picker-error">{{ error }}</div>
            <div v-else-if="!filtered.length" class="picker-empty">
              没有匹配的国家{{ minPrice || maxPrice ? "（试试放宽价格区间）" : "" }}
            </div>
            <template v-for="item in filtered" :key="item.id">
              <div
                :class="['picker-row', selected.includes(item.id) ? 'on' : '']"
                role="button"
                tabindex="0"
                :aria-pressed="selected.includes(item.id)"
                @click="emit('toggle', item.id)"
                @keydown.enter.prevent="emit('toggle', item.id)"
                @keydown.space.prevent="emit('toggle', item.id)"
              >
                <span class="picker-order">{{ selected.includes(item.id) ? selected.indexOf(item.id) + 1 : "" }}</span>
                <span class="picker-name">{{ item.name }}</span>
                <span v-if="item.calling" class="picker-calling">{{ item.calling }}</span>
                <span v-if="item.price" class="picker-price">≈${{ item.price }}</span>
                <span v-if="item.count" class="picker-stock">{{ fmtCount(item.count) }} 件</span>
                <span
                  v-if="provider !== 'firefox' && (item.tiers || []).length"
                  :class="['picker-tier-btn', providerList(item.id).length ? 'picked' : '']"
                  title="展开价格档位（可按顺序多选）"
                  @click.stop="toggleExpand(item)"
                >
                  {{ tierSummary(item.id) }}
                </span>
              </div>
              <div v-if="expanded === item.id" class="tier-panel">
                <div class="tier-head">
                  <span v-if="providerList(item.id).length" class="tier-seq">
                    购买顺序：{{ providerList(item.id).map((pid) => `#${pid}`).join(" → ") }}
                  </span>
                  <span v-else class="tier-hint">点价格追加，先选的先试</span>
                  <button v-if="providerList(item.id).length" class="tier-clear" type="button" @click="clearTiers(item.id)">清空</button>
                </div>
                <button
                  v-for="tier in item.tiers"
                  :key="tier.id"
                  type="button"
                  :class="['tier-row', providerRank(item.id, tier.id) ? 'cur' : '']"
                  @click="toggleTier(item.id, tier.id)"
                >
                  <span class="tier-rank">{{ providerRank(item.id, tier.id) || "" }}</span>
                  <b>${{ roundPrice(tier.price) }}</b>
                  <small>#{{ tier.id }} · {{ fmtCount(tier.count) }} 件</small>
                </button>
              </div>
            </template>
          </div>

          <div class="picker-foot">
            <span class="picker-count">已选 {{ selectedCount }} 个国家 · 共 {{ filtered.length }} 个</span>
            <button class="picker-done" type="button" @click="emit('close')">完成</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.18s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}

.picker-mask {
  position: fixed;
  inset: 0;
  z-index: 90;
  background: rgba(30, 45, 80, 0.28);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.picker-dialog {
  width: min(680px, 100%);
  max-height: min(78vh, 720px);
  display: flex;
  flex-direction: column;
  background: var(--glass-bg-strong, rgba(255, 255, 255, 0.92));
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg, 0 24px 60px rgba(30, 45, 80, 0.22));
  overflow: hidden;
}

.picker-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 10px;
}

.picker-kicker {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.05em;
  color: var(--accent);
}

.picker-head h2 {
  margin: 3px 0 2px;
  font-size: 1.02rem;
  color: var(--text-primary);
}

.picker-head small {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.picker-close {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px;
  border-radius: 8px;
  transition: var(--transition-fast);
}

.picker-close:hover {
  color: var(--text-primary);
  background: var(--glass-bg-hover);
}

.picker-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 18px 10px;
  flex-wrap: wrap;
}

.picker-searchwrap {
  flex: 1;
  min-width: 200px;
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--text-muted);
}

.picker-search {
  flex: 1;
}

.picker-price-filter {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 0.74rem;
  color: var(--text-secondary);
}

.picker-price-input {
  width: 64px;
  padding: 5px 8px;
}

.picker-reset {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--glass-bg);
  color: var(--text-secondary);
  font-size: 0.74rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.picker-reset:hover {
  color: var(--accent);
  border-color: var(--accent-border);
}

.picker-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 12px 10px;
}

.picker-empty {
  padding: 32px 0;
  text-align: center;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.picker-error {
  color: var(--error);
}

.picker-row {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  cursor: pointer;
  transition: var(--transition-fast);
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.picker-row:hover {
  background: var(--glass-bg-hover);
}

.picker-row.on {
  background: var(--accent-bg);
  border-color: var(--accent-border);
  color: var(--accent);
}

.picker-order {
  width: 18px;
  text-align: center;
  font-size: 0.72rem;
  font-weight: 700;
  color: var(--accent);
  flex-shrink: 0;
}

.picker-name {
  font-weight: 600;
  color: var(--text-primary);
}

.picker-row.on .picker-name {
  color: var(--accent);
}

.picker-calling,
.picker-stock {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.picker-price {
  font-size: 0.74rem;
  font-weight: 600;
  color: var(--success, #16a34a);
}

.picker-tier-btn {
  margin-left: auto;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 0.7rem;
  border: 1px solid var(--field-border);
  color: var(--text-muted);
  flex-shrink: 0;
}

.picker-tier-btn:hover {
  border-color: var(--accent-border);
  color: var(--accent);
}

.picker-tier-btn.picked {
  background: var(--accent-bg);
  border-color: var(--accent-border);
  color: var(--accent);
  font-weight: 600;
}

.tier-panel {
  margin: 2px 6px 8px 28px;
  padding: 8px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.tier-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 2px 4px;
}

.tier-seq {
  font-size: 0.7rem;
  color: var(--accent);
  font-weight: 600;
}

.tier-hint {
  font-size: 0.7rem;
  color: var(--text-muted);
}

.tier-clear {
  background: none;
  border: none;
  font-size: 0.7rem;
  color: var(--text-muted);
  cursor: pointer;
}

.tier-clear:hover {
  color: var(--error);
}

.tier-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: none;
  cursor: pointer;
  font-size: 0.78rem;
  color: var(--text-secondary);
  transition: var(--transition-fast);
  text-align: left;
}

.tier-row:hover {
  background: var(--glass-bg-hover);
}

.tier-row.cur {
  background: var(--accent-bg);
  border-color: var(--accent-border);
}

.tier-rank {
  width: 16px;
  text-align: center;
  font-size: 0.72rem;
  font-weight: 700;
  color: var(--accent);
}

.tier-row b {
  color: var(--text-primary);
  min-width: 56px;
}

.tier-row small {
  color: var(--text-muted);
  font-size: 0.7rem;
}

.picker-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 18px;
  border-top: 1px solid var(--line-subtle);
}

.picker-count {
  font-size: 0.74rem;
  color: var(--text-muted);
}

.picker-done {
  padding: 7px 22px;
  border-radius: var(--radius-sm);
  border: none;
  background: var(--accent);
  color: #fff;
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition-fast);
}

.picker-done:hover {
  background: var(--accent-hover, var(--accent));
  filter: brightness(1.05);
}
</style>
