import { existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { selectPort } from "./runtime-port.mjs";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const packaged = existsSync(path.join(scriptDir, "web", "server.py"));
const deployRoot = packaged ? scriptDir : path.resolve(scriptDir, "..");
const python = packaged
  ? (process.platform === "win32"
      ? path.join(deployRoot, "web", "catalog", ".venv", "Scripts", "python.exe")
      : path.join(deployRoot, "web", "catalog", ".venv", "bin", "python"))
  : (process.env.PYTHON || "python3");
const server = path.join(deployRoot, "web", "server.py");
const catalog = path.join(deployRoot, "web", "catalog", "catalog.py");
const gplay = path.join(deployRoot, process.platform === "win32" ? "gplay.exe" : "gplay");
const jobsDir = process.env.GOOGLE_PLAY_WEB_JOBS || path.join(
  homedir(), ".appstore_google_updata", "GooglePlay", "jobs"
);

for (const required of [...(packaged ? [python] : []), server, catalog, gplay]) {
  if (!existsSync(required)) {
    console.error(`运行文件缺失：${required}`);
    console.error("请检查本项目运行文件");
    process.exit(1);
  }
}

const host = process.env.GOOGLE_PLAY_WEB_HOST || process.env.HOST || "127.0.0.1";
const configuredPort = process.env.GOOGLE_PLAY_WEB_PORT || process.env.PORT;
let port;
try {
  port = await selectPort({
    requestedPort: configuredPort || 8787,
    host,
    explicit: Boolean(configuredPort),
  });
} catch (error) {
  console.error(`服务启动失败：${error.message}`);
  process.exit(1);
}
if (!configuredPort && port !== 8787) {
  console.log(`端口 8787 已被占用，自动改用 http://${host}:${port}`);
}
const environment = {
  ...process.env,
  GOOGLE_PLAY_CATALOG_SCRIPT: catalog,
  GOOGLE_PLAY_WEB_HOST: host,
  GOOGLE_PLAY_WEB_JOBS: jobsDir,
  GOOGLE_PLAY_WEB_PORT: String(port),
  PYTHONDONTWRITEBYTECODE: "1",
  PATH: `${deployRoot}${path.delimiter}${process.env.PATH || ""}`,
};
if (packaged) environment.GOOGLE_PLAY_CATALOG_PYTHON = python;
mkdirSync(jobsDir, { recursive: true });

const child = spawn(python, [server], {
  cwd: jobsDir,
  env: environment,
  stdio: "inherit",
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}

child.on("error", (error) => {
  console.error(`服务启动失败：${error.message}`);
  process.exitCode = 1;
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
