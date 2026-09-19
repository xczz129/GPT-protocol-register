import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

// Execute the real dialog handlers and opening watchers with synthetic accounts.
// All task launches and settings requests are mocked; no network is available.
function loadDialog(path, bindings, imports, globals) {
  const component = readFileSync(new URL(path, import.meta.url), "utf8");
  const source = component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const { outputText } = ts.transpileModule(`${source}\nexport { ${bindings.join(", ")} };\n`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: `${path}.ts`,
  });
  const exports = {};
  runInNewContext(outputText, {
    exports,
    Error,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
    ...globals,
  }, { filename: path });
  return exports;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const flushPromises = () => new Promise((resolve) => setImmediate(resolve));

function harness(kind, {
  keys = ["email:member@example.test"],
  all = false,
  settings = { health_workers: 12, trial_workers: 6, trial_channel: "v4" },
  settingsRequest,
  launchRequest,
} = {}) {
  const props = { open: false, keys, all, kind };
  const launches = [];
  const notices = [];
  const events = [];
  const watchers = [];
  const recordLaunch = (operation) => (...args) => {
    launches.push({ operation, args: JSON.parse(JSON.stringify(args)) });
    return launchRequest || Promise.resolve();
  };
  const accounts = {
    activeCount: 250,
    health: { running: false },
    healthSubmitting: false,
    startHealth: recordLaunch("health"),
  };
  const batch = {
    startTrial: recordLaunch("trial"),
    startRefreshAt: recordLaunch("refreshAt"),
    startReauthBatch: recordLaunch("reauth"),
  };
  const imports = {
    vue: {
      ref: (value) => ({ value }),
      computed: (get) => ({ get value() { return get(); } }),
      watch(get, callback, options = {}) {
        const watcher = { get, callback, value: get() };
        watchers.push(watcher);
        if (options.immediate) callback(watcher.value);
      },
    },
    "@lucide/vue": {},
    "../../components/ui/AppModal.vue": {},
    "../../components/ui/AppButton.vue": {},
    "../../api/settings": { fetchUiSettings: () => settingsRequest || Promise.resolve(settings) },
    "../../stores/accounts": { useAccountsStore: () => accounts },
    "../../stores/batchops": { useBatchOpsStore: () => batch },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => notices.push(args) }) },
  };
  const dialog = loadDialog(
    kind === "health"
      ? "../src/new/views/accounts/HealthStartDialog.vue"
      : "../src/new/views/accounts/BatchStartDialog.vue",
    ["submit", "concurrency", "submitting", ...(kind === "trial" ? ["trialChannel", "force"] : [])],
    imports,
    {
      defineProps: () => props,
      defineEmits: () => (...args) => events.push(args),
    },
  );
  function setOpen(open) {
    props.open = open;
    for (const watcher of watchers) {
      const value = watcher.get();
      if (value === watcher.value) continue;
      const previous = watcher.value;
      watcher.value = value;
      watcher.callback(value, previous);
    }
  }
  return { dialog, props, accounts, launches, notices, events, setOpen };
}

for (const all of [false, true]) {
  test(`health ${all ? "all" : "selection"} submits only account validity checks with the chosen concurrency`, async () => {
    const keys = all ? [] : ["email:first@example.test", "email:second+child@example.test"];
    const h = harness("health", { keys, all });
    h.setOpen(true);
    await flushPromises();
    assert.equal(h.dialog.concurrency.value, 12);
    h.dialog.concurrency.value = 7;

    await h.dialog.submit();

    assert.deepEqual(h.launches, [{ operation: "health", args: [keys, all, 7] }]);
    assert.deepEqual(h.events, [["close"], ["started", 7]]);
    assert.equal(h.dialog.submitting.value, false);
  });
}

for (const kind of ["health", "reauth", "refreshAt", "trial"]) {
  test(`${kind} preserves concurrency defaults and submissions above former fixed limits`, async () => {
    const h = harness(kind, { settings: { health_workers: 150, reauth_workers: 150, refresh_at_workers: 150, trial_workers: 150 } });
    h.setOpen(true);
    await flushPromises();
    assert.equal(h.dialog.concurrency.value, 150);
    h.dialog.concurrency.value = 275;
    await h.dialog.submit();
    const launch = h.launches[0];
    assert.ok(launch);
    assert.equal(kind === "reauth" ? launch.args[2].concurrency : launch.args[kind === "trial" ? 3 : 2], 275);
  });
  test(`${kind} rejects nonpositive and fractional concurrency without launching`, async () => {
    const h = harness(kind);
    h.setOpen(true);
    await flushPromises();
    for (const invalid of [0, -1, 1.5, NaN, Infinity]) {
      h.dialog.concurrency.value = invalid;
      await h.dialog.submit();
    }
    assert.equal(h.launches.length, 0);
    assert.equal(h.notices.length, 5);
    assert.ok(h.notices.every(([message]) => /正整数/.test(message)));
  });
}

