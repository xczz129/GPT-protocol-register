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

test("health API preserves legacy channel arguments for start and resume", async () => {
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
