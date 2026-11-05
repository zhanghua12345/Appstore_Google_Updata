#!/usr/bin/env python3
"""Persistent intake for the store-upload agent. No remote writes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
STEP_NAMES = {
    1: "核对商品解读", 2: "确认 App", 3: "选择平台", 4: "确认 Product ID 前缀",
    5: "检查根目录截图", 6: "本次执行安排（可留空）", 7: "App Store 资料",
    8: "Google Play 资料", 9: "生成并确认 Excel",
}
LAST_STEP = 9
CREDENTIAL_STEPS = {"appstore": 7, "googleplay": 8}
PLATFORMS = {"appstore", "googleplay"}
KINDS = {"coins", "bag", "membership"}
PROFILE_FILE = "平台资料.json"
PROFILE_FIELDS = (
    "App", "App Store Bundle ID", "App Store Connect API Issuer ID",
    "App Store Connect API Key", "App Store ID", "App Store p8文件名",
    "App Store 使用图片文件名", "Google Play Android 包名",
    "Google Play 服务账号 JSON文件名",
)
APPLE_PROFILE_FIELDS = PROFILE_FIELDS[1:7]
GOOGLE_PROFILE_FIELDS = PROFILE_FIELDS[7:9]


class WorkflowError(ValueError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_prefix(value: str) -> str:
    base = value.strip().rstrip(".")
    if not re.fullmatch(r"[a-z][a-z0-9_.]*", base) or ".." in base:
        raise WorkflowError("前缀须以小写字母开头，只含小写字母、数字、下划线和点")
    return base + "."


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_items(items):
    if not isinstance(items, list) or not items:
        raise WorkflowError("需要至少一个已解读商品")
    from decimal import Decimal, InvalidOperation
    for item in items:
        if item.get("kind") not in KINDS:
            raise WorkflowError("商品类别须为 coins、bag 或 membership")
        fields = ["price"] if item["kind"] == "coins" else ["intro_price", "renewal_price"]
        for field in fields:
            try:
                price = Decimal(str(item[field]))
                if not price.is_finite() or price <= 0:
                    raise InvalidOperation
            except (KeyError, InvalidOperation):
                raise WorkflowError(f"{item['kind']} 的 {field} 价格无效") from None
        if item["kind"] != "coins" and item.get("period") != "ONE_WEEK":
            raise WorkflowError("当前制表器支持 demo 的周订阅；其他周期需扩展后再制表")
    return items


def bare_product_id(item):
    """Build the same ID stem for intake preview and workbook generation."""
    kind = item["kind"]
    suffix = str(item.get("revision", 1))
    first = str(item["price"] if kind == "coins" else item["intro_price"])
    if kind == "coins":
        return f"coins_{first}_{suffix}"
    stem = "bag" if kind == "bag" else "vip"
    return f"{stem}_week_{first}_{item['renewal_price']}_{suffix}"


def demo_reference_name(app: str, item: dict) -> str:
    suffix = str(item.get("revision", 1))
    if item["kind"] == "coins":
        label = f"Coins {item['price']}_{suffix}"
    else:
        kind = "Weekly Coin Bag" if item["kind"] == "bag" else "Weekly VIP Membership"
        label = f"{kind} {item['intro_price']}_{item['renewal_price']}_{suffix}"
    return f"{app} - {label}"


class Task:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.path = self.root / "当前任务.json"

    def load(self):
        if not self.path.is_file():
            raise WorkflowError("没有当前任务；先创建新任务，不沿用其他 App")
        state = read_json(self.path)
        if state.get("schema") == 1:
            old = state["steps"]
            apple = {"credentials": old["5"]["data"].get("credentials", {})}
            screenshot = {k: v for k, v in old["5"]["data"].items() if k != "credentials"}
            state["steps"] = {str(n): {"name": name, "confirmed": False, "data": {}} for n, name in STEP_NAMES.items()}
            for n in range(1, 5):
                state["steps"][str(n)] = old[str(n)]
            state["steps"]["5"]["data"] = screenshot
            state["steps"]["5"]["confirmed"] = bool(screenshot.get("content_checked"))
            state["steps"]["6"] = {"name": STEP_NAMES[6], "confirmed": True,
                                       "data": {"mode": "submit", "deferred": [], "notes": ""}}
            state["steps"]["7"]["data"] = apple
            state["steps"]["8"]["data"] = old["6"]["data"]
            state["steps"]["9"]["data"] = old["7"]["data"]
            state["schema"] = 2
            state["revision"] += 1
            state["authorization"] = None
            state["preflight"] = {}
            state["workbook"] = None
            if state.get("paused_step") in (6, 7, 8):
                state["paused_step"] = 7
            self.save(state)
        return state

    def save(self, state):
        state["updated_at"] = now()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        return state

    def start(self, source: str, items: list, replace=False):
        if self.path.exists() and not replace:
            raise WorkflowError("已有任务；继续请用 resume，新任务明确指定 --replace")
        validate_items(items)
        state = {
            "schema": 2, "task_id": str(uuid.uuid4()), "status": "active", "revision": 1,
            "source": source, "created_at": now(), "paused_step": None, "reuse_cache": False,
            "steps": {str(n): {"name": name, "confirmed": False, "data": {}}
                      for n, name in STEP_NAMES.items()},
            "screenshot_failures": 0, "preflight": {}, "workbook": None,
            "authorization": None, "results": [],
        }
        state["steps"]["1"]["data"] = {"items": items, "currency": "USD", "base_region": "USA"}
        state["steps"]["6"] = {"name": STEP_NAMES[6], "confirmed": True,
                                   "data": {"mode": "submit", "deferred": [], "notes": ""}}
        return self.save(state)

    def init_profile(self, replace=False):
        """Open a new intake with the tracked, empty profile shape."""
        template = self.root / "平台资料空白模板.json"
        if not template.is_file():
            raise WorkflowError("缺少远程跟踪的 平台资料空白模板.json")
        blank = read_json(template)
        if not isinstance(blank, dict) or tuple(blank) != PROFILE_FIELDS or any(value != "" for value in blank.values()):
            raise WorkflowError("平台资料空白模板.json 的字段或空值不符合入口格式")
        path = self.root / PROFILE_FILE
        if path.exists() and not replace:
            raise WorkflowError("平台资料.json 已存在；新任务请使用 profile-init --replace，旧文件会备份")
        backup = None
        if path.exists():
            directory = self.credential_dir / "profile-backups"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            backup = directory / f"profile-{uuid.uuid4().hex}.json"
            shutil.copyfile(path, backup)
            backup.chmod(0o600)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(blank, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        return {"file": str(path), "ready": False, "backup": str(backup) if backup else None}

    def inspect_intake_profile(self):
        """Only the filled platform's fields gate the rest of a new task."""
        path = self.root / PROFILE_FILE
        if not path.is_file():
            return {}, [], ["先运行 profile-init 生成空白平台资料.json"], {}
        profile = read_json(path)
        if not isinstance(profile, dict) or not any(name in profile for name in PROFILE_FIELDS[1:]):
            raise WorkflowError("平台资料.json 不是九字段空白模板；新任务请先运行 profile-init --replace")
        for name in PROFILE_FIELDS:
            value = profile.get(name, "")
            if not isinstance(value, str) or len(value) > 4096 or "\n" in value:
                raise WorkflowError(f"{name} 必须是单行文字；私钥正文只保留在凭据文件中")
            profile[name] = value.strip()
        selected = []
        if any(profile[name] for name in APPLE_PROFILE_FIELDS):
            selected.append("appstore")
        if any(profile[name] for name in GOOGLE_PROFILE_FIELDS):
            selected.append("googleplay")
        missing = []
        if not profile["App"]:
            missing.append("App")
        if not selected:
            missing.append("至少填写一组 App Store 或 Google Play 资料")
        for platform, fields in (("appstore", APPLE_PROFILE_FIELDS), ("googleplay", GOOGLE_PROFILE_FIELDS)):
            if platform in selected:
                missing.extend(name for name in fields if not profile[name])
        if "appstore" in selected and profile["App Store ID"] and not profile["App Store ID"].isdigit():
            missing.append("App Store ID：填写数字 App Store Connect App ID")
        if "googleplay" in selected and profile["Google Play Android 包名"] and not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", profile["Google Play Android 包名"]):
            missing.append("Google Play Android 包名：格式无效")
        files = {}
        for platform, field, suffix in (("appstore", "App Store p8文件名", ".p8"),
                                        ("googleplay", "Google Play 服务账号 JSON文件名", ".json")):
            if platform not in selected or not profile[field]:
                continue
            name = profile[field]
            if Path(name).name != name or not name.lower().endswith(suffix):
                missing.append(f"{field}：只填写根目录中的 {suffix} 文件名")
                continue
            file = self.root / name
            if not file.is_file() or file.is_symlink():
                missing.append(f"{field}：根目录未找到文件")
                continue
            if platform == "googleplay":
                try:
                    key = read_json(file)
                    if key.get("type") != "service_account" or not key.get("private_key") or not key.get("client_email"):
                        raise ValueError
                except (ValueError, OSError, AttributeError):
                    missing.append(f"{field}：文件不是有效的服务账号 JSON")
                    continue
            files[platform] = file
        if "appstore" in selected and profile["App Store 使用图片文件名"]:
            name = profile["App Store 使用图片文件名"]
            image = self.root / name
            if Path(name).name != name or image.is_symlink() or not image.is_file() or image.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                missing.append("App Store 使用图片文件名：请填写根目录中的 PNG/JPEG 文件名")
            else:
                try:
                    from PIL import Image
                    with Image.open(image) as opened:
                        opened.verify()
                    files["image"] = image
                except (OSError, ValueError):
                    missing.append("App Store 使用图片文件名：图片无法读取")
        return profile, selected, missing, files

    def start_from_profile(self, source, items, replace=False):
        profile, platforms, missing, files = self.inspect_intake_profile()
        if missing:
            raise WorkflowError("请先补齐平台资料.json：" + "；".join(dict.fromkeys(missing)))
        state = self.start(source, items, replace)
        state["profile_first"] = True
        state["profile_sha256"] = digest(self.root / PROFILE_FILE)
        state["steps"]["2"] = {"name": STEP_NAMES[2], "confirmed": True, "data": {"app": profile["App"]}}
        state["steps"]["3"] = {"name": STEP_NAMES[3], "confirmed": True, "data": {"platforms": platforms}}
        if "appstore" in platforms:
            image = self.copy_to_root(files["image"])
            state["steps"]["5"]["data"] = {
                "screenshots": [image.name], "images": [{"file": image.name, "sha256": digest(image)}],
                "content_checked": False,
            }
            state["steps"]["7"]["data"] = {"credentials": {
                "issuer_id": profile["App Store Connect API Issuer ID"],
                "key_id": profile["App Store Connect API Key"], "app_id": profile["App Store ID"],
                "bundle_id": profile["App Store Bundle ID"], "p8_path": str(files["appstore"]),
            }}
        if "googleplay" in platforms:
            state["steps"]["8"]["data"] = {"credentials": {
                "package": profile["Google Play Android 包名"],
                "service_account_path": str(files["googleplay"]),
            }}
        return self.save(state)

    def require_current_profile(self, state):
        if state.get("profile_first"):
            path = self.root / PROFILE_FILE
            if not path.is_file() or digest(path) != state.get("profile_sha256"):
                raise WorkflowError("平台资料.json 已修改；请先运行 profile 重新核对并导入")

    @staticmethod
    def current(state):
        for number in range(1, LAST_STEP + 1):
            if state["steps"][str(number)]["confirmed"] or state["steps"][str(number)]["data"].get("later"):
                continue
            platform = "appstore" if number == 7 else "googleplay" if number == 8 else None
            access = state.get("preflight", {}).get(f"{platform}_access", {}) if platform else {}
            if access.get("ready") and access.get("revision") == state["revision"]:
                continue
            return number
        return LAST_STEP + 1

    @staticmethod
    def invalidate(state, step):
        for n in range(step, LAST_STEP + 1):
            state["steps"][str(n)]["confirmed"] = False
        # Execution notes are optional, not an extra approval gate.
        state["steps"]["6"]["confirmed"] = True
        state["revision"] += 1
        state["preflight"] = {}
        state["workbook"] = None
        state["authorization"] = None
        state["status"] = "active"
        state["paused_step"] = None
        if step <= 5:
            state["screenshot_failures"] = 0

    def set_step(self, step, data, confirmed=True):
        if step not in {1, 2, 3, 4}:
            raise WorkflowError("截图和平台资料必须实际检查；Excel 必须展示并确认")
        state = self.load()
        if step == 1:
            validate_items(data.get("items"))
            if data.get("currency") != "USD" or data.get("base_region") != "USA":
                raise WorkflowError("当前制表器支持 demo 的 USA/USD；其他币种需扩展后再制表")
        elif step == 2:
            if not isinstance(data.get("app"), str) or not data["app"].strip():
                raise WorkflowError("请提供本次 App 名称")
            data = {"app": data["app"].strip()}
        elif step == 3:
            if not data.get("platforms") or not set(data["platforms"]) <= PLATFORMS:
                raise WorkflowError("平台须为 appstore、googleplay 或两者")
            data = {"platforms": sorted(set(data["platforms"]))}
        elif step == 4:
            if not isinstance(data.get("enabled"), bool):
                raise WorkflowError("请先选择是否使用前缀")
            if data["enabled"]:
                data = {"enabled": True, "prefix": normalize_prefix(data.get("prefix", ""))}
            else:
                if "appstore" in state["steps"]["3"]["data"].get("platforms", []):
                    raise WorkflowError("按本项目约定，App Store 商品需填写该 App 的前缀")
                data = {"enabled": False, "prefix": ""}
        data = {key: value for key, value in data.items() if key != "later"}
        old = state["steps"][str(step)]
        old_data = {key: value for key, value in old["data"].items() if key != "later"}
        if old_data != data:
            if step == 2:
                # A different App must never inherit the previous App's targets.
                for credential_step in CREDENTIAL_STEPS.values():
                    state["steps"][str(credential_step)]["data"] = {}
                old_app = old["data"].get("app", "")
                if old_app and old_app.casefold() != data["app"].casefold():
                    state["steps"]["6"]["data"] = {"mode": "submit", "deferred": [], "notes": ""}
                    for reset_step in (3, 4, 5):
                        state["steps"][str(reset_step)]["data"] = {}
                    state["reuse_cache"] = False
            self.invalidate(state, step)
            if step == 2 and state["steps"]["1"]["confirmed"]:
                state["steps"]["1"]["confirmed"] = False
            if step == 4 and state["steps"]["1"]["confirmed"]:
                try:
                    suggested_prefix = normalize_prefix(state["steps"]["2"]["data"]["app"].lower())
                except WorkflowError:
                    suggested_prefix = None
                if data["prefix"] != suggested_prefix:
                    state["steps"]["1"]["confirmed"] = False
        elif old["confirmed"] and not confirmed:
            self.invalidate(state, step)
        state["steps"][str(step)] = {"name": STEP_NAMES[step], "data": data, "confirmed": confirmed}
        saved = self.save(state)
        app = state["steps"]["2"]["data"].get("app")
        if confirmed and app and state["steps"]["2"]["confirmed"] and state.get("reuse_cache") and step in (3, 4):
            preferences = self.suggestions()
            preferences["platforms" if step == 3 else "prefix"] = data["platforms"] if step == 3 else data
            self.cache_credentials(app, "_preferences", preferences)
        return saved

    def pause(self):
        state = self.load()
        state["status"] = "paused"
        state["paused_step"] = self.current(state)
        return self.save(state)

    def later(self, step):
        if step not in {1, 2, 3, 4, 5, 7, 8, 9}:
            raise WorkflowError("该步骤不能标记稍后填写")
        state = self.load()
        platform = "appstore" if step == 7 else "googleplay"
        if step in (7, 8) and platform not in state["steps"]["3"]["data"].get("platforms", []):
            raise WorkflowError("本次未选择该平台")
        if step == 9:
            state["authorization"] = None
            state["preflight"] = {k: v for k, v in state["preflight"].items() if not k.endswith("_final")}
            state["status"] = "active"
        elif state["steps"][str(step)]["confirmed"] or state.get("workbook"):
            self.invalidate(state, step)
        state["steps"][str(step)]["data"]["later"] = True
        state["steps"][str(step)]["confirmed"] = False
        return self.save(state)

    def inventory(self):
        """List current root inputs without revealing credential contents."""
        files = []
        for path in sorted(self.root.iterdir()):
            if not path.is_file() or path.is_symlink():
                continue
            suffix = path.suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg"}:
                kind = "image"
            elif suffix == ".p8":
                kind = "apple_key"
            elif suffix == ".xlsx" and path.name != "demo.xlsx":
                kind = "workbook"
            elif suffix == ".json" and path.name not in {"apps.json", "当前任务.json"}:
                try:
                    data = read_json(path)
                    kind = "google_key" if isinstance(data, dict) and data.get("type") == "service_account" else None
                except (OSError, ValueError):
                    kind = None
            else:
                kind = None
            if kind:
                files.append({"name": path.name, "kind": kind})
        return {"root": str(self.root), "files": files}

    def preview(self):
        """Show complete candidate IDs after the App name is recorded."""
        state = self.load()
        self.require_current_profile(state)
        data = state["steps"]["1"]["data"]
        columns = ["类别", "ID", "一次性价", "首周价", "续订价"]
        app = state["steps"]["2"]["data"].get("app")
        if not app:
            return {"step": 2, "columns": columns, "rows": [],
                    "question": "本次 App 叫什么？先记录 App 名称，再展示带前缀的完整候选 ID。"}
        prefix_step = state["steps"]["4"]
        if prefix_step["confirmed"]:
            prefix = prefix_step["data"].get("prefix", "")
        else:
            try:
                prefix = normalize_prefix(app.lower())
            except WorkflowError:
                return {"step": 4, "columns": columns, "rows": [], "app": app,
                        "question": "App 名称不能直接用作 Product ID 前缀；请提供以小写字母开头、只含小写字母、数字、下划线和点的前缀。"}
        rows = []
        labels = {"coins": "金币", "bag": "金币包", "membership": "会员"}
        def display(value):
            return "-" if value is None or isinstance(value, str) and not value.strip() else value
        for item in data["items"]:
            kind = item["kind"]
            pid = item.get("source_product_id") or item.get("product_id")
            if isinstance(pid, str):
                pid = pid.strip()
            row = {"类别": labels[kind], "ID": display(pid or prefix + bare_product_id(item)),
                   "一次性价": display(item.get("price")) if kind == "coins" else "-",
                   "首周价": display(item.get("intro_price")) if kind != "coins" else "-",
                   "续订价": display(item.get("renewal_price")) if kind != "coins" else "-"}
            if pid:
                if prefix and re.fullmatch(r"(?:coins|bag|vip|membership)_[a-z0-9_.]+", pid):
                    row["ID"] = prefix + pid
                    row["ID说明"] = "源 ID 未带 App 前缀；完整 ID 待确认和在线查重"
                elif prefix and not pid.startswith(prefix):
                    row["ID说明"] = "源 ID 与本次 App 前缀不符；待核对，不自动修改"
            else:
                row["ID说明"] = "按 demo 生成的完整候选 ID；待前缀确认和在线查重"
            rows.append(row)
        periods = sorted({item["period"] for item in data["items"] if item.get("period")})
        return {"step": 1, "columns": columns, "rows": rows, "app": app,
                "currency": data.get("currency", "-"), "renewal_periods": periods,
                "question": "以上商品解读和完整候选 ID 是否正确？" if state["steps"]["3"]["confirmed"]
                else "以上商品解读和完整候选 ID 是否正确？要上传 App Store、Google Play，还是两个平台？"}

    def resume(self):
        state = self.load()
        state["status"] = "authorized_pending_fresh_checks" if state.get("authorization") else "active"
        state["paused_step"] = None
        return self.save(state)

    def prerequisites(self, step):
        state = self.load()
        self.require_current_profile(state)
        if state["status"] == "paused":
            raise WorkflowError("任务已暂停，请先继续当前任务")
        active = self.active_platforms(state)
        for platform, number in CREDENTIAL_STEPS.items():
            if platform not in active:
                state["steps"][str(number)]["confirmed"] = True
                state["steps"][str(number)]["data"]["deferred"] = True
        if "appstore" not in active:
            state["steps"]["5"]["confirmed"] = True
        self.save(state)
        missing = [n for n in range(1, step) if not state["steps"][str(n)]["confirmed"]]
        if missing:
            raise WorkflowError(f"请先完成第 {missing[0]} 步：{STEP_NAMES[missing[0]]}")
        return state

    def screenshots(self, files=None, count_failure=True):
        state = self.prerequisites(5)
        if "appstore" not in self.active_platforms(state):
            state["steps"]["5"]["confirmed"] = True
            return self.save(state)
        # “我已经放根文件” does not require the user to repeat a filename.
        # Scan only direct root images; the agent still checks relevance visually.
        files = files or [p.name for p in sorted(self.root.iterdir())
                          if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
        images = []
        from PIL import Image
        for raw in files:
            path = Path(raw)
            path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
            if not path.is_file():
                continue
            try:
                with Image.open(path) as image:
                    if image.format not in {"PNG", "JPEG"}:
                        continue
                    image.verify()
                path = self.copy_to_root(path)
                images.append({"file": path.name, "sha256": digest(path)})
            except (OSError, ValueError, SyntaxError):
                continue
        if not images:
            if state["steps"]["5"]["confirmed"] or state.get("workbook"):
                self.invalidate(state, 5)
            if count_failure:
                state["screenshot_failures"] += 1
            state["steps"]["5"]["confirmed"] = False
            self.save(state)
            return {"ok": False, "attempts": state["screenshot_failures"], "paused": False,
                    "message": "审核截图待补；可继续填写后续资料，最终检查会列出缺项"}
        data = state["steps"]["5"]["data"]
        changed = data.get("images") != images
        if changed:
            self.invalidate(state, 5)
        data = {**data, "screenshots": [i["file"] for i in images], "images": images,
                "content_checked": False if changed else data.get("content_checked", False)}
        state["steps"]["5"]["data"] = data
        state["steps"]["5"]["data"].pop("later", None)
        state["screenshot_failures"] = 0
        self.save(state)
        return {"ok": True, "files": data["screenshots"], "needs_visual_check": not data["content_checked"]}

    def mark_screenshot_content(self):
        state = self.prerequisites(5)
        if not state["steps"]["5"]["data"].get("images"):
            raise WorkflowError("请先检查真实截图文件，再由智能体查看购买页面内容")
        state["steps"]["5"]["data"]["content_checked"] = True
        state["steps"]["5"]["data"].pop("later", None)
        state["steps"]["5"]["confirmed"] = True
        return self.save(state)

    @staticmethod
    def active_platforms(state):
        selected = state["steps"]["3"]["data"].get("platforms", [])
        execution = state["steps"]["6"]["data"]
        if execution.get("mode") == "excel_only":
            return []
        return [p for p in selected if p not in execution.get("deferred", [])]

    def arrange(self, message):
        """Recognize common execution requests; retain other notes for the agent."""
        state = self.load()
        text = message.strip()
        normalized = re.sub(r"\s+", "", text.lower()).replace("googlepaly", "googleplay")
        selected = state["steps"]["3"]["data"].get("platforms", [])
        data = dict(state["steps"]["6"]["data"])
        data["notes"] = text
        excel = bool(re.search(r"(?:先|只|仅).{0,8}(?:做|生成|出|制作|整理)?(?:excel|excl|表格|表)", normalized))
        skip = {p for p in PLATFORMS if re.search(
            r"(?:跳过|暂缓|暂不|不提交|不上传|不做|不要(?:提交|上传|处理)?|别(?:提交|上传|处理)?).{0,4}" + (r"(?:appstore|苹果|apple|ios)" if p == "appstore" else r"(?:googleplay|google|安卓|谷歌)"), normalized)}
        positive = re.sub(r"(?:不要|别|暂不|不|跳过|暂缓)(?:先)?(?:提交|上传|处理|做)?(?:appstore|googleplay|google|apple|ios|苹果|安卓|谷歌)", "", normalized)
        submit = {p for p in PLATFORMS if re.search(
            r"(?:先|只|仅)?(?:提交|上传|处理).{0,3}" + (r"(?:appstore|苹果|apple|ios)" if p == "appstore" else r"(?:googleplay|google|安卓|谷歌)"), positive)}
        if re.search(r"(?:不要|别|取消|不能).{0,3}(?:先做|先生成|做|生成)?(?:excel|excl|表格)", normalized):
            data["needs_agent"] = True
        elif excel:
            data.update(mode="excel_only", deferred=sorted(set(selected) | skip), needs_agent=False)
        elif skip or submit:
            unknown = (skip | submit) - set(selected)
            if unknown:
                raise WorkflowError("安排涉及未选择的平台，请先修改第三步的平台选择")
            deferred = set(data.get("deferred", [])) | skip
            if submit:
                deferred |= set(selected) - submit
                deferred -= submit
            data.update(mode="submit", deferred=sorted(deferred), needs_agent=False)
        elif re.search(r"恢复全部|两个平台都(?:提交|上传)|全部(?:提交|上传)", normalized):
            data.update(mode="submit", deferred=[], needs_agent=False)
        else:
            data["needs_agent"] = bool(text)
        if data != state["steps"]["6"]["data"]:
            self.invalidate(state, 5)
        state["steps"]["6"] = {"name": STEP_NAMES[6], "confirmed": True, "data": data}
        return self.save(state)

    def prepare_excel(self):
        """Build a review workbook before deciding which store to submit to."""
        state = self.load()
        self.require_current_profile(state)
        if not state["steps"]["1"]["confirmed"] or not state["steps"]["2"]["confirmed"]:
            raise WorkflowError("先确认本次商品和 App，再生成 Excel")
        app = state["steps"]["2"]["data"]["app"]
        items = state["steps"]["1"]["data"]["items"]
        if any(not item.get("reference_name") for item in items):
            self.invalidate(state, 1)
            for item in items:
                item.setdefault("reference_name", demo_reference_name(app, item))
            state["steps"]["1"]["confirmed"] = True
            state["steps"]["2"]["confirmed"] = True
            self.save(state)
        state = self.load()
        if not state["steps"]["3"]["confirmed"]:
            self.set_step(3, {"platforms": ["appstore", "googleplay"]})
        state = self.load()
        if not state["steps"]["4"]["confirmed"]:
            self.set_step(4, {"enabled": True, "prefix": normalize_prefix(app.lower())})
        state = self.load()
        if state["steps"]["6"]["data"].get("mode") != "excel_only":
            self.arrange("只做Excel")
        return self.check_intake()

    def choose_submission(self, platforms):
        """Select write targets after showing Excel without changing its contents."""
        state = self.load()
        workbook = self.verify_workbook(state)
        if not workbook["shown"]:
            raise WorkflowError("先展示本次 Excel，再选择实际提交平台")
        formatted = set(state["steps"]["3"]["data"].get("platforms", []))
        selected = set(platforms)
        if not selected or not selected <= formatted:
            raise WorkflowError("提交平台须为 Excel 支持的 App Store、Google Play 或两者")
        state["steps"]["6"]["data"] = {"mode": "submit", "deferred": sorted(formatted - selected), "notes": ""}
        state["steps"]["6"]["confirmed"] = True
        screenshot = state["steps"]["5"]["data"]
        state["steps"]["5"]["confirmed"] = "appstore" not in selected or bool(
            screenshot.get("content_checked") and screenshot.get("images"))
        for platform, step in CREDENTIAL_STEPS.items():
            state["steps"][str(step)]["confirmed"] = platform not in selected
            if platform not in selected:
                state["steps"][str(step)]["data"]["deferred"] = True
        state["steps"]["9"]["confirmed"] = False
        state["authorization"] = None
        state["preflight"] = {}
        state["status"] = "active"
        self.save(state)
        return {"platforms": sorted(selected), "file": str(self.root / workbook["file"]),
                "authorized": False}

    def credentials(self, platform, config, replace=False):
        state = self.load()
        if platform not in PLATFORMS:
            raise WorkflowError("未知平台")
        step = CREDENTIAL_STEPS[platform]
        allowed = {"issuer_id", "key_id", "p8_path", "app_id", "bundle_id"} if platform == "appstore" else {"package", "service_account_path"}
        if set(config) - allowed:
            raise WorkflowError("状态只接受凭据路径和账号标识，不保存密钥正文或令牌")
        if any(not isinstance(v, str) or len(v) > 4096 or "\n" in v for v in config.values()):
            raise WorkflowError("凭据标识应为单行文字，密钥正文请保留在本机文件中")
        data = state["steps"][str(step)]["data"]
        config = config if replace else {**data.get("credentials", {}), **config}
        for key in ("p8_path", "service_account_path"):
            if config.get(key):
                path = Path(config[key]).expanduser()
                path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
                config[key] = str(self.copy_to_root(path))
        if data.get("credentials") != config:
            self.invalidate(state, step)
        state["steps"][str(step)]["data"]["credentials"] = config
        saved = self.save(state)
        app = state["steps"]["2"]["data"].get("app")
        if app and state["steps"]["2"]["confirmed"] and state.get("reuse_cache"):
            self.cache_credentials(app, platform, config)
        return saved

    @property
    def credential_dir(self):
        return self.root.parent / (self.root.name + "_credentials")

    def copy_to_root(self, source):
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise WorkflowError("资料文件不存在")
        if source.parent == self.root:
            return source
        destination = self.root / source.name
        if destination.exists() and digest(destination) != digest(source):
            destination = self.root / f"{source.stem}-{digest(source)[:8]}{source.suffix}"
        if destination.exists() and digest(destination) != digest(source):
            raise WorkflowError("根目录有同名且内容不同的资料文件")
        if not destination.exists():
            shutil.copy2(source, destination)
        destination.chmod(0o600)
        return destination

    def cache_credentials(self, app, platform, config):
        directory = self.credential_dir
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / "registry.json"
        records = read_json(path) if path.is_file() else {}
        records.setdefault(app.strip().casefold(), {})[platform] = config
        temporary = directory / "registry.tmp"
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def cached_credentials(self, platform):
        state = self.load()
        app = state["steps"]["2"]["data"].get("app")
        if not app or not state["steps"]["2"]["confirmed"]:
            return {}
        current = state["steps"][str(CREDENTIAL_STEPS[platform])]["data"].get("credentials")
        if current:
            return current
        if not state.get("reuse_cache"):
            return {}
        path = self.credential_dir / "registry.json"
        records = read_json(path) if path.is_file() else {}
        return records.get(app.strip().casefold(), {}).get(platform, {})

    def suggestions(self):
        """Remembered settings are proposals, never current-task confirmations."""
        state = self.load()
        app = state["steps"]["2"]["data"].get("app")
        if not app or not state["steps"]["2"]["confirmed"]:
            return {}
        if not state.get("reuse_cache"):
            return {}
        path = self.credential_dir / "registry.json"
        records = read_json(path) if path.is_file() else {}
        return records.get(app.strip().casefold(), {}).get("_preferences", {}).copy()

    def enable_cache(self):
        state = self.load()
        if not state["steps"]["2"]["confirmed"]:
            raise WorkflowError("请先确认本次 App，再明确要求复用该 App 的历史资料")
        state["reuse_cache"] = True
        self.save(state)
        return {"suggestions": self.suggestions(),
                "credentials": {platform: self.cached_credentials(platform)
                                for platform in state["steps"]["3"]["data"].get("platforms", [])}}

    def clear_cache(self, all_apps=False):
        state = self.load()
        path = self.credential_dir / "registry.json"
        if path.is_file():
            records = read_json(path)
            if all_apps:
                records = {}
            else:
                app = state["steps"]["2"]["data"].get("app")
                if app:
                    records.pop(app.strip().casefold(), None)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        if state.get("profile_first"):
            result = self.init_profile(replace=True)
            self.path.unlink()
            return {"status": "awaiting_profile", **result}
        return self.start(state["source"], state["steps"]["1"]["data"]["items"], replace=True)

    def credential_files(self, platform):
        """Return metadata only, never private key contents."""
        if platform not in PLATFORMS:
            raise WorkflowError("未知平台")
        field = "p8_path" if platform == "appstore" else "service_account_path"
        cached = self.cached_credentials(platform)
        paths = list(self.root.iterdir())
        if cached.get(field):
            paths.append(Path(cached[field]))
        found = []
        seen = set()
        for path in paths:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                continue
            path = path.resolve()
            if path.parent != self.root:
                root_copy = self.root / path.name
                if root_copy.is_file() and digest(root_copy) == digest(path):
                    continue
            if str(path) in seen:
                continue
            seen.add(str(path))
            valid = path.suffix.lower() == ".p8" if platform == "appstore" else path.suffix.lower() == ".json"
            if not valid:
                continue
            if platform == "googleplay":
                try:
                    value = read_json(path)
                    if not isinstance(value, dict) or value.get("type") != "service_account" or not value.get("private_key") or not value.get("client_email"):
                        continue
                except (ValueError, OSError):
                    continue
            found.append({"path": str(path), "name": path.name,
                          "location": "根目录，确认后迁至本机凭据目录" if path.parent == self.root else "本机凭据目录"})
        return found

    def platform_profile(self):
        """Keep editable account identifiers separate from detected key files."""
        path = self.root / PROFILE_FILE
        if path.is_file() and "App Store Connect API Issuer ID" in read_json(path):
            profile, platforms, missing, files = self.inspect_intake_profile()
            if missing:
                return {"file": str(path), "ready": False,
                        "platforms": platforms, "missing": list(dict.fromkeys(missing))}
            if self.path.is_file():
                state = self.load()
                if state.get("profile_first") and state.get("profile_sha256") != digest(path):
                    if profile["App"] != state["steps"]["2"]["data"].get("app"):
                        raise WorkflowError("App 已改变；请按新任务重新确认商品和 Product ID")
                    if platforms != state["steps"]["3"]["data"].get("platforms"):
                        self.set_step(3, {"platforms": platforms})
                    if "appstore" in platforms:
                        self.credentials("appstore", {
                            "issuer_id": profile["App Store Connect API Issuer ID"],
                            "key_id": profile["App Store Connect API Key"],
                            "app_id": profile["App Store ID"],
                            "bundle_id": profile["App Store Bundle ID"],
                            "p8_path": str(files["appstore"]),
                        }, replace=True)
                        image = self.copy_to_root(files["image"])
                        state = self.load()
                        images = [{"file": image.name, "sha256": digest(image)}]
                        if state["steps"]["5"]["data"].get("images") != images:
                            self.invalidate(state, 5)
                            state["steps"]["5"]["data"] = {
                                "screenshots": [image.name], "images": images, "content_checked": False,
                            }
                            self.save(state)
                    else:
                        state = self.load()
                        if state["steps"]["5"]["data"]:
                            self.invalidate(state, 5)
                            state["steps"]["5"]["data"] = {}
                            self.save(state)
                    if "googleplay" in platforms:
                        self.credentials("googleplay", {
                            "package": profile["Google Play Android 包名"],
                            "service_account_path": str(files["googleplay"]),
                        }, replace=True)
                    state = self.load()
                    state["profile_sha256"] = digest(path)
                    self.save(state)
            return {"file": str(path), "ready": True, "platforms": platforms,
                    "apple_p8_found": "appstore" in files,
                    "google_service_account_json_found": "googleplay" in files}
        state = self.load()
        app = state["steps"]["2"]["data"].get("app", "")
        if not app:
            raise WorkflowError("先记录本次 App，再生成平台资料文件")
        path = self.root / PROFILE_FILE
        existing = read_json(path) if path.is_file() else {}
        if not isinstance(existing, dict):
            raise WorkflowError("平台资料.json 须为 JSON 对象")
        same_task = existing.get("_任务ID") == state["task_id"] and existing.get("App") == app
        apple = state["steps"]["7"]["data"].get("credentials", {})
        google = state["steps"]["8"]["data"].get("credentials", {})
        source = existing if same_task else {
            "App Store Connect App ID": apple.get("app_id", ""),
            "Bundle ID": apple.get("bundle_id", ""),
            "App Store Connect Issuer ID": apple.get("issuer_id", ""),
            "App Store Connect Key ID": apple.get("key_id", ""),
            "App Store p8文件名": Path(apple["p8_path"]).name if apple.get("p8_path") else "",
            "Google Play 包名": google.get("package", ""),
            "Google Play 服务账号JSON文件名": Path(google["service_account_path"]).name
            if google.get("service_account_path") else "",
        }
        apple_files = {file["name"] for file in self.credential_files("appstore")
                       if Path(file["path"]).parent == self.root}
        google_files = {file["name"] for file in self.credential_files("googleplay")
                        if Path(file["path"]).parent == self.root}
        def selected(name, available):
            value = source.get(name, "")
            if not isinstance(value, str):
                raise WorkflowError(f"{name} 须为文件名")
            return value if value in available else next(iter(available)) if len(available) == 1 else ""
        apple_name = selected("App Store p8文件名", apple_files)
        google_name = selected("Google Play 服务账号JSON文件名", google_files)
        profile = {"_任务ID": state["task_id"], "App": app}
        for name in ("App Store Connect App ID", "Bundle ID", "App Store Connect Issuer ID",
                     "App Store Connect Key ID", "Google Play 包名"):
            value = source.get(name, "")
            if not isinstance(value, str):
                raise WorkflowError(f"{name} 须为文字")
            profile[name] = value.strip()
        profile.update({
            "App Store p8文件是否上传": bool(apple_files), "App Store p8文件名": apple_name,
            "Google Play 服务账号JSON文件是否上传": bool(google_files),
            "Google Play 服务账号JSON文件名": google_name,
        })
        if same_task:
            self.credentials("appstore", {key: value for key, value in {
                "app_id": profile["App Store Connect App ID"], "bundle_id": profile["Bundle ID"],
                "issuer_id": profile["App Store Connect Issuer ID"],
                "key_id": profile["App Store Connect Key ID"],
                "p8_path": str(self.root / apple_name) if apple_name else "",
            }.items() if value}, replace=True)
            self.credentials("googleplay", {key: value for key, value in {
                "package": profile["Google Play 包名"],
                "service_account_path": str(self.root / google_name) if google_name else "",
            }.items() if value}, replace=True)
        if existing != profile:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        return {"file": str(path), "apple_p8_found": bool(apple_files),
                "google_service_account_json_found": bool(google_files)}

    def accept_credential_file(self, platform, selected):
        selected = str(Path(selected).expanduser().resolve())
        candidates = {entry["path"] for entry in self.credential_files(platform)}
        if selected not in candidates:
            raise WorkflowError("请先将密钥文件放在根目录，刷新检测后选择；不接受任意文件路径")
        source = self.copy_to_root(selected)
        source.chmod(0o600)
        return str(source)

    def preflight(self, platform, phase="initial", online=True):
        if platform not in PLATFORMS or phase not in {"access", "initial", "final"}:
            raise WorkflowError("无效的预检请求")
        step = CREDENTIAL_STEPS[platform]
        state = self.load() if phase == "access" else self.prerequisites(step if phase == "initial" else LAST_STEP + 1)
        self.require_current_profile(state)
        if phase == "final":
            workbook = self.verify_workbook(state)
            auth = state.get("authorization")
            if not auth or auth["revision"] != state["revision"] or auth["sha256"] != workbook["sha256"]:
                raise WorkflowError("尚未获得本次工作簿确认")
        selected = state["steps"]["3"]["data"].get("platforms", []) if phase == "access" else self.active_platforms(state)
        if platform not in selected:
            result = {"ready": True, "skipped": True}
        else:
            data = state["steps"][str(step)]["data"]
            config = data.get("credentials", {})
            missing = []
            fields = ("issuer_id", "key_id", "p8_path", "app_id") if platform == "appstore" else ("package", "service_account_path")
            for field in fields:
                if not config.get(field):
                    missing.append(field)
            for field in ("p8_path",) if platform == "appstore" else ("service_account_path",):
                if config.get(field) and not Path(config[field]).expanduser().is_file():
                    missing.append(field + " 文件不存在")
            if platform == "appstore" and phase != "access":
                screenshot = state["steps"]["5"]["data"]
                if not screenshot.get("content_checked"):
                    missing.append("根目录购买页审核截图及内容核对")
                for image in screenshot.get("images", []):
                    path = self.root / image["file"]
                    if not path.is_file() or digest(path) != image["sha256"]:
                        missing.append("截图已改变，需重新检查")
            result = {"ready": False, "missing": missing, "read_access": "未验证", "write_access": "实际写入时验证"}
            if not missing and online:
                try:
                    if platform == "appstore":
                        result.update(apple_read_check(config))
                    else:
                        result.update(google_read_check(self.root, config))
                    result["ready"] = True
                    result["read_access"] = "已验证"
                except WorkflowError as error:
                    result["error"] = str(error)
            elif not missing:
                result["missing"] = ["需在线验证目标 App 访问权限"]
        result.update({"checked_at": now(), "revision": state["revision"], "phase": phase})
        if phase == "final":
            result["workbook_sha256"] = state["workbook"]["sha256"]
        state["preflight"][f"{platform}_{phase}"] = result
        if phase == "initial":
            state["steps"][str(step)]["confirmed"] = result["ready"]
            if result["ready"]:
                state["steps"][str(step)]["data"].pop("later", None)
        self.save(state)
        return result

    def check_intake(self):
        """Collect every outstanding input before creating the review workbook."""
        state = self.load()
        self.require_current_profile(state)
        if "appstore" in self.active_platforms(state) and all(
                state["steps"][str(step)]["confirmed"] for step in range(1, 5)):
            chosen_images = [image["file"] for image in state["steps"]["5"]["data"].get("images", [])]
            self.screenshots(files=chosen_images if state.get("profile_first") else None, count_failure=False)
            state = self.load()
        missing = []
        for step in range(1, 5):
            if not state["steps"][str(step)]["confirmed"]:
                missing.append(STEP_NAMES[step])
        selected = state["steps"]["3"]["data"].get("platforms", [])
        active = self.active_platforms(state)
        if "appstore" in active:
            screenshot = state["steps"]["5"]["data"]
            if not screenshot.get("images"):
                missing.append("App Store 购买页审核截图（PNG/JPEG，根目录）")
            elif not screenshot.get("content_checked") or any(
                    not (self.root / image["file"]).is_file()
                    or digest(self.root / image["file"]) != image["sha256"]
                    for image in screenshot["images"]):
                missing.append("App Store 截图内容和文件核对")
        for platform, label, fields in (
                ("appstore", "App Store", ("issuer_id", "key_id", "app_id", "p8_path")),
                ("googleplay", "Google Play", ("package", "service_account_path"))):
            if platform not in active:
                continue
            config = state["steps"][str(CREDENTIAL_STEPS[platform])]["data"].get("credentials", {})
            cached = self.cached_credentials(platform)
            if not config and cached:
                file_key = "p8_path" if platform == "appstore" else "service_account_path"
                cached_file = cached.get(file_key)
                if cached_file and Path(cached_file).is_file():
                    self.copy_to_root(cached_file)
                detail = (f"包名 {cached.get('package', '待核对')}、文件 {Path(cached_file).name}"
                          if platform == "googleplay" and cached_file else "此前保存的账号与密钥文件")
                missing.append(f"{label}：确认是否使用{detail}")
                continue
            names = {"issuer_id": "Issuer ID", "key_id": "Key ID", "app_id": "数字 App ID",
                     "p8_path": "App Store Connect API .p8", "package": "Android 包名",
                     "service_account_path": "服务账号 JSON"}
            for field in fields:
                if not config.get(field):
                    missing.append(f"{label}：{names[field]}")
                elif field.endswith("_path") and not Path(config[field]).is_file():
                    missing.append(f"{label}：{names[field]} 文件不存在")
            if all(config.get(field) for field in fields) and all(
                    Path(config[field]).is_file() for field in fields if field.endswith("_path")):
                check = self.preflight(platform, "access")
                if not check["ready"]:
                    missing.append(f"{label}：{check.get('error') or '在线读取目标 App 未通过'}")
        for number, item in enumerate(state["steps"]["1"]["data"].get("items", []), 1):
            reference = item.get("reference_name", "")
            if not reference or not re.search(r"[A-Za-z]", reference) or re.search(r"[\u4e00-\u9fff]", reference):
                missing.append(f"第 {number} 条商品：目标 App 的英文参考名称")
        if missing:
            return {"ready": False, "missing": [{"number": n, "item": value}
                                               for n, value in enumerate(missing, 1)]}
        for platform in selected:
            if platform in active:
                check = self.preflight(platform, "initial")
                if not check["ready"]:
                    return {"ready": False, "missing": [{"number": 1, "item": check.get("error") or
                            f"{platform} 在线检查未通过"}]}
        state = self.load()
        if state.get("workbook"):
            try:
                workbook = self.verify_workbook(state)
                return {"ready": True, "file": str(self.root / workbook["file"]), "shown": workbook["shown"]}
            except WorkflowError:
                pass
        try:
            workbook = self.generate()
            return {"ready": True, **workbook}
        except WorkflowError as error:
            return {"ready": False, "missing": [{"number": 1, "item": str(error)}]}

    def generate(self):
        state = self.prerequisites(LAST_STEP)
        if state["steps"]["1"]["data"].get("currency") != "USD" or state["steps"]["1"]["data"].get("base_region") != "USA":
            raise WorkflowError("不得把其他币种或地区悄悄转换为 USA/USD")
        app = state["steps"]["2"]["data"]["app"]
        platforms = state["steps"]["3"]["data"]["platforms"]
        prefix = state["steps"]["4"]["data"]["prefix"]
        from openpyxl import load_workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        w = load_workbook(self.root / "demo.xlsx")
        keep = {"AppleParty", "PricePoints", "IntroductoryOffers", "Territories", "商品规划"}
        for sheet in list(w):
            if sheet.title not in keep:
                del w[sheet.title]
        templates = {kind: [c.value for c in w["AppleParty"][row]] for kind, row in {"bag": 2, "membership": 3, "coins": 4}.items()}
        for name in ("AppleParty", "PricePoints", "IntroductoryOffers", "Territories"):
            sheet = w[name]
            sheet.delete_rows(2, sheet.max_row - 1)
        plan = w["商品规划"]
        for merged in list(plan.merged_cells.ranges):
            plan.unmerge_cells(str(merged))
        plan.delete_rows(1, plan.max_row)
        plan.append([f"本次 App：{app}；兼容平台：{', '.join(platforms)}；实际提交平台以本次授权为准；未提交"])
        plan.append(["类别", "Product ID", "英文参考名称", "一次性/首周 USD", "续订 USD", "周期", "截图", "原始 ID"])
        ids = set()
        for item in state["steps"]["1"]["data"]["items"]:
            kind = item["kind"]
            suffix = str(item.get("revision", 1))
            first = str(item["price"] if kind == "coins" else item["intro_price"])
            renewal = None if kind == "coins" else str(item["renewal_price"])
            bare = bare_product_id(item)
            pid = item.get("product_id") or prefix + bare
            if prefix and not pid.startswith(prefix):
                raise WorkflowError("源 Product ID 与已确认前缀不符，回到第 4 步")
            if not re.fullmatch(r"[a-z][a-z0-9_.]{1,99}", pid) or (kind != "coins" and "googleplay" in platforms and len(pid) > 40):
                raise WorkflowError("Product ID 不符合目标平台限制")
            if pid in ids:
                raise WorkflowError("同一工作簿有重复 Product ID")
            ids.add(pid)
            reference = item.get("reference_name")
            if not reference or not re.search(r"[A-Za-z]", reference) or re.search(r"[\u4e00-\u9fff]", reference):
                raise WorkflowError("先核对目标 App 的英文参考名称，并写入 reference_name")
            row = list(templates[kind])
            row[0] = None if kind == "coins" else f"{'Bag' if kind == 'bag' else 'Membership'} Week {first}_{renewal}_{suffix}"
            row[1], row[2] = pid, reference
            screenshot_data = state["steps"]["5"]["data"]
            verified_images = screenshot_data.get("screenshots", []) if screenshot_data.get("content_checked") else []
            requested_image = item.get("screenshot")
            image = (requested_image if requested_image in verified_images else None) if requested_image else (
                verified_images[0] if verified_images else None)
            if "appstore" in self.active_platforms(state) and not image:
                raise WorkflowError("商品的截图尚未完成根目录及内容核对")
            row[5] = image
            row[6] = row[6].replace("$59.99", "$" + first).replace("$119.99", "$" + (renewal or first))
            w["AppleParty"].append(row)
            w["PricePoints"].append([pid, "USA", renewal or first])
            w["Territories"].append([pid, 1, 1])
            if renewal:
                w["IntroductoryOffers"].append([pid, "USA", first, "PAY_AS_YOU_GO", "ONE_WEEK", 1])
            plan.append([kind, pid, reference, first, renewal, "ONE_WEEK" if renewal else "无", image, item.get("source_product_id", "用户未提供")])
        for cell in plan[2]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="315A59")
        for col in "ABCDEFGH":
            plan.column_dimensions[col].width = 28 if col != "B" else 44
        for row in plan.iter_rows():
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="center")
        plan.freeze_panes = "A3"
        w.active = w.sheetnames.index("商品规划")
        label = "AppStore-GooglePlay" if len(platforms) == 2 else ("AppStore" if platforms[0] == "appstore" else "GooglePlay")
        safe_app = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]", "_", app)
        path = self.root / f"待确认-{label}-{safe_app}-{state['task_id'][:8]}.xlsx"
        # Convert with the production Google importer before exposing this Excel.
        # Only the final source workbook survives; validation data lives outside the project.
        with tempfile.TemporaryDirectory(prefix="store-workflow-") as directory:
            candidate = Path(directory) / "source.xlsx"
            w.save(candidate)
            conversion = None
            google_config = state["steps"]["8"]["data"].get("credentials", {})
            if "googleplay" in platforms and google_config.get("package"):
                output = Path(directory) / "google.xlsx"
                config = google_config
                result = subprocess.run([sys.executable, "-B", str(self.root / "GooglePlay/web/catalog/catalog.py"),
                                         "--package", config["package"], "import-appleparty", str(candidate),
                                         "--output", str(output)], capture_output=True, text=True, timeout=60)
                if result.returncode:
                    raise WorkflowError("Google Play 格式转换失败，请检查商品字段和转换器")
                conversion = json.loads(result.stdout)
                converted = load_workbook(output, read_only=True, data_only=True)
                exceptions = list(converted["异常"].values)[1:]
                converted.close()
                if conversion["products"] != len(ids) or any(any(v is not None for v in row) for row in exceptions):
                    raise WorkflowError("Google Play 转换存在遗漏或异常，不能展示为可提交 Excel")
            temporary = path.with_suffix(".tmp")
            shutil.copyfile(candidate, temporary)
            temporary.replace(path)
        state["workbook"] = {"file": path.name, "sha256": digest(path), "revision": state["revision"], "shown": False}
        state["steps"]["9"]["data"] = {"file": path.name, "products": len(ids)}
        if conversion:
            state["steps"]["9"]["data"]["google_conversion"] = {k: conversion[k] for k in ("products", "offers")}
        elif "googleplay" in platforms:
            state["steps"]["9"]["data"]["google_conversion"] = {"pending": "补充 Google 包名后进行转换校验"}
        state["steps"]["9"]["confirmed"] = False
        state["authorization"] = None
        self.save(state)
        return {"file": str(path), "products": len(ids)}

    def verify_workbook(self, state):
        self.require_current_profile(state)
        workbook = state.get("workbook")
        if not workbook or workbook["revision"] != state["revision"]:
            raise WorkflowError("本次 Excel 尚未生成或前面的步骤已改变")
        path = self.root / workbook["file"]
        if not path.is_file() or digest(path) != workbook["sha256"]:
            raise WorkflowError("Excel 已被修改，请重新核对并展示，不沿用旧确认")
        return workbook

    def mark_shown(self):
        state = self.prerequisites(LAST_STEP)
        self.verify_workbook(state)["shown"] = True
        return self.save(state)

    def approve(self, message):
        state = self.prerequisites(LAST_STEP)
        workbook = self.verify_workbook(state)
        if not workbook["shown"]:
            raise WorkflowError("尚未向用户展示本次 Excel，继续仅恢复流程")
        # Deliberately accept only unambiguous approval phrases; the agent may
        # normalize a longer, equally explicit reply after interpreting it.
        phrase = message.strip().strip("。.!！ ")
        if not re.fullmatch(r"(?:我(?:已经|已)?|已经|已)?(?:确认(?:无误)?|核对无误|无误|没问题|继续(?:执行|提交|上传)?|可以(?:提交|上传)|确认无误进行第四步)", phrase):
            raise WorkflowError("请明确确认本次 Excel 或继续执行")
        state["steps"]["9"]["confirmed"] = True
        state["steps"]["9"]["data"].pop("later", None)
        state["authorization"] = {"sha256": workbook["sha256"], "revision": state["revision"], "at": now(),
                                   "platforms": self.active_platforms(state)}
        state["preflight"] = {k: v for k, v in state["preflight"].items() if not k.endswith("_final")}
        state["status"] = "authorized_pending_fresh_checks"
        return self.save(state)

    def can_submit(self):
        state = self.load()
        self.require_current_profile(state)
        workbook = self.verify_workbook(state)
        auth = state.get("authorization")
        active = self.active_platforms(state)
        if not active or state["steps"]["6"]["data"].get("needs_agent"):
            return False
        if not auth or state["status"] == "paused" or not all(state["steps"][str(n)]["confirmed"] for n in range(1, LAST_STEP + 1)):
            return False
        if auth["sha256"] != workbook["sha256"] or auth["revision"] != state["revision"]:
            return False
        if auth.get("platforms") != active:
            return False
        for platform in active:
            check = state["preflight"].get(platform + "_final", {})
            if not check.get("ready") or check.get("revision") != state["revision"]:
                return False
            if check.get("checked_at", "") < state["authorization"]["at"]:
                return False
            if check.get("workbook_sha256") != workbook["sha256"]:
                return False
            if (datetime.now(timezone.utc) - datetime.fromisoformat(check["checked_at"])).total_seconds() > 600:
                return False
        return True


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def der_ecdsa_raw(der: bytes) -> bytes:
    def part(position, tag):
        if position >= len(der) or der[position] != tag:
            raise WorkflowError("API 私钥签名格式无效")
        length = der[position + 1]
        start = position + 2
        if length & 128:
            count = length & 127
            length = int.from_bytes(der[start:start + count], "big")
            start += count
        return der[start:start + length], start + length, start
    _, _, start = part(0, 0x30)
    r, end, _ = part(start, 0x02)
    s, _, _ = part(end, 0x02)
    values = [v.lstrip(b"\0") for v in (r, s)]
    if any(len(v) > 32 for v in values):
        raise WorkflowError("App Store API 私钥必须是 ES256 密钥")
    return b"".join(v.rjust(32, b"\0") for v in values)


