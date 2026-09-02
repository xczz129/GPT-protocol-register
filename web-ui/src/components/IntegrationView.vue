<script setup>
import { computed } from "vue";
const props = defineProps({ form: { type: Object, required: true }, groups: { type: Array, default: () => [] }, progress: { type: Object, default: () => ({ items: [] }) }, busy: Boolean });
defineEmits(["save", "test", "upload"]);
const upload = computed(() => (Array.isArray(props.progress?.items) ? props.progress.items : []).find((item) => item.key === "c2a_upload"));
</script>

<template>
  <section class="card">
    <div class="card-h"><div><h2>chatgpt2api</h2><p class="subtle">管理注册成功后的自动入库目标。</p></div></div>
    <div class="form two">
      <label class="check"><input v-model="form.enabled" type="checkbox" />启用 chatgpt2api</label>
      <label class="check"><input v-model="form.autoUpload" type="checkbox" />注册后自动上传</label>
      <label class="full">实例地址<input v-model="form.url" type="text" placeholder="https://chatgpt2api.example/" /></label>
      <label class="full">Admin Key<input v-model="form.key" type="password" placeholder="留空保持现有密钥" autocomplete="off" /></label>
      <label class="full">上传账号分组<select v-model="form.groupId"><option value="">全部分组</option><option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}（{{ group.account_count || 0 }}）</option></select></label>
    </div>
    <div class="actions">
      <button class="primary" :disabled="busy" @click="$emit('save')">保存配置</button>
      <button class="btn secondary" :disabled="busy" @click="$emit('test')">测试连接</button>
      <button class="btn secondary" :disabled="busy" @click="$emit('upload')">上传所选分组</button>
    </div>
    <div v-if="upload" class="inline-progress"><div><strong>{{ upload.stage }}</strong><span>{{ upload.done || 0 }} / {{ upload.total || 0 }} · {{ Math.round(upload.percent || 0) }}%</span></div><div class="inline-progress-track"><i :style="{ width: `${upload.percent || 0}%` }"></i></div><small>{{ upload.message || `已运行 ${Math.round(upload.elapsed_sec || 0)}s` }}</small></div>
  </section>
</template>
