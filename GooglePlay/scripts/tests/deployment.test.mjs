import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import {
  requiredSourcePaths,
  runtimeOutputPath,
  runtimePythonPath,
} from "../build-web.mjs";
import { selectPort } from "../runtime-port.mjs";

test("deployment build requires every runtime source", () => {
  const root = path.resolve("/project");
  const required = requiredSourcePaths(root);

  assert.equal(required.server, path.join(root, "web", "server.py"));
  assert.equal(required.catalog, path.join(root, "web", "catalog", "catalog.py"));
  assert.equal(required.requirements, path.join(root, "web", "catalog", "requirements.txt"));
  assert.equal(required.demo, path.join(root, "..", "demo.xlsx"));
  assert.equal(required.apps, path.join(root, "..", "apps.json"));
});

test("deployment resolves the virtualenv Python for each platform", () => {
  assert.equal(
    runtimePythonPath("/deploy", "linux"),
    path.join("/deploy", "web", "catalog", ".venv", "bin", "python"),
  );
  assert.equal(
    runtimePythonPath("C:\\deploy", "win32"),
    path.join("C:\\deploy", "web", "catalog", ".venv", "Scripts", "python.exe"),
  );
});

test("deployment output stays outside the project", () => {
  assert.equal(
    runtimeOutputPath("/home/user"),
    path.join("/home/user", ".appstore_google_updata", "GooglePlay", "dist"),
  );
});

test("default startup selects the next available port", async () => {
  const checked = [];
  const port = await selectPort({
    requestedPort: 8787,
    explicit: false,
    check: async (candidate) => {
      checked.push(candidate);
      return candidate === 8789;
    },
  });

  assert.equal(port, 8789);
  assert.deepEqual(checked, [8787, 8788, 8789]);
});

test("explicit startup port never changes silently", async () => {
  await assert.rejects(
    selectPort({
      requestedPort: 8787,
      explicit: true,
      check: async () => false,
    }),
    /8787.*占用/,
  );
});