def apple_read_check(config):
    issued = int(time.time())
    header = b64(json.dumps({"alg": "ES256", "kid": config["key_id"], "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iss": config["issuer_id"], "iat": issued, "exp": issued + 300, "aud": "appstoreconnect-v1"}).encode())
    unsigned = (header + "." + claims).encode()
    result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(Path(config["p8_path"]).expanduser())],
                            input=unsigned, capture_output=True, check=False)
    if result.returncode:
        raise WorkflowError("无法使用所提供的 .p8 签名；请检查文件和 Key ID")
    token = unsigned.decode() + "." + b64(der_ecdsa_raw(result.stdout))
    app_id = str(config["app_id"])
    if not app_id.isdigit():
        raise WorkflowError("App Store Connect App ID 应为数字，不能用包名替代")
    def get(path):
        request = Request("https://api.appstoreconnect.apple.com" + path,
                          headers={"Authorization": "Bearer " + token})
        try:
            with urlopen(request, timeout=25) as response:
                return json.load(response)
        except HTTPError as error:
            raise WorkflowError(f"App Store 在线预检 HTTP {error.code}；请核对 API key、App 权限和账号协议") from None
        except (URLError, TimeoutError, OSError):
            raise WorkflowError("App Store 在线连接失败；本次未验证通过") from None
    app = get(f"/v1/apps/{app_id}")["data"]
    if config.get("bundle_id") and app.get("attributes", {}).get("bundleId") != config["bundle_id"]:
        raise WorkflowError("API 返回的 App Bundle ID 与本次目标不符")
    get(f"/v1/apps/{app_id}/inAppPurchasesV2?limit=1")
    get(f"/v1/apps/{app_id}/subscriptionGroups?limit=1")
    return {"app_id": app_id, "app_name": app.get("attributes", {}).get("name"),
            "note": "已核对目标 App 和内购读取权限；协议、价格点及实际写入权限仍以远端处理结果为准"}


