<script setup lang="ts">
/* --- 设备信息弹窗：注册环境 / 指纹快照（行内"设备"按钮打开），展示逻辑对齐旧版 deviceDialog --- */
import { computed, ref, watch } from "vue";
import { Monitor, X } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import { fetchDeviceInfo } from "../../api/accounts";

const props = defineProps<{
  open: boolean;
  accountKey: string;
  email: string;
}>();

const emit = defineEmits<{ close: [] }>();

const loading = ref(false);
const error = ref("");
const fingerprint = ref<Record<string, unknown>>({});
const proxy = ref("");
const registeredAt = ref("");
const registrationMethod = ref("");
const deviceId = ref("");
const hasSnapshot = ref(false);

watch(
  () => props.open,
  (value) => {
    if (value) void load();
  },
);

async function load() {
  loading.value = true;
  error.value = "";
  fingerprint.value = {};
  registrationMethod.value = "";
  deviceId.value = "";
  hasSnapshot.value = false;
  try {
    const res = await fetchDeviceInfo(props.accountKey);
    fingerprint.value = res.fingerprint || {};
    proxy.value = res.proxy || proxy.value;
    registeredAt.value = res.registered_at || registeredAt.value;
    registrationMethod.value = res.registration_method || "";
    deviceId.value = res.device_id || "";
    hasSnapshot.value = !!res.has_snapshot;
  } catch (err) {
    error.value = err instanceof Error ? err.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

function text(value: unknown): string {
  return String(value ?? "").trim();
}

function deviceFamilyLabel(fp: Record<string, unknown>): string {
  const family = text(fp.browser_family || "firefox").toLowerCase();
  const version = text(fp.full_version) || text(fp.major);
  const name = family === "chrome" ? "Chrome" : "Firefox";
  return version ? `${name} ${version}` : name;
}

function deviceTzLabel(fp: Record<string, unknown>): string {
  if (!fp.timezone) return "";
  const raw = fp.timezone_offset_min;
  if (raw === undefined || raw === null || raw === "") return text(fp.timezone);
  const off = Number(raw);
  if (Number.isNaN(off)) return text(fp.timezone);
  const sign = off <= 0 ? "+" : "-";
  const abs = Math.abs(off);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return `${text(fp.timezone)} (UTC${sign}${h}${m ? ":" + String(m).padStart(2, "0") : ""})`;
}

const groups = computed(() => {
  const fp = fingerprint.value;
  const mem = fp.device_memory ? `${text(fp.device_memory)} GB` : "";
  const list: { title: string; rows: [string, string][] }[] = [
    {
      title: "浏览器环境",
      rows: [
        ["浏览器", deviceFamilyLabel(fp)],
        ["TLS 指纹", text(fp.impersonate)],
        ["User-Agent", text(fp.user_agent)],
        ["Vendor", text(fp.navigator_vendor)],
        ["Sec-CH-UA", text(fp.sec_ch_ua)],
      ],
    },
    {
      title: "屏幕与硬件",
      rows: [
        ["分辨率", text(fp.screen)],
        ["像素比", text(fp.device_pixel_ratio)],
        ["CPU 线程", text(fp.hardware_concurrency)],
        ["内存", mem],
        ["色深", fp.color_depth ? `${text(fp.color_depth)} bit` : ""],
        ["触点", text(fp.max_touch_points)],
        ["平台", fp.platform ? `${text(fp.platform)} · Win ${text(fp.platform_version) || "10"}` : ""],
      ],
    },
    {
      title: "图形 (WebGL)",
      rows: [
        ["Vendor", text(fp.webgl_vendor)],
        ["Renderer", text(fp.webgl_renderer)],
      ],
    },
    {
      title: "地区环境",
      rows: [
        ["时区", deviceTzLabel(fp)],
        ["Locale", text(fp.locale)],
        ["语言", text(fp.accept_language)],
      ],
    },
    {
      title: "注册上下文",
      rows: [
        ["注册方式", registrationMethod.value === "protocol" ? "协议注册" : registrationMethod.value === "browser" ? "浏览器注册" : registrationMethod.value],
        ["代理", proxy.value],
        ["Device ID", deviceId.value || text(fp.device_id)],
        ["Profile ID", text(fp.profile_id)],
        ["Sentinel SID", text(fp.sentinel_sid)],
        [
          "注册时间",
          registeredAt.value ? new Date(registeredAt.value).toLocaleString("zh-CN", { hour12: false }) : "",
        ],
      ],
    },
  ];
  return list.map((group) => ({ ...group, rows: group.rows.filter((row) => row[1]) }));
});
</script>

<template>
  <AppModal :open="open" title="设备信息" :subtitle="email" width="640px" @close="emit('close')">
    <template #icon><Monitor :size="18" /></template>

    <div v-if="loading" class="device-empty">加载注册环境中…</div>
    <div v-else-if="error" class="device-empty error-text">加载失败：{{ error }}</div>

    <template v-else>
      <p v-if="!hasSnapshot" class="device-note">
        该账号注册较早，以下信息由历史字段还原；新注册账号将保存完整指纹快照
      </p>
      <div class="device-grid">
        <section v-for="group in groups" v-show="group.rows.length" :key="group.title" class="device-group">
          <h4>{{ group.title }}</h4>
          <dl>
            <template v-for="row in group.rows" :key="row[0]">
              <dt>{{ row[0] }}</dt>
              <dd>{{ row[1] }}</dd>
            </template>
          </dl>
        </section>
      </div>
    </template>

    <template #footer>
      <button class="btn-ghost" type="button" @click="emit('close')"><X :size="14" />关闭</button>
    </template>
  </AppModal>
</template>

<style scoped>
.device-empty {
  text-align: center;
  color: var(--text-muted);
  padding: 32px 0;
  font-size: 0.86rem;
}

.error-text {
  color: var(--error);
}

.device-note {
  margin: 0 0 10px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--info-bg);
  color: var(--info);
  font-size: 0.76rem;
}

.device-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  max-height: 60vh;
  overflow-y: auto;
}

.device-group {
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
}

.device-group h4 {
  margin: 0 0 8px;
  font-size: 0.74rem;
  font-weight: 600;
  color: var(--text-muted);
}

.device-group dl {
  margin: 0;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 10px;
  font-size: 0.78rem;
}

.device-group dt {
  color: var(--text-muted);
  white-space: nowrap;
}

.device-group dd {
  margin: 0;
  color: var(--text-primary);
  word-break: break-all;
}

.btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg);
  color: var(--text-secondary);
  font-size: 0.83rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.btn-ghost:hover {
  background: var(--glass-bg-hover);
  color: var(--accent);
  border-color: var(--accent-border);
}
</style>
