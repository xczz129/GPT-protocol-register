import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function loadApi(requests) {
  const path = "../src/new/api/accounts.ts";
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: path,
  });
  const exports = {};
  runInNewContext(outputText, {
    exports,
    require(name) {
      assert.equal(name, "./client");
      return {
        api: async (url, options = {}) => {
          requests.push({ url, body: options.body ? JSON.parse(options.body) : null });
          return {};
        },
        jsonBody: (value) => JSON.stringify(value),
      };
    },
    AbortController,
  }, { filename: path });
  return exports;
}

test("new account API submits the selected health channel for start and resume", async () => {
  const requests = [];
  const api = loadApi(requests);

  await api.startHealthCheck(["key-a"], false, 7, "nerver");
  await api.resumeHealthCheck(4, "nerver");

  assert.deepEqual(requests, [
    {
      url: "/api/accounts/health",
      body: { keys: ["key-a"], delete_all: false, workers: 7, channel: "nerver" },
    },
    {
      url: "/api/accounts/health/resume",
      body: { workers: 4, channel: "nerver" },
    },
  ]);
});

test("both account-health dialogs expose v4 and nerver as detection channels", () => {
  const legacy = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const modern = readFileSync(new URL("../src/new/views/accounts/HealthStartDialog.vue", import.meta.url), "utf8");
  const concurrency = readFileSync(new URL("../src/components/ConcurrencyDialog.vue", import.meta.url), "utf8");

  for (const source of [legacy, modern]) {
    assert.match(source, /v4 接口/);
    assert.match(source, /nerver 接口/);
    assert.match(source, /检测渠道/);
  }
  assert.match(legacy, /workers, channel/);
  assert.match(modern, /accounts\.startHealth\([^\n]+channel\.value\)/);
  assert.match(concurrency, /:aria-label="modeLabel"/);
});
