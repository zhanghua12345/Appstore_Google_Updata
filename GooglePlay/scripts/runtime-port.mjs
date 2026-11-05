import { createServer } from "node:net";

export function portIsAvailable(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const probe = createServer();
    probe.unref();
    probe.once("error", () => resolve(false));
    probe.listen({ host, port, exclusive: true }, () => {
      probe.close(() => resolve(true));
    });
  });
}

export async function selectPort({
  requestedPort,
  host = "127.0.0.1",
  explicit = false,
  maxAttempts = 50,
  check = portIsAvailable,
}) {
  const start = Number(requestedPort);
  if (!Number.isInteger(start) || start < 1 || start > 65535) {
    throw new Error(`端口无效：${requestedPort}`);
  }
  if (explicit) {
    if (await check(start, host)) return start;
    throw new Error(`端口 ${start} 已被占用，请停止旧服务或修改 PORT`);
  }
  for (let offset = 0; offset < maxAttempts && start + offset <= 65535; offset += 1) {
    const candidate = start + offset;
    if (await check(candidate, host)) return candidate;
  }
  throw new Error(`从端口 ${start} 开始没有找到可用端口`);
}