def google_read_check(root, config):
    package = config["package"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
        raise WorkflowError("Android 包名格式无效")
    credential = Path(config["service_account_path"]).expanduser()
    try:
        key = read_json(credential)
    except (ValueError, OSError):
        raise WorkflowError("服务账号 JSON 无法读取") from None
    if key.get("type") != "service_account" or not key.get("private_key") or not key.get("client_email"):
        raise WorkflowError("需要 Google 服务账号 JSON，不是 .p8 或其他配置文件")
    environment = {k: v for k, v in os.environ.items() if k not in {"GPLAY_PROFILE", "GPLAY_PACKAGE", "GPLAY_OAUTH_TOKEN_PATH"}}
    environment.update(GPLAY_SERVICE_ACCOUNT_JSON=str(credential), GPLAY_NO_UPDATE="1", GPLAY_MAX_RETRIES="0")
    for command in ("onetimeproducts", "subscriptions"):
        try:
            result = subprocess.run([str(root / "GooglePlay" / "gplay"), command, "list", "--package", package,
                                     "--page-size", "1", "--output", "json"], cwd=root, env=environment,
                                    capture_output=True, text=True, check=False, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            raise WorkflowError("Google 在线预检连接失败；本次未验证通过") from None
        if result.returncode:
            code = re.search(r"Error (\d{3})", result.stderr)
            raise WorkflowError("Google Play 在线预检失败" + (f" HTTP {code[1]}" if code else "") + "；请核对服务账号、包名、API 开启状态和 App 权限")
    return {"package": package, "note": "已核对一次性商品和订阅读取权限；实际写入权限以远端处理结果为准"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("status", "inventory", "suggestions", "preview", "pause", "resume", "generate", "mark-shown", "check-content", "check-intake", "can-submit", "enable-cache", "profile", "prepare-excel"):
        sub.add_parser(name)
    p = sub.add_parser("profile-init"); p.add_argument("--replace", action="store_true")
    p = sub.add_parser("later"); p.add_argument("--step", type=int, choices=(1, 2, 3, 4, 5, 7, 8, 9), required=True)
    p = sub.add_parser("clear-cache"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("start"); p.add_argument("--input", type=Path, required=True); p.add_argument("--replace", action="store_true")
    p = sub.add_parser("confirm"); p.add_argument("--step", type=int, required=True); p.add_argument("--input", type=Path)
    p = sub.add_parser("prefix"); p.add_argument("--enabled", choices=["yes", "no"], required=True); p.add_argument("--value", default="")
    p = sub.add_parser("screenshots"); p.add_argument("files", nargs="*"); p.add_argument("--scan-only", action="store_true")
    p = sub.add_parser("credentials"); p.add_argument("platform", choices=sorted(PLATFORMS)); p.add_argument("--input", type=Path, required=True)
    p = sub.add_parser("preflight"); p.add_argument("platform", choices=sorted(PLATFORMS)); p.add_argument("--phase", choices=["access", "initial", "final"], default="initial")
    p = sub.add_parser("approve"); p.add_argument("--message", required=True)
    p = sub.add_parser("arrange"); p.add_argument("--message", required=True)
    p = sub.add_parser("choose-submit"); p.add_argument("platforms", nargs="+", choices=sorted(PLATFORMS))
    sub.add_parser("serve").add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    task = Task(args.root)
    try:
        if args.action == "serve":
            from agent_form import serve
            serve(args.root, args.port)
            return 0
        if args.action == "start":
            data = read_json(args.input); result = task.start_from_profile(data["source"], data["items"], args.replace)
        elif args.action == "profile-init": result = task.init_profile(args.replace)
        elif args.action == "confirm":
            data = read_json(args.input) if args.input else task.load()["steps"][str(args.step)]["data"]
            result = task.set_step(args.step, data)
        elif args.action == "prefix": result = task.set_step(4, {"enabled": args.enabled == "yes", "prefix": args.value})
        elif args.action == "screenshots": result = task.screenshots(args.files, not args.scan_only)
        elif args.action == "check-content": result = task.mark_screenshot_content()
        elif args.action == "credentials": result = task.credentials(args.platform, read_json(args.input))
        elif args.action == "preflight": result = task.preflight(args.platform, args.phase)
        elif args.action == "approve": result = task.approve(args.message)
        elif args.action == "arrange": result = task.arrange(args.message)
        elif args.action == "choose-submit": result = task.choose_submission(args.platforms)
        elif args.action == "profile": result = task.platform_profile()
        elif args.action == "prepare-excel": result = task.prepare_excel()
        elif args.action == "later": result = task.later(args.step)
        elif args.action == "check-intake": result = task.check_intake()
        elif args.action == "inventory": result = task.inventory()
        elif args.action == "enable-cache": result = task.enable_cache()
        elif args.action == "clear-cache": result = task.clear_cache(args.all)
        elif args.action == "can-submit": result = {"ready": task.can_submit()}
        elif args.action == "status":
            if task.path.is_file():
                result = task.load(); result["current_step"] = task.current(result); result["suggestions"] = task.suggestions()
            else:
                profile_path = task.root / PROFILE_FILE
                if profile_path.is_file() and "App Store Connect API Issuer ID" in read_json(profile_path):
                    _, platforms, missing, _ = task.inspect_intake_profile()
                    result = {"status": "awaiting_profile" if missing else "profile_ready",
                              "profile": str(profile_path), "platforms": platforms, "missing": missing}
                else:
                    result = {"status": "no_task", "next": "profile-init"}
        else: result = getattr(task, args.action.replace("-", "_"))()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, KeyError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
