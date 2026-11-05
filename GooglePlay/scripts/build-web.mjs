import { spawnSync } from "node:child_process";
import {
  chmodSync,
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const projectRoot = path.resolve(path.dirname(scriptPath), "..");

export function requiredSourcePaths(root) {
  return {
    server: path.join(root, "web", "server.py"),
    static: path.join(root, "web", "static"),
    catalog: path.join(root, "web", "catalog", "catalog.py"),
    requirements: path.join(root, "web", "catalog", "requirements.txt"),
    demo: path.resolve(root, "..", "demo.xlsx"),
    apps: path.resolve(root, "..", "apps.json"),
    start: path.join(root, "scripts", "start-web.mjs"),
    runtimePort: path.join(root, "scripts", "runtime-port.mjs"),
    goModule: path.join(root, "go.mod"),
  };
}

export function runtimePythonPath(root, platform = process.platform) {
  return platform === "win32"
    ? path.join(root, "web", "catalog", ".venv", "Scripts", "python.exe")
    : path.join(root, "web", "catalog", ".venv", "bin", "python");
}

export function runtimeOutputPath(home = homedir()) {
  return path.join(home, ".appstore_google_updata", "GooglePlay", "dist");
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd ?? projectRoot,
    env: process.env,
    stdio: "inherit",
  });
  if (result.error) {
    throw new Error(`无法执行 ${command}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${command} 执行失败，退出码 ${result.status}`);
  }
}

function assertSources(paths) {
  const missing = Object.entries(paths)
    .filter(([, value]) => !existsSync(value))
    .map(([name, value]) => `${name}: ${value}`);
  if (missing.length) {
    throw new Error(`构建缺少源文件：\n${missing.join("\n")}`);
  }
}

function copyRuntimeSources(paths, distRoot) {
  const webRoot = path.join(distRoot, "web");
  const catalogRoot = path.join(webRoot, "catalog");
  const staticRoot = path.join(webRoot, "static");

  mkdirSync(catalogRoot, { recursive: true });
  mkdirSync(path.join(distRoot, "key"), { recursive: true });
  rmSync(staticRoot, { recursive: true, force: true });
  cpSync(paths.static, staticRoot, { recursive: true });
  copyFileSync(paths.server, path.join(webRoot, "server.py"));
  copyFileSync(paths.catalog, path.join(catalogRoot, "catalog.py"));
  copyFileSync(paths.requirements, path.join(catalogRoot, "requirements.txt"));
  copyFileSync(paths.demo, path.join(distRoot, "demo.xlsx"));
  copyFileSync(paths.apps, path.join(distRoot, "key", "apps.json"));
  copyFileSync(paths.start, path.join(distRoot, "start.mjs"));
  copyFileSync(paths.runtimePort, path.join(distRoot, "runtime-port.mjs"));
}

function buildGplay(distRoot) {
  const executable = process.platform === "win32" ? "gplay.exe" : "gplay";
  const destination = path.join(distRoot, executable);
  const temporary = path.join(distRoot, `.${executable}.next`);
  rmSync(temporary, { force: true });
  run("go", ["build", "-trimpath", "-o", temporary, "."]);
  renameSync(temporary, destination);
  if (process.platform !== "win32") chmodSync(destination, 0o755);
}

function preparePython(distRoot) {
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const runtimePython = runtimePythonPath(distRoot);
  if (!existsSync(runtimePython)) {
    run(python, ["-m", "venv", path.dirname(path.dirname(runtimePython))]);
  }
  run(runtimePython, [
    "-m",
    "pip",
    "install",
    "--disable-pip-version-check",
    "--requirement",
    path.join(distRoot, "web", "catalog", "requirements.txt"),
  ]);
  run(runtimePython, ["-c", "import openpyxl"]);
}

export function build(root = projectRoot) {
  const paths = requiredSourcePaths(root);
  assertSources(paths);
  const distRoot = runtimeOutputPath();
  mkdirSync(distRoot, { recursive: true });
  copyRuntimeSources(paths, distRoot);
  buildGplay(distRoot);
  preparePython(distRoot);
  writeFileSync(
    path.join(distRoot, "BUILD_INFO.json"),
    `${JSON.stringify({ builtAt: new Date().toISOString(), platform: process.platform, arch: process.arch }, null, 2)}\n`,
  );
  console.log(`\n构建完成：${distRoot}`);
  console.log("源码启动命令：npm start");
  console.log("默认地址：http://127.0.0.1:8787");
}

if (process.argv[1] && path.resolve(process.argv[1]) === scriptPath) {
  try {
    build();
  } catch (error) {
    console.error(`\n构建失败：${error.message}`);
    process.exitCode = 1;
  }
}