for (const channel of ["v4", "nerver"]) {
  for (const all of [false, true]) {
    test(`trial ${channel} ${all ? "all" : "selection"} preserves the user's channel, scope, force and concurrency`, async () => {
      const keys = all ? [] : ["email:trial@example.test"];
      const h = harness("trial", {
        keys,
        all,
        settings: { health_workers: 12, trial_workers: 6, trial_channel: channel === "v4" ? "nerver" : "v4" },
      });
      h.setOpen(true);
      await flushPromises();
      assert.equal(h.dialog.concurrency.value, 6);
      assert.notEqual(h.dialog.trialChannel.value, channel);
      h.dialog.concurrency.value = 9;
      h.dialog.trialChannel.value = channel;
      h.dialog.force.value = all;

      await h.dialog.submit();

      assert.deepEqual(h.launches, [{ operation: "trial", args: [keys, all, all, 9, channel] }]);
      assert.deepEqual(h.events, [["started", "trial"], ["close"]]);
      assert.equal(h.dialog.submitting.value, false);
    });
  }
}

for (const kind of ["health", "trial"]) {
  test(`${kind} ignores a second submit while the first launch is pending`, async () => {
    const pending = deferred();
    const h = harness(kind, { launchRequest: pending.promise });
    h.setOpen(true);
    await flushPromises();

    const first = h.dialog.submit();
    assert.equal(h.dialog.submitting.value, true);
    await h.dialog.submit();
    assert.equal(h.launches.length, 1);
    assert.equal(h.launches[0].operation, kind);
    assert.equal(h.events.some(([event]) => event === "started"), false);

    pending.resolve();
    await first;
    assert.equal(h.dialog.submitting.value, false);
    assert.equal(h.events.filter(([event]) => event === "started").length, 1);
  });

  test(`${kind} waits for settings and closing the dialog never starts a task`, async () => {
    const pending = deferred();
    const h = harness(kind, { settingsRequest: pending.promise });
    h.setOpen(true);
    await h.dialog.submit();
    assert.deepEqual(h.launches, []);

    h.setOpen(false);
    pending.resolve({ health_workers: 15, trial_workers: 15, trial_channel: "nerver" });
    await flushPromises();

    assert.deepEqual(h.launches, []);
    assert.deepEqual(h.events, []);
    assert.notEqual(h.dialog.concurrency.value, 15);
  });

  test(`${kind} reports a launch failure without emitting a successful start`, async () => {
    const pending = deferred();
    const h = harness(kind, { launchRequest: pending.promise });
    h.setOpen(true);
    await flushPromises();

    const submitted = h.dialog.submit();
    pending.reject(new Error("synthetic launch failure"));
    await submitted;

    assert.equal(h.launches.length, 1);
    assert.equal(h.launches[0].operation, kind);
    assert.equal(h.events.some(([event]) => event === "started"), false);
    assert.deepEqual(h.notices.at(-1), ["synthetic launch failure", "error"]);
    assert.equal(h.dialog.submitting.value, false);
  });
}

test("health cannot launch an empty selection or overlap an existing health job", async () => {
  const h = harness("health", { keys: [] });
  h.setOpen(true);
  await flushPromises();
  await h.dialog.submit();
  assert.deepEqual(h.launches, []);

  h.props.keys = ["email:member@example.test"];
  h.accounts.health.running = true;
  await h.dialog.submit();
  assert.deepEqual(h.launches, []);

  h.accounts.health.running = false;
  h.accounts.healthSubmitting = true;
  await h.dialog.submit();
  assert.deepEqual(h.launches, []);

  h.accounts.healthSubmitting = false;
  await h.dialog.submit();
  assert.deepEqual(h.launches, [{ operation: "health", args: [h.props.keys, false, 12] }]);
});
