<script setup>
import { computed } from "vue";

const props = defineProps({ config: { type: Object, required: true }, capabilities: { type: Object, default: () => ({ items: [] }) }, busy: Boolean });
const emit = defineEmits(["save", "refresh"]);
const fp = computed(() => props.config.fingerprint || (props.config.fingerprint = {}));
const reg = computed(() => props.config.registration || (props.config.registration = {}));
const camoufox = computed(() => fp.value.camoufox || (fp.value.camoufox = {}));
</script>

<template>
  <section class="card">
    <div class="card-h">
      <div><h2>浏览器运行环境</h2><p class="subtle">状态来自服务器实时检测；只有标记“可用”的类型才能直接启动。</p></div>
      <button class="btn secondary" :disabled="busy" @click="emit('refresh')">重新检测</button>
    </div>
    <div class="browser-grid">
      <article v-for="item in capabilities.items || []" :key="item.id" :class="['browser-capability', { selected: item.selected }]">
        <div class="browser-capability-head">
          <div><b>{{ item.name }}</b><span>{{ item.kind }}</span></div>
          <span :class="['availability', item.available ? 'ready' : 'missing']"><i></i>{{ item.available ? "可用" : "不可用" }}</span>
        </div>
        <p>{{ item.detail }}</p>
        <code v-if="item.endpoint">{{ item.endpoint }}</code>
        <span v-if="item.selected" class="selected-mark">当前选择</span>
      </article>
    </div>
  </section>

  <section class="card">
    <div class="card-h"><div><h2>指纹浏览器配置</h2><p class="subtle">选择浏览器后保存，浏览器引擎注册任务会使用该运行环境。</p></div></div>
    <div class="form three">
      <label>浏览器类型
        <select v-model="fp.browser">
          <option value="local">Local Chrome</option><option value="camoufox">Camoufox</option><option value="obscura">Obscura</option>
        </select>
      </label>
      <label>单账号超时(s)<input v-model.number="reg.timeout" type="number" min="60" max="3600" /></label>
      <label>瞬时重试<input v-model.number="reg.max_transient_retries" type="number" min="0" max="10" /></label>
      <label class="check"><input v-model="reg.post_register_health_check" type="checkbox" />协议注册后立即远程测活</label>

      <template v-if="fp.browser === 'camoufox'">
        <label>Camoufox OS
          <select v-model="camoufox.os"><option value="windows">Windows</option><option value="macos">macOS</option><option value="linux">Linux</option><option value="windows,macos,linux">随机桌面系统</option></select>
        </label>
        <label>Locale<input v-model="camoufox.locale" type="text" placeholder="自动匹配出口 IP" /></label>
        <label>Humanize (s)<input v-model.number="camoufox.humanize" type="number" min="0" max="1.5" step="0.05" /></label>
        <label class="check"><input v-model="camoufox.headless" type="checkbox" />无头运行</label>
        <label class="check"><input v-model="camoufox.geoip" type="checkbox" />出口 GeoIP 同步</label>
      </template>

    </div>
    <div class="actions"><button class="primary" :disabled="busy" @click="emit('save')">保存浏览器配置</button></div>
  </section>
</template>
