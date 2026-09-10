import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

// Execute the real account store and button handler with synthetic paginated
// API responses. No browser, network, SMS provider or real account is used.
function loadModule(path, imports, globals = {}) {
  let source = readFileSync(new URL(path, import.meta.url), "utf8");
  if (path.endsWith(".vue")) {
    source = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
    source += "\nexport { codexSelected };\n";
  }
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: path.endsWith(".vue") ? `${path}.ts` : path,
  });
  const exports = {};
  runInNewContext(outputText, {
    exports,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
    ...globals,
  }, { filename: path });
  return exports;
}

async function harness({ count = 250 } = {}) {
  const sourceRows = Array.from({ length: count }, (_, index) => ({
    delete_key: `email:account-${index}@example.test`,
    email: `account-${index}@example.test`,
    has_access_token: true,
  }));
  const requests = [];
  const launches = [];
  const notices = [];
  const events = [];
  const refMarker = Symbol("mock-ref");
  const ref = (value) => ({ value, [refMarker]: true });
  const vue = {
    ref,
    shallowRef: ref,
    computed(get) { return { get value() { return get(); }, [refMarker]: true }; },
  };
  const { useAccountsStore } = loadModule("../src/new/stores/accounts.ts", {
    vue,
    pinia: { defineStore: (_name, setup) => setup },
    "../api/accounts": {
      async fetchAccountsPage(limit, offset) {
        requests.push({ limit, offset });
        const items = sourceRows.slice(offset, offset + limit);
        return { items, total: sourceRows.length, revision: 1, returned: items.length };
      },
    },
  });
  // Pinia exposes ref values directly to components.
  const accounts = new Proxy(useAccountsStore(), {
    get(target, key) {
      const value = target[key];
      return value?.[refMarker] ? value.value : value;
    },
    set(target, key, value) {
      if (target[key]?.[refMarker]) target[key].value = value;
      else target[key] = value;
      return true;
    },
  });
  await accounts.load();
  const { codexSelected } = loadModule("../src/new/views/accounts/AccountBatchBar.vue", {
    vue,
    "../../stores/accounts": { useAccountsStore: () => accounts },
    "../../stores/groups": { useGroupsStore: () => ({}) },
    "../../stores/batchops": {
      useBatchOpsStore: () => ({
        async runCodex(emails, concurrency) { launches.push({ emails: [...emails], concurrency }); },
      }),
    },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => notices.push(args) }) },
    "../../composables/useConfirm": { useConfirm: () => async () => true },
    "../../composables/useCopy": { useCopy: () => ({}) },
    "../../api/accounts": {},
  }, { defineEmits: () => (...args) => events.push(args) });
  return { accounts, codexSelected, requests, launches, notices, events, sourceRows };
}

test("SMS submits all 30 cross-page selections after the store loads every server page", async () => {
  const h = await harness();
  assert.deepEqual(h.requests, [{ limit: 200, offset: 0 }, { limit: 1000, offset: 200 }]);
  assert.equal(h.accounts.rows.length, 250);
  h.accounts.selectedKeys = h.sourceRows.slice(0, 30).map((row) => row.delete_key);
  h.accounts.page = 2;
  assert.equal(h.accounts.pageRows.filter((row) => h.accounts.selectedSet.has(row.delete_key)).length, 10);

  await h.codexSelected();

  assert.deepEqual(h.launches, [{ emails: h.sourceRows.slice(0, 30).map((row) => row.email), concurrency: 3 }]);
  assert.deepEqual(h.events, [["codex"]]);
  assert.deepEqual(h.notices, []);
});

test("SMS selection remains actionable when the current page contains none of the selected accounts", async () => {
  const h = await harness();
  h.accounts.selectedKeys = [h.sourceRows[0].delete_key, h.sourceRows[249].delete_key];
  h.accounts.page = 5;
  assert.equal(h.accounts.pageRows.filter((row) => h.accounts.selectedSet.has(row.delete_key)).length, 0);

  await h.codexSelected();

  assert.deepEqual(h.launches[0].emails, [h.sourceRows[0].email, h.sourceRows[249].email]);
  assert.equal(h.launches.length, 1);
});

test("changing a filter preserves the complete selection without including unselected rows", async () => {
  const h = await harness();
  h.accounts.selectedKeys = [h.sourceRows[0].delete_key, h.sourceRows[2].delete_key];
  h.accounts.query = h.sourceRows[1].email;
  assert.equal(h.accounts.filtered.length, 1);

  await h.codexSelected();

  assert.deepEqual(h.launches[0].emails, [h.sourceRows[0].email, h.sourceRows[2].email]);
});

test("a refresh still loading rows cannot launch a partial selection", async () => {
  const h = await harness();
  h.accounts.selectedKeys = [h.sourceRows[0].delete_key, h.sourceRows[249].delete_key];
  h.accounts.rows = h.sourceRows.slice(0, 200);
  h.accounts.loading = true;

  await h.codexSelected();

  assert.equal(h.launches.length, 0);
  assert.equal(h.events.length, 0);
  assert.equal(h.notices[0][1], "warning");
});

test("a deleted or missing selected account prevents a silent partial batch", async () => {
  const h = await harness();
  h.accounts.selectedKeys = [h.sourceRows[0].delete_key, "email:removed@example.test"];

  await h.codexSelected();

  assert.equal(h.launches.length, 0);
  assert.equal(h.events.length, 0);
  assert.equal(h.notices[0][1], "error");
  assert.match(h.notices[0][0], /重新选择/);
});

test("a selected account without an email is reported instead of silently omitted", async () => {
  const h = await harness();
  h.accounts.selectedKeys = [h.sourceRows[0].delete_key, h.sourceRows[1].delete_key];
  h.accounts.rows = h.accounts.rows.map((row, index) => index === 1 ? { ...row, email: " " } : row);

  await h.codexSelected();

  assert.equal(h.launches.length, 0);
  assert.equal(h.notices[0][1], "error");
  assert.match(h.notices[0][0], /缺少邮箱/);
});
