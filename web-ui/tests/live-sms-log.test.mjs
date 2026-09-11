import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as vue from 'vue';
import ts from 'typescript';

// Execute the actual component with Vue's watchers and synthetic task data.
// No login, account mutation, supplier call or real credential is involved.
function harness(t, overrides = {}) {
  const calls = [];
  const cleanup = [];
  const clocks = new Map();
  const batch = vue.reactive({
    codexDialogOpen: false,
    codex: { mode: 'codex', running: true, batch_id: 'synthetic-batch', logs: [], items: [], results: [] },
    codexSummary: { total: 1, done: 0, active: 1, failed: 0, cancelled: 0, pending: 0, reused: 0, synced: 0, newSuccess: 0 },
    codexConnection: 'connected', codexLastUpdated: 1000, codexError: '',
    refreshCodexStatus: async () => { calls.push('refresh'); },
    stopCodex: () => calls.push('stop'),
    stopCodexPolling: () => calls.push('stop-polling'),
    retryCodexFailed: () => calls.push('retry'),
    canRetryImport: () => false,
    ...overrides,
  });
  const source = readFileSync(new URL('../src/new/views/accounts/CodexLiveDialog.vue', import.meta.url), 'utf8')
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const imports = {
    vue: {
      ...vue,
      watch(...args) { const stop = vue.watch(...args); cleanup.push(stop); return stop; },
      onBeforeUnmount(fn) { cleanup.push(fn); },
    },
    '../../stores/batchops': { useBatchOpsStore: () => batch },
    '../../stores/ui': { useUiStore: () => ({ notify() {} }) },
    '../../composables/useCopy': { useCopy: () => ({ copiedKey: vue.ref(''), copyText: async () => true }) },
  };
  const exports = {};
  const { outputText } = ts.transpileModule(source + '\nexport { showLogs, logFilter, followLogs, logsEl, filteredLogs, items, close, logScroll, progress };', {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  runInNewContext(outputText, {
    exports, require(id) { assert.ok(id in imports, id); return imports[id]; },
    defineEmits: () => () => {},
    setInterval(fn) { const id = {}; clocks.set(id, fn); return id; },
    clearInterval(id) { clocks.delete(id); },
    Date, Blob, URL, setTimeout, clearTimeout,
  });
  t.after(() => { for (const fn of cleanup) fn(); clocks.clear(); });
  return { ...exports, batch, calls, clocks };
}
const line = (m, t = '11:24:45', level = 'info') => ({ t, level, m });
const flush = async () => { await vue.nextTick(); await vue.nextTick(); };

test('opening a running task exposes current logs and requests a fresh snapshot', async t => {
  const h = harness(t);
  h.batch.codex.logs = [line('alpha@example.test | 接码获取号码(第 3/20 次)')];
  h.showLogs.value = false;
  h.batch.codexDialogOpen = true;
  await flush();
  assert.equal(h.showLogs.value, true);
  assert.equal(h.filteredLogs.value.length, 1);
  assert.equal(h.calls.filter(value => value === 'refresh').length, 1);
  assert.equal(h.clocks.size, 1);
});

test('closing then reopening restores all live logs and latest position without stopping or retrying', async t => {
  const h = harness(t);
  h.logsEl.value = { scrollTop: 0, scrollHeight: 1600, clientHeight: 180 };
  h.batch.codexDialogOpen = true;
  await flush();
  h.showLogs.value = false;
  h.logFilter.value = 'error';
  h.followLogs.value = false;
  h.close();
  await flush();
  h.batch.codex.logs = [line('alpha@example.test | 正在获取下一号码')];
  h.logsEl.value.scrollTop = 0;
  h.batch.codexDialogOpen = true;
  await flush();
  assert.equal(h.showLogs.value, true);
  assert.equal(h.logFilter.value, 'all');
  assert.equal(h.followLogs.value, true);
  assert.equal(h.logsEl.value.scrollTop, 1600);
  assert.equal(h.batch.codex.running, true);
  assert.deepEqual(h.calls, ['refresh', 'refresh']);
});

test('mounting an already open dialog starts its clock and refreshes logs', async t => {
  const h = harness(t, { codexDialogOpen: true });
  await flush();
  assert.equal(h.showLogs.value, true);
  assert.equal(h.clocks.size, 1);
  assert.deepEqual(h.calls, ['refresh']);
});

test('800-line rollover continues following the newest log when the array length is unchanged', async t => {
  const h = harness(t);
  const logs = Array.from({ length: 800 }, (_, i) => line(`alpha@example.test | entry ${i}`));
  h.batch.codex.logs = logs;
  h.logsEl.value = { scrollTop: 0, scrollHeight: 20000, clientHeight: 180 };
  h.batch.codexDialogOpen = true;
  await flush();
  h.logsEl.value.scrollTop = 19700;
  h.batch.codex.logs = [...logs.slice(1), line('alpha@example.test | newest entry')];
  await flush();
  assert.equal(h.batch.codex.logs.length, 800);
  assert.equal(h.filteredLogs.value[799].m, 'alpha@example.test | newest entry');
  assert.equal(h.logsEl.value.scrollTop, 20000);
});

test('manually reading earlier logs pauses following while new logs continue arriving', async t => {
  const h = harness(t);
  h.logsEl.value = { scrollTop: 0, scrollHeight: 1000, clientHeight: 200 };
  h.batch.codexDialogOpen = true;
  await flush();
  h.logsEl.value.scrollTop = 100;
  h.logScroll();
  h.batch.codex.logs = [line('alpha@example.test | a newer line')];
  await flush();
  assert.equal(h.followLogs.value, false);
  assert.equal(h.logsEl.value.scrollTop, 100);
  assert.equal(h.filteredLogs.value.length, 1);
});

test('new logs do not scroll a closed or collapsed panel', async t => {
  const h = harness(t);
  h.logsEl.value = { scrollTop: 10, scrollHeight: 1000, clientHeight: 200 };
  h.batch.codex.logs = [line('alpha@example.test | closed log')];
  await flush();
  assert.equal(h.logsEl.value.scrollTop, 10);
  h.batch.codexDialogOpen = true;
  await flush();
  h.showLogs.value = false;
  h.logsEl.value.scrollTop = 20;
  h.batch.codex.logs = [line('alpha@example.test | collapsed log')];
  await flush();
  assert.equal(h.logsEl.value.scrollTop, 20);
});

test('active account rows receive only their exact email-prefixed live logs before completion', t => {
  const h = harness(t);
  h.batch.codex.items = [
    { email: 'alpha@example.test', status: 'active', stage: 'acquiring', attempt: 3 },
    { email: 'other@example.test', status: 'active', stage: 'password' },
  ];
  h.batch.codex.logs = [
    line('xalpha@example.test | Wrong prefix'),
    line('alpha@example.test.evil | Wrong suffix'),
    line('Alpha@Example.Test | First account'),
    line('other@example.test | Second account'),
    line('Unscoped batch log'),
  ];
  assert.equal(h.items.value[0].logs.length, 1);
  assert.equal(h.items.value[0].logs[0].m, 'First account');
  assert.equal(h.items.value[1].logs[0].m, 'Second account');
  assert.equal(h.items.value[0].attempt, 3);
});

test('completed account rows retain finalized logs over a truncated global log buffer', t => {
  const h = harness(t);
  h.batch.codex.items = [{ email: 'alpha@example.test', status: 'active', stage: 'token' }];
  h.batch.codex.results = [{ email: 'alpha@example.test', status: 'success', logs: [line('Full finalized history')] }];
  h.batch.codex.logs = [line('alpha@example.test | Only remaining global line')];
  assert.equal(h.items.value[0].status, 'success');
  assert.equal(h.items.value[0].logs[0].m, 'Full finalized history');
});

test('completed-account progress does not count each phone-number retry as a completed account', t => {
  const h = harness(t);
  h.batch.codex.items = [{ email: 'alpha@example.test', status: 'active', attempt: 3, max_phone_attempts: 20 }];
  assert.equal(h.progress.value, 0);
  assert.equal(h.items.value[0].attempt, 3);
  h.batch.codexSummary.done = 1;
  assert.equal(h.progress.value, 100);
});

test('live logs precede account results and the taskbar provides an explicit reopening control', () => {
  const dialog = readFileSync(new URL('../src/new/views/accounts/CodexLiveDialog.vue', import.meta.url), 'utf8');
  assert.ok(dialog.indexOf('class="live-log-panel"') < dialog.indexOf('class="results-toolbar"'));
  const bar = readFileSync(new URL('../src/new/views/accounts/SmsTaskBar.vue', import.meta.url), 'utf8');
  assert.match(bar, /@click="batch\.openCodexDialog\(\)"[^>]*>[\s\S]*?实时日志/);
});
