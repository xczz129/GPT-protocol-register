import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

function load(path, imports, extra = '') {
  let source = readFileSync(new URL(path, import.meta.url), 'utf8');
  if (path.endsWith('.vue')) source = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const { outputText } = ts.transpileModule(source + extra, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }, fileName: 'fixture.ts' });
  const exports = {};
  runInNewContext(outputText, { exports, require(name) { assert.ok(name in imports, name); return imports[name]; } });
  return exports;
}

function settings() {
  const cfg = { enabled: true, provider: 'smsbower', provider_label: 'SMSBower', country_order: '6', timeout_seconds: 180, max_phone_attempts: 2, max_price: '0.25', configured: true };
  const saved = [];
  const notices = [];
  const ops = { smsConfig: cfg, smsBalance: null, async saveSmsConfig(body) { saved.push(body); }, async testSms() { return { ok: true, message: 'queried saved SMSBower' }; } };
  const vue = { reactive: value => value, ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }), watch: (source, callback, options) => { if (options?.immediate) callback(source()); } };
  const module = load('../src/new/views/system/OpsSms.vue', {
    vue, '../../api/ops': {}, '../../stores/operations': { useOperationsStore: () => ops },
    '../../stores/ui': { useUiStore: () => ({ notify: (...args) => notices.push(args) }) },
  }, '\nexport { form, buildBody, save, testBalance, hasUnsavedChanges, savedProviderLabel };');
  return { ...module, ops, saved, notices };
}

test('saved price cap is displayed and edited numeric input is submitted as a string', () => {
  const h = settings();
  assert.equal(h.form.maxPrice, '0.25');
  h.form.maxPrice = 0.4;
  assert.equal(h.buildBody().max_price, '0.4');
  assert.equal(h.hasUnsavedChanges.value, true);
});

test('SMS settings submit only supported provider fields', () => {
  const h = settings();
  h.ops.smsConfig.firefox_api_token = 'synthetic-obsolete-key';
  h.ops.smsConfig.firefox_docking_code = 'synthetic-obsolete-code';
  const body = h.buildBody();
  assert.equal(body.provider, 'smsbower');
  assert.ok(Object.keys(body).every((key) => !key.startsWith('firefox')));
  assert.doesNotMatch(JSON.stringify(body), /synthetic-obsolete/);
  assert.equal(Object.hasOwn(h.form, 'firefoxToken'), false);
});

test('invalid price and fractional phone limit stop before saving settings', async () => {
  const h = settings();
  h.form.maxPrice = '-1';
  await h.save();
  assert.equal(h.saved.length, 0);
  assert.match(h.notices[0][0], /最高价/);
  h.form.maxPrice = 0;
  await h.save();
  assert.equal(h.saved.length, 0);
  assert.match(h.notices[1][0], /最高价/);
  h.form.maxPrice = '';
  h.form.maxPhoneAttempts = 1.5;
  await h.save();
  assert.equal(h.saved.length, 0);
  assert.match(h.notices[2][0], /整数/);
});

test('zero means unlimited and positive phone limits are not capped at twenty', () => {
  const h = settings();
  h.form.maxPhoneAttempts = 0;
  assert.equal(h.buildBody().max_phone_attempts, 0);
  h.form.maxPhoneAttempts = 130;
  assert.equal(h.buildBody().max_phone_attempts, 130);
  h.form.maxPhoneAttempts = -1;
  assert.throws(() => h.buildBody(), /非负整数/);
});

test('editing unsaved credentials retains the saved configuration for balance query', async () => {
  const h = settings();
  h.form.apiKey = 'synthetic-unsaved-key';
  assert.equal(h.hasUnsavedChanges.value, true);
  assert.equal(h.savedProviderLabel.value, 'SMSBower');
  await h.testBalance();
  assert.equal(h.saved.length, 0);
  assert.match(h.notices[0][0], /saved SMSBower/);
});

function operations(api) {
  const { useOperationsStore } = load('../src/new/stores/operations.ts', {
    '../api/ops': api,
    pinia: { defineStore: (_name, options) => () => {
      const result = options.state();
      for (const [name, method] of Object.entries(options.actions)) result[name] = method.bind(result);
      return result;
    } },
  });
  return useOperationsStore();
}

test('balance retains actual queried provider, amount and timestamp', async () => {
  const ops = operations({ testSmsConfig: async () => ({ ok: true, message: 'ok', provider: 'smsbower', provider_label: 'SMSBower', checked_at: '2026-09-11T01:00:00Z', balance: { balance: '12.34', points: '50' } }) });
  ops.smsConfig = { provider: 'smsbower', provider_label: 'SMSBower' };
  await ops.testSms();
  assert.equal(ops.smsBalance.provider, 'smsbower');
  assert.equal(ops.smsBalance.amount, '12.34');
  assert.equal(ops.smsBalance.points, '50');
  assert.equal(ops.smsBalance.checkedAt, '2026-09-11T01:00:00Z');
  assert.equal(ops.busy['sms-test'], false);
});

test('balance failure clears old amount and remains visible instead of leaving stale success', async () => {
  const ops = operations({ testSmsConfig: async () => ({ ok: false, message: 'rate limited' }) });
  ops.smsConfig = { provider: 'smsbower', provider_label: 'SMSBower' };
  ops.smsBalance = { amount: '99', error: '' };
  await ops.testSms();
  assert.equal(ops.smsBalance.amount, '');
  assert.equal(ops.smsBalance.error, 'rate limited');
});

test('saving settings invalidates the old balance display', async () => {
  const ops = operations({ updateSmsConfig: async () => ({}), fetchSmsConfig: async () => ({ provider: 'smsbower' }) });
  ops.smsBalance = { amount: '99', error: '' };
  await ops.saveSmsConfig({ max_price: '0.5' });
  assert.equal(ops.smsBalance, null);
});
