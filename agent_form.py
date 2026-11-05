"""Local platform-credential forms for the agent. No store writes."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from workflow import CREDENTIAL_STEPS, PLATFORMS, Task, WorkflowError

PRIVACY = "凭据仅在当前设备保存，以便下次由你确认后复用；仅用于连接 Apple / Google 官方服务，不写入 Excel、提交报告或 Git。"


class Forms:
    def __init__(self, root):
        self.task = Task(root)
        self.lock = threading.RLock()

    def snapshot(self):
        state = self.task.load()
        selected = state["steps"]["3"]["data"].get("platforms", [])
        # Do not include source documents, tokens or private-key material in JSON.
        return {
            "task_id": state["task_id"],
            "app": state["steps"]["2"]["data"].get("app", ""),
            "target_confirmed": state["steps"]["2"]["confirmed"] and state["steps"]["3"]["confirmed"],
            "platforms": selected, "active_platforms": self.task.active_platforms(state),
            "current_step": self.task.current(state), "status": state["status"],
            "execution": state["steps"]["6"]["data"], "privacy": PRIVACY,
            "materials_later": {str(n): bool(state["steps"][str(n)]["data"].get("later"))
                                for n in (5, 7, 8, 9)},
            "screenshots": state["steps"]["5"]["data"].get("screenshots", []),
            "credentials": {p: self.task.cached_credentials(p) for p in selected},
            "files": {p: [file for file in self.task.credential_files(p)
                          if Path(file["path"]).parent == self.task.root] for p in selected},
            "preflight": state["preflight"], "root": str(self.task.root),
            "workbook": state.get("workbook"),
            "agent_requests": state.get("agent_requests", []),
        }

    def require_target(self, platform):
        state = self.task.load()
        if platform not in PLATFORMS or platform not in state["steps"]["3"]["data"].get("platforms", []):
            raise WorkflowError("本次未选择该平台，不显示也不接收它的资料")
        if not state["steps"]["2"]["confirmed"] or not state["steps"]["3"]["confirmed"]:
            raise WorkflowError("请先确认本次 App 和平台，再确认历史资料")

    def action(self, data):
        with self.lock:
            action = data.get("action")
            if action == "save":
                platform = data.get("platform")
                self.require_target(platform)
                config = data.get("config", {})
                allowed = {"issuer_id", "key_id", "app_id", "bundle_id"} if platform == "appstore" else {"package"}
                if not isinstance(config, dict) or set(config) - allowed:
                    raise WorkflowError("表单只接收账号标识，不接收密钥正文")
                if any(not isinstance(v, str) or len(v) > 512 or "\n" in v for v in config.values()):
                    raise WorkflowError("账号标识格式无效")
                field = "p8_path" if platform == "appstore" else "service_account_path"
                cached = self.task.cached_credentials(platform)
                selected = data.get("file") or cached.get(field)
                if selected:
                    config[field] = self.task.accept_credential_file(platform, selected)
                self.task.credentials(platform, {k: v.strip() for k, v in config.items() if v.strip()})
                if data.get("notes", "").strip():
                    self.task.arrange(data["notes"])
                result = {"message": "资料已在本机确认保存；未写入远端。"}
            elif action == "arrange":
                if not isinstance(data.get("notes"), str):
                    raise WorkflowError("请填写本次安排")
                self.task.arrange(data["notes"])
                state = self.task.load()
                result = {"message": "安排已保存。" if not state["steps"]["6"]["data"].get("needs_agent")
                          else "备注已保存，等待智能体解读并索取需要的资料；不会自行提交。"}
            elif action == "later":
                step = data.get("step")
                if step == 5:
                    self.require_target("appstore")
                elif step in (7, 8):
                    self.require_target("appstore" if step == 7 else "googleplay")
                self.task.later(step)
                result = {"message": "Excel 已标记稍后确认；未获得提交授权。" if step == 9
                          else "已标记稍后填写；目标平台保留，最终检查会列出缺项。"}
            elif action == "check":
                platform = data.get("platform")
                self.require_target(platform)
                result = self.task.preflight(platform, data.get("phase", "initial"))
            elif action == "check-intake":
                result = self.task.check_intake()
            elif action in {"pause", "resume"}:
                getattr(self.task, action)()
                result = {"message": "任务已暂停" if action == "pause" else "已恢复保存的任务节点"}
            elif action == "generate":
                result = self.task.generate()
            elif action == "continue-agent":
                # Continuation is read by the agent, never mistaken for an upload.
                state = self.task.load()
                state["agent_requests"] = [{"message": "继续当前任务", "step": self.task.current(state)}]
                self.task.save(state)
                result = {"message": "继续请求已保存；请回到智能体对话说“继续”，它会读取本机进度。"}
            else:
                raise WorkflowError("未知操作")
            return {"result": result, "state": self.snapshot()}


def make_server(root, port=8790):
    forms = Forms(root)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log form fields or credentials.

        def send(self, status, body, kind="application/json; charset=utf-8"):
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(raw)

        def local_request(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host") == expected

        def do_GET(self):
            if not self.local_request():
                return self.send(403, {"error": "仅接受本机页面请求"})
            path = urlsplit(self.path).path
            if path == "/":
                html = (Path(__file__).parent / "agent_form.html").read_text()
                return self.send(200, html.replace("__FORM_TOKEN__", token).encode(), "text/html; charset=utf-8")
            if path == "/api/state" and self.headers.get("X-Form-Token") == token:
                try:
                    with forms.lock:
                        return self.send(200, forms.snapshot())
                except (WorkflowError, OSError, ValueError):
                    return self.send(400, {"error": "请先在智能体对话创建或恢复任务"})
            return self.send(404, {"error": "未找到"})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.local_request() or self.headers.get("Origin") not in (None, origin) or self.headers.get("X-Form-Token") != token:
                return self.send(403, {"error": "请求来源无效"})
            if self.path != "/api/action":
                return self.send(404, {"error": "未找到"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 32768:
                    raise WorkflowError("表单大小无效，请勿粘贴密钥正文")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise WorkflowError("表单格式无效")
                result = forms.action(data)
                self.send(200, result)
            except WorkflowError as error:
                self.send(400, {"error": str(error)})
            except (ValueError, OSError, KeyError, TypeError):
                self.send(400, {"error": "资料尚不完整或文件无法读取，请补齐后继续"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(root, port=8790):
    server = make_server(root, port)
    print(f"平台资料表单：http://127.0.0.1:{server.server_port}（仅本机，Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
