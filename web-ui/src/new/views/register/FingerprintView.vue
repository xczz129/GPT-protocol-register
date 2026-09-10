<script setup lang="ts">
/* --- 指纹环境：浏览器运行环境检测卡片 + 指纹浏览器配置（对齐旧版 BrowserView） --- */
import { onMounted, ref } from "vue";
import { Activity, RefreshCw } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppEmpty from "../../components/ui/AppEmpty.vue";
import { useFingerprintStore } from "../../stores/fingerprint";
import { useUiStore } from "../../stores/ui";

const fp = useFingerprintStore();
const ui = useUiStore();
const busy = ref(false);

async function save() {
  if (busy.value) return;
  busy.value = true;
  try {
    await fp.save();
    ui.notify("浏览器配置已保存", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "保存失败", "error");
  } finally {
    busy.value = false;
  }
}

onMounted(() => fp.load());
</script>

<template>
  <div class="fp-page">
    <!-- 运行环境检测 -->
    <section class="panel glass-panel">
      <div class="panel-head">
        <div>
          <h2 class="panel-title">浏览器运行环境</h2>
          <p class="panel-sub">状态来自服务器实时检测；只有标记"可用"的类型才能直接启动</p>
        </div>
        <AppButton :loading="fp.loading" @click="fp.load()"><RefreshCw :size="14" /><span>重新检测</span></AppButton>
      </div>

      <AppEmpty v-if="fp.loadError" title="运行环境检测失败" :hint="fp.loadError">
        <template #icon><Activity :size="48" :stroke-width="1" /></template>
      </AppEmpty>

      <div v-else class="cap-grid">
        <article
          v-for="item in fp.capabilities" :key="item.id"
          :class="['cap-card', { selected: item.selected, missing: !item.available }]"
        >
          <div class="cap-head">
            <div><b>{{ item.name }}</b><span class="cap-kind">{{ item.kind }}</span></div>
            <span :class="['availability', item.available ? 'ready' : 'missing']">
              <i></i>{{ item.available ? "可用" : "不可用" }}
            </span>
          </div>
          <p class="cap-detail">{{ item.detail }}</p>
          <code v-if="item.endpoint" class="cap-endpoint">{{ item.endpoint }}</code>
          <span v-if="item.selected" class="selected-mark">当前选择</span>
        </article>
      </div>
    </section>

    <!-- 指纹浏览器配置 -->
    <section class="panel glass-panel">
      <h2 class="panel-title">指纹浏览器配置</h2>
      <p class="panel-sub">选择浏览器后保存，浏览器引擎注册任务会使用该运行环境</p>

      <div class="form-grid">
        <label class="field">
          <span>浏览器类型</span>
          <select v-model="fp.form.browser" class="input">
            <option value="local">Local Chrome</option>
            <option value="camoufox">Camoufox</option>
            <option value="obscura">Obscura</option>
          </select>
        </label>

        <label class="field">
          <span>单账号超时(s)</span>
          <input v-model.number="fp.form.timeout" class="input" type="number" min="60" max="3600" />
        </label>

        <label class="field">
          <span>瞬时重试</span>
          <input v-model.number="fp.form.maxTransientRetries" class="input" type="number" min="0" max="10" />
        </label>

        <label class="check">
          <input v-model="fp.form.postRegisterHealthCheck" type="checkbox" />
          <span>协议注册后立即远程测活</span>
        </label>

        <template v-if="fp.form.browser === 'camoufox'">
          <label class="field">
            <span>Camoufox OS</span>
            <select v-model="fp.form.camoufoxOs" class="input">
              <option value="windows">Windows</option>
              <option value="macos">macOS</option>
              <option value="linux">Linux</option>
              <option value="windows,macos,linux">随机桌面系统</option>
            </select>
          </label>

          <label class="field">
            <span>Locale</span>
            <input v-model="fp.form.camoufoxLocale" class="input" type="text" placeholder="自动匹配出口 IP" />
          </label>

          <label class="field">
            <span>Humanize (s)</span>
            <input v-model.number="fp.form.camoufoxHumanize" class="input" type="number" min="0" max="1.5" step="0.05" />
          </label>

          <label class="check">
            <input v-model="fp.form.camoufoxHeadless" type="checkbox" />
            <span>无头运行</span>
          </label>

          <label class="check">
            <input v-model="fp.form.camoufoxGeoip" type="checkbox" />
            <span>出口 GeoIP 同步</span>
          </label>
        </template>
      </div>

      <div class="form-actions">
        <AppButton variant="primary" :loading="busy" @click="save"><span>保存浏览器配置</span></AppButton>
      </div>
    </section>
  </div>
</template>

<style scoped>
.fp-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.panel-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.panel-sub {
  font-size: 0.76rem;
  color: var(--text-muted);
  margin-top: 4px;
}

.panel-head .panel-sub {
  margin-top: 4px;
}

/* --- 运行环境卡片 --- */
.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}

.cap-card {
  position: relative;
  padding: 14px;
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: var(--transition-fast);
}

.cap-card.selected {
  border-color: var(--accent-border);
  background: var(--accent-bg);
}

.cap-card.missing {
  opacity: 0.65;
}

.cap-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.cap-head b {
  color: var(--text-primary);
  font-size: 0.86rem;
}

.cap-kind {
  margin-left: 6px;
  font-size: 0.68rem;
  color: var(--text-muted);
}

.availability {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 0.7rem;
  font-weight: 600;
  white-space: nowrap;
}

.availability i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.availability.ready {
  color: var(--success);
}

.availability.ready i {
  background: var(--success);
  box-shadow: 0 0 6px var(--success);
}

.availability.missing {
  color: var(--error);
}

.availability.missing i {
  background: var(--error);
}

.cap-detail {
  margin: 0;
  font-size: 0.74rem;
  color: var(--text-muted);
  line-height: 1.5;
}

.cap-endpoint {
  font-size: 0.68rem;
  color: var(--text-secondary);
  word-break: break-all;
}

.selected-mark {
  position: absolute;
  top: 10px;
  right: 12px;
  font-size: 0.64rem;
  font-weight: 700;
  color: var(--accent);
}

/* --- 配置表单 --- */
.form-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field > span {
  font-size: 0.74rem;
  color: var(--text-secondary);
  font-weight: 500;
}

.check {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  cursor: pointer;
  align-self: end;
  padding-bottom: 8px;
}

.check input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
}

.form-actions {
  border-top: 1px solid var(--line-subtle);
  padding-top: 14px;
}

@media (max-width: 900px) {
  .form-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
