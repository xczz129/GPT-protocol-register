<script setup lang="ts">
/* --- chatgpt2api 集成：启用/自动上传/实例地址/Admin Key/分组上传 --- */
import { onMounted, reactive, ref } from "vue";
import { Plug, UploadCloud } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useGroupsStore } from "../../stores/groups";
import { useUiStore } from "../../stores/ui";
import { fetchChatgpt2Api, saveChatgpt2Api, testChatgpt2Api, uploadAllChatgpt2Api } from "../../api/chatgpt2api";

const groups = useGroupsStore();
const ui = useUiStore();

const form = reactive({ enabled: false, autoUpload: true, url: "", key: "", groupId: "" });
const keyMasked = ref("");
const loading = ref(false);
const busy = ref(false);

async function load() {
  loading.value = true;
  try {
    const config = await fetchChatgpt2Api();
    form.enabled = config.enabled;
    form.autoUpload = config.auto_upload;
    form.url = config.url;
    form.key = "";
    keyMasked.value = config.key_masked || "";
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "配置加载失败", "error");
  } finally {
    loading.value = false;
  }
}

async function save() {
  busy.value = true;
  try {
    const config = await saveChatgpt2Api({ enabled: form.enabled, url: form.url, key: form.key, auto_upload: form.autoUpload });
    form.key = "";
    keyMasked.value = config.key_masked || "";
    ui.notify("chatgpt2api 配置已保存", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "保存失败", "error");
  } finally {
    busy.value = false;
  }
}

async function test() {
  busy.value = true;
  try {
    const result = await testChatgpt2Api(form.url, form.key);
    ui.notify(result.message || (result.ok ? "连接成功" : "连接失败"), result.ok ? "success" : "error");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "测试失败", "error");
  } finally {
    busy.value = false;
  }
}

async function upload() {
  busy.value = true;
  try {
    const result = await uploadAllChatgpt2Api(form.url, form.key, form.groupId);
    ui.notify(result.message || `已处理 ${result.count || 0} 个账号`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "上传失败", "error");
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  void load();
  if (!groups.items.length) void groups.load();
});
</script>

<template>
  <section class="integration-panel glass-panel">
    <div class="panel-head">
      <div>
        <h2><Plug :size="17" /> chatgpt2api</h2>
        <p class="panel-sub">管理注册成功后的自动入库目标。</p>
      </div>
    </div>

    <div v-if="loading" class="loading-hint">配置加载中…</div>

    <template v-else>
      <div class="form-grid">
        <label class="check-row"><input v-model="form.enabled" type="checkbox" /><span>启用 chatgpt2api</span></label>
        <label class="check-row"><input v-model="form.autoUpload" type="checkbox" /><span>注册后自动上传</span></label>
        <label class="field full">
          <span>实例地址</span>
          <input v-model="form.url" class="input" type="text" placeholder="https://chatgpt2api.example/" />
        </label>
        <label class="field full">
          <span>Admin Key</span>
          <input v-model="form.key" class="input" type="password" :placeholder="keyMasked ? `留空保持现有密钥（${keyMasked}）` : '留空保持现有密钥'" autocomplete="off" />
        </label>
        <label class="field full">
          <span>上传账号分组</span>
          <select v-model="form.groupId" class="input">
            <option value="">全部分组</option>
            <option v-for="group in groups.items" :key="group.id" :value="group.id">{{ group.name }}（{{ group.count ?? 0 }}）</option>
          </select>
        </label>
      </div>

      <div class="actions">
        <AppButton variant="primary" :loading="busy" @click="save">保存配置</AppButton>
        <AppButton variant="accent" :disabled="busy" @click="test">测试连接</AppButton>
        <AppButton variant="accent" :disabled="busy" @click="upload"><UploadCloud :size="14" /><span>上传所选分组</span></AppButton>
      </div>
    </template>
  </section>
</template>

<style scoped>
.integration-panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 720px;
}

.panel-head h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 1.05rem;
  color: var(--text-primary);
}

.panel-sub {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 3px;
}

.loading-hint {
  color: var(--text-muted);
  font-size: 0.85rem;
  padding: 24px 0;
  text-align: center;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 18px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.field.full { grid-column: 1 / -1; }

.check-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.84rem;
  color: var(--text-primary);
  cursor: pointer;
}

.check-row input { accent-color: var(--accent); }

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

@media (max-width: 640px) {
  .form-grid { grid-template-columns: 1fr; }
}
</style>
