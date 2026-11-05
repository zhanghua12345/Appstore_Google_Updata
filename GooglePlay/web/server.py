#!/usr/bin/env python3
"""Local web UI for the Excel-driven Google Play catalog workflow."""

from __future__ import annotations

import argparse
import cgi
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 1024 * 1024
BATCH_SIZE = 100
MAX_TRANSIENT_APPLY_RECOVERIES = 5
TRANSIENT_APPLY_RETRY_DELAYS = (2, 4, 8, 15, 30)
TRANSIENT_GOOGLE_ERROR_MARKERS = (
    "error 429",
    "error 500",
    "error 502",
    "error 503",
    "error 504",
    "backenderror",
    "service is currently unavailable",
    "oauth2: cannot fetch token",
    "unexpected eof",
    "connection reset by peer",
    "context deadline exceeded",
    "i/o timeout",
)
JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


class RequestError(RuntimeError):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = int(status)


@dataclass(frozen=True)
class Config:
    root: Path
    catalog_script: Path
    catalog_python: Path
    gplay_dir: Path
    jobs_dir: Path
    host: str
    port: int
    demo_workbook: Optional[Path] = None
    apps_file: Optional[Path] = None
    credentials_dir: Optional[Path] = None

    @classmethod
    def from_environment(cls) -> "Config":
        root = Path(__file__).resolve().parents[1]
        default_catalog = root / "web" / "catalog" / "catalog.py"
        catalog_script = Path(
            os.environ.get("GOOGLE_PLAY_CATALOG_SCRIPT", str(default_catalog))
        ).expanduser().resolve()
        default_python = catalog_script.parent / ".venv" / "bin" / "python"
        catalog_python_value = Path(
            os.environ.get(
                "GOOGLE_PLAY_CATALOG_PYTHON",
                str(default_python if default_python.is_file() else Path(sys.executable)),
            )
        ).expanduser()
        catalog_python = Path(os.path.abspath(str(catalog_python_value)))
        return cls(
            root=root,
            catalog_script=catalog_script,
            catalog_python=catalog_python,
            gplay_dir=root,
            jobs_dir=Path(
                os.environ.get(
                    "GOOGLE_PLAY_WEB_JOBS",
                    str(Path.home() / ".appstore_google_updata" / "GooglePlay" / "jobs"),
                )
            ).expanduser().resolve(),
            host=os.environ.get("GOOGLE_PLAY_WEB_HOST", "127.0.0.1"),
            port=int(os.environ.get("GOOGLE_PLAY_WEB_PORT", "8787")),
            demo_workbook=(
                root / "demo.xlsx"
                if (root / "demo.xlsx").is_file()
                else root.parent / "demo.xlsx"
            ),
            apps_file=(
                root / "key" / "apps.json"
                if (root / "key" / "apps.json").is_file()
                else root.parent / "apps.json"
            ),
            credentials_dir=Path(
                os.environ.get(
                    "GOOGLE_PLAY_WEB_CREDENTIALS",
                    str(Path.home() / ".gplay" / "credentials"),
                )
            ).expanduser().resolve(),
        )

    def validate(self) -> None:
        missing = [
            str(path)
            for path in (self.catalog_script, self.catalog_python, self.gplay_dir / "gplay")
            if not path.is_file()
        ]
        if missing:
            raise RuntimeError("缺少网页上传所需文件：" + "、".join(missing))
        if self.demo_workbook is None or not self.demo_workbook.is_file():
            raise RuntimeError("缺少项目根目录 demo.xlsx")
        if self.apps_file is None or not self.apps_file.is_file():
            raise RuntimeError("缺少项目最外层 apps.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def command_environment(
    run: Optional[Callable[..., Any]] = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    if any(environment.get(key) for key in PROXY_ENV_KEYS):
        return environment
    if sys.platform != "darwin":
        return environment

    runner = run or subprocess.run
    try:
        result = runner(
            ["/usr/sbin/scutil", "--proxy"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return environment
    if result.returncode != 0:
        return environment

    def value(name: str) -> str:
        match = re.search(
            rf"^\s*{re.escape(name)}\s*:\s*(.*?)\s*$",
            result.stdout,
            re.MULTILINE,
        )
        return match.group(1).strip() if match else ""

    for prefix in ("HTTP", "HTTPS"):
        if value(f"{prefix}Enable") != "1":
            continue
        host = value(f"{prefix}Proxy")
        port = value(f"{prefix}Port")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host) or not port.isdigit():
            continue
        port_number = int(port)
        if not 0 < port_number <= 65535:
            continue
        url_host = f"[{host}]" if ":" in host else host
        proxy_url = f"http://{url_host}:{port_number}"
        environment[f"{prefix}_PROXY"] = proxy_url
        environment[f"{prefix.lower()}_proxy"] = proxy_url

    if any(environment.get(key) for key in PROXY_ENV_KEYS):
        environment.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        environment.setdefault("no_proxy", environment["NO_PROXY"])
    return environment


def is_transient_google_error(error: object) -> bool:
    detail = str(error).lower()
    return any(marker in detail for marker in TRANSIENT_GOOGLE_ERROR_MARKERS)


def validate_package(value: str) -> str:
    value = value.strip()
    if not PACKAGE_RE.fullmatch(value):
        raise RequestError("请输入合法的 Android 包名")
    return value


def validate_profile(value: str) -> str:
    value = value.strip()
    if not PROFILE_RE.fullmatch(value):
        raise RequestError("profile 只能包含字母、数字、点、横线和下划线")
    return value


def validate_job_id(value: str) -> str:
    if not JOB_ID_RE.fullmatch(value):
        raise RequestError("任务 ID 无效", HTTPStatus.NOT_FOUND)
    return value


def validate_upload(filename: str, first_bytes: bytes, size: int) -> None:
    if not filename or Path(filename).suffix.lower() != ".xlsx":
        raise RequestError("只支持 .xlsx 文件")
    if size < 1:
        raise RequestError("Excel 文件不能为空")
    if size > MAX_UPLOAD_BYTES:
        raise RequestError("Excel 文件不能超过 20 MB")
    if not first_bytes.startswith(b"PK\x03\x04"):
        raise RequestError("文件不是有效的 Excel .xlsx 格式")


def validate_service_account(
    raw: bytes,
    expected_project_id: str = "",
) -> dict[str, Any]:
    if not raw or len(raw) > MAX_CREDENTIAL_BYTES:
        raise RequestError("服务账号 JSON 不能为空且不能超过 1 MB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestError("服务账号文件不是有效 JSON") from error
    required = {
        "type",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "token_uri",
    }
    if not isinstance(value, dict) or value.get("type") != "service_account":
        raise RequestError("请选择 Google service account JSON 文件")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
        raise RequestError("服务账号 JSON 缺少必要字段")
    if "BEGIN PRIVATE KEY" not in value["private_key"]:
        raise RequestError("服务账号 JSON 的 private_key 无效")
    if not value["client_email"].endswith(".gserviceaccount.com"):
        raise RequestError("服务账号 JSON 的 client_email 无效")
    if expected_project_id and value["project_id"] != expected_project_id:
        raise RequestError(
            f"服务账号 JSON 的 project_id 必须为 {expected_project_id}，"
            "与所选 App 的 name 一致"
        )
    return value


def safe_credential_metadata(profile: str, key_path: Path) -> dict[str, Any]:
    profile = validate_profile(profile)
    value = validate_service_account(key_path.read_bytes(), profile)
    return {
        "profile": profile,
        "credential_type": value["type"],
        "project_id": value["project_id"],
        "client_email": value["client_email"],
        "file_name": key_path.name,
        "file_status": "可用",
    }


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.findall(".//{*}t"))
        for item in root.findall(".//{*}si")
    ]


def _xlsx_sheet_paths(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    targets = {
        item.attrib.get("Id", ""): item.attrib.get("Target", "")
        for item in relationships.findall(".//{*}Relationship")
    }
    result: list[tuple[str, str]] = []
    for sheet in workbook.findall(".//{*}sheet"):
        relationship_id = next(
            (value for key, value in sheet.attrib.items() if key.endswith("}id")), ""
        )
        target = targets.get(relationship_id, "")
        if not target:
            continue
        path = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        result.append((sheet.attrib.get("name", ""), path))
    return result


def _xlsx_column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    value = 0
    for character in letters.upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return max(0, value - 1)


def _xlsx_cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//{*}t"))
    value_node = cell.find("{*}v")
    value = value_node.text if value_node is not None and value_node.text else ""
    if cell_type == "s" and value:
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return value
    if cell_type == "b":
        return "是" if value == "1" else "否"
    return value


def workbook_preview(
    path: Path,
    max_rows: int = 300,
    preferred_sheet: Optional[str] = None,
) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            shared = _xlsx_shared_strings(archive)
            sheets = _xlsx_sheet_paths(archive)
            names = [name for name, _ in sheets]
            preferred = (
                preferred_sheet
                if preferred_sheet in names
                else next(
                    (name for name in ("AppleParty", "products") if name in names),
                    names[0] if names else "",
                )
            )
            sheet_path = next(
                (sheet_path for name, sheet_path in sheets if name == preferred), ""
            )
            if not sheet_path:
                raise RequestError("Excel 没有可预览的工作表")
            sheet = ElementTree.fromstring(archive.read(sheet_path))
    except RequestError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise RequestError("无法读取 Excel 预览") from error

    matrix: list[list[str]] = []
    for row in sheet.findall(".//{*}sheetData/{*}row")[: max_rows + 1]:
        values: list[str] = []
        for cell in row.findall("{*}c"):
            index = _xlsx_column_index(cell.attrib.get("r", "A1"))
            while len(values) <= index:
                values.append("")
            values[index] = _xlsx_cell_value(cell, shared)
        matrix.append(values)
    if not matrix:
        return {"sheet": preferred, "columns": [], "rows": []}
    columns = [value.strip() for value in matrix[0]]
    rows = [
        {
            column: (values[index] if index < len(values) else "")
            for index, column in enumerate(columns)
            if column
        }
        for values in matrix[1:]
        if any(value.strip() for value in values)
    ]
    return {"sheet": preferred, "columns": [value for value in columns if value], "rows": rows}


def load_app_profiles(path: Path) -> dict[str, dict[str, str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RequestError("无法读取 apps.json") from error
    if not isinstance(value, dict) or not value:
        raise RequestError("apps.json 必须是非空的 App 到 profile 映射")
    result: dict[str, dict[str, str]] = {}
    for app, raw_config in value.items():
        if not isinstance(app, str) or not app.strip():
            raise RequestError("apps.json 中的 App 名称必须是文本")
        if isinstance(raw_config, str):
            profile = raw_config
            package = ""
        elif isinstance(raw_config, dict):
            profile = raw_config.get("name", raw_config.get("profile", ""))
            package = raw_config.get("pack", raw_config.get("package", ""))
        else:
            raise RequestError(
                "apps.json 中每个 App 必须配置 name 和 pack"
            )
        if not isinstance(profile, str) or not isinstance(package, str):
            raise RequestError("apps.json 中的 name 和 pack 必须是文本")
        normalized_package = package.strip()
        if normalized_package:
            normalized_package = validate_package(normalized_package)
        result[app.strip()] = {
            "name": validate_profile(profile),
            "pack": normalized_package,
        }
    return result


def resolve_app_target(
    path: Path,
    app_name: str,
    profile: str = "",
    package: str = "",
    require_package: bool = False,
) -> dict[str, str]:
    app_name = app_name.strip()
    target = load_app_profiles(path).get(app_name)
    if target is None:
        raise RequestError("请选择 apps.json 中已配置的 App")
    if profile and validate_profile(profile) != target["name"]:
        raise RequestError("页面提交的 profile 与所选 App 配置不一致")
    if package:
        submitted_package = validate_package(package)
        if target["pack"] and submitted_package != target["pack"]:
            raise RequestError("页面提交的包名与所选 App 配置不一致")
    if require_package and not target["pack"]:
        raise RequestError(
            f"请先在 apps.json 中填写 {app_name} 的 Android 包名 pack"
        )
    return {"app": app_name, **target}


def public_config(config: Config, csrf_token: str) -> dict[str, Any]:
    return {
        "batch_size": BATCH_SIZE,
        "csrf_token": csrf_token,
        "demo_url": "/demo.xlsx",
        "lightweight_job_status": True,
        "wizard_version": 2,
    }


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _directory(self, job_id: str) -> Path:
        return self.root / validate_job_id(job_id)

    def _metadata_path(self, job_id: str) -> Path:
        return self._directory(job_id) / "job.json"

    def _write(self, job: dict[str, Any]) -> None:
        path = self._metadata_path(job["id"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    def create(
        self,
        filename: str,
        stream: BinaryIO,
        package: str,
        profile: str,
        allow_one_time_price_changes: bool = False,
    ) -> dict[str, Any]:
        package = validate_package(package)
        profile = validate_profile(profile)
        raw = stream.read(MAX_UPLOAD_BYTES + 1)
        validate_upload(filename, raw[:4], len(raw))
        job_id = str(uuid.uuid4())
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        (directory / "source.xlsx").write_bytes(raw)
        job = {
            "id": job_id,
            "original_name": Path(filename).name,
            "package": package,
            "profile": profile,
            "allow_one_time_price_changes": bool(allow_one_time_price_changes),
            "status": "queued",
            "stage": "等待校验",
            "operation": None,
            "error": None,
            "validation": None,
            "preview": None,
            "plan": None,
            "plan_file": None,
            "plan_revision": 0,
            "state_revision": 0,
            "active_batch": None,
            "apply_runs": 0,
            "applied_total": 0,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        with self._lock:
            self._write(job)
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        path = self._metadata_path(job_id)
        with self._lock:
            if not path.is_file():
                raise RequestError("任务不存在", HTTPStatus.NOT_FOUND)
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RequestError("任务状态文件已损坏", HTTPStatus.INTERNAL_SERVER_ERROR) from error

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if "plan" in changes:
                changes["plan_revision"] = int(job.get("plan_revision") or 0) + 1
            job.update(changes)
            job["state_revision"] = int(job.get("state_revision") or 0) + 1
            job["updated_at"] = utc_now()
            self._write(job)
            return job

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in self.root.glob("*/job.json"):
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(jobs, key=lambda item: item.get("updated_at", ""), reverse=True)[:limit]

    def clear(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job.get("operation") or job.get("status") == "running":
                raise RequestError("任务仍在运行，完成后才能清空", HTTPStatus.CONFLICT)
            directory = self._directory(job_id)
            shutil.rmtree(directory)

    def recover_interrupted(self) -> list[str]:
        resumable: list[str] = []
        for job in self.list_recent(limit=1000):
            if job.get("operation"):
                resume_operation = (
                    "apply_all" if job["operation"] == "apply_all" else None
                )
                self.update(
                    job["id"],
                    status=(
                        "uncertain"
                        if job["operation"] in {"apply", "apply_all"}
                        else "error"
                    ),
                    stage="上次操作因服务重启而中断",
                    operation=None,
                    resume_operation=resume_operation,
                    active_batch=None,
                    error=(
                        "服务重启中断了同步，正在自动核对线上状态后续传。"
                        if resume_operation
                        else "请先重新生成只读计划，确认线上状态后再继续。"
                    ),
                )
                if resume_operation:
                    resumable.append(job["id"])
            elif (
                job.get("status") == "ready"
                and job.get("plan")
                and not job["plan"].get("blocked")
                and not job["plan"].get("actionable")
            ):
                self.update(
                    job["id"],
                    status="verified",
                    stage="核对通过，Excel 与 Google Play 已一致",
                    error=None,
                )
        return resumable


def build_catalog_command(
    config: Config,
    action: str,
    workbook: Path,
    output_dir: Optional[Path],
    package: str,
    profile: str,
    confirmation: Optional[str] = None,
    allow_one_time_price_changes: bool = False,
    input_plan: Optional[Path] = None,
) -> list[str]:
    if action not in {"validate", "plan", "apply", "verify"}:
        raise RequestError("不支持的上传操作")
    package = validate_package(package)
    profile = validate_profile(profile)
    command = [
        str(config.catalog_python),
        str(config.catalog_script),
        "--package",
        package,
        "--profile",
        profile,
        action,
        str(workbook),
    ]
    if action in {"plan", "apply", "verify"}:
        if output_dir is None:
            raise RequestError("缺少计划输出目录")
        command.extend(["--output-dir", str(output_dir)])
    if action in {"plan", "apply"} and allow_one_time_price_changes:
        command.append("--allow-one-time-price-changes")
    if action == "apply":
        if confirmation != package:
            raise RequestError("确认包名与目标包名不一致")
        command.extend(
            ["--confirm-package", confirmation, "--batch-size", str(BATCH_SIZE)]
        )
        if input_plan is not None:
            command.extend(["--input-plan", str(input_plan)])
    return command


def xlsx_sheet_names(path: Path) -> set[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise RequestError("文件不是有效的 Excel .xlsx 工作簿") from error
    return {
        element.attrib.get("name", "")
        for element in root.findall(".//{*}sheet")
        if element.attrib.get("name")
    }


def source_workbook_format(sheets: set[str]) -> Optional[str]:
    if {"products", "localizations", "metadata"}.issubset(sheets):
        return "google_catalog"
    if {"AppleParty", "PricePoints"}.issubset(sheets):
        return "appleparty"
    return None


def parse_last_json(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("上传器没有返回可识别的 JSON 结果")


def display_money(price: Any) -> tuple[str, str]:
    if not isinstance(price, dict):
        return "", ""
    try:
        units = int(str(price.get("units", "0")))
        nanos = int(price.get("nanos") or 0)
    except (TypeError, ValueError):
        return "", ""
    amount = units + nanos / 1_000_000_000
    return f"{amount:.2f}", str(price.get("currencyCode") or "")


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for section in ("items", "offers"):
        for item in plan.get(section, []):
            changes = [str(value) for value in item.get("changes", [])]
            requested_price, currency_code = display_money(item.get("base_price"))
            effective_price = requested_price
            for change in changes:
                normalized = re.fullmatch(
                    r"Google price point maps workbook price (.+) to (.+)",
                    change,
                )
                if normalized:
                    effective_price = normalized.group(2)
                    break
            rows.append(
                {
                    "kind": "offer" if section == "offers" else item.get("product_type", ""),
                    "product_id": item.get("product_id", ""),
                    "base_plan_id": item.get("base_plan_id", ""),
                    "offer_id": item.get("offer_id", ""),
                    "action": item.get("action", ""),
                    "activate_required": bool(item.get("activate_required", False)),
                    "changes": changes,
                    "requested_price": requested_price,
                    "effective_price": effective_price,
                    "currency_code": currency_code,
                }
            )
    actionable = [
        item
        for item in rows
        if item["action"] != "noop" or item["activate_required"]
    ]
    blocked = [item for item in rows if item["action"].startswith("blocked_")]
    return {
        "counts": plan.get("counts", {}),
        "total": len(rows),
        "actionable": len(actionable),
        "blocked": len(blocked),
        "items": rows,
    }


def selected_batch_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "kind": item.get("kind", ""),
            "product_id": item.get("product_id", ""),
            "base_plan_id": item.get("base_plan_id", ""),
            "offer_id": item.get("offer_id", ""),
        }
        for item in plan.get("items", [])
        if item.get("action") != "noop" or item.get("activate_required", False)
    ][:BATCH_SIZE]


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "original_name",
        "package",
        "profile",
        "allow_one_time_price_changes",
        "status",
        "stage",
        "operation",
        "error",
        "validation",
        "preview",
        "plan",
        "plan_revision",
        "state_revision",
        "active_batch",
        "apply_runs",
        "applied_total",
        "last_apply",
        "created_at",
        "updated_at",
    }
    result = {key: value for key, value in job.items() if key in allowed}
    result["plan_revision"] = int(job.get("plan_revision") or 0)
    result["state_revision"] = int(job.get("state_revision") or 0)
    return result


def public_job_status(job: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "original_name",
        "package",
        "profile",
        "status",
        "stage",
        "operation",
        "error",
        "active_batch",
        "apply_runs",
        "applied_total",
        "last_apply",
        "created_at",
        "updated_at",
    }
    result = {key: value for key, value in job.items() if key in allowed}
    result["plan_revision"] = int(job.get("plan_revision") or 0)
    result["state_revision"] = int(job.get("state_revision") or 0)
    plan = job.get("plan")
    result["plan"] = (
        {
            "total": int(plan.get("total") or 0),
            "actionable": int(plan.get("actionable") or 0),
            "blocked": int(plan.get("blocked") or 0),
        }
        if plan
        else None
    )
    return result


class CommandRunner:
    def __init__(self, config: Config):
        self.config = config

    def run(self, command: list[str], accepted: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        env = command_environment()
        env["PATH"] = str(self.config.gplay_dir) + os.pathsep + env.get("PATH", "")
        env["GPLAY_NO_UPDATE"] = "1"
        env["GPLAY_MAX_RETRIES"] = "0"
        result = subprocess.run(
            command,
            cwd=self.config.jobs_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1800,
        )
        if result.returncode not in accepted:
            detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
            detail = detail.replace(str(Path.home()), "<本机目录>")
            raise RuntimeError(detail[-4000:])
        return result


class AuthManager:
    def __init__(self, config: Config, runner: CommandRunner):
        self.config = config
        self.runner = runner
        self._lock = threading.Lock()

    def _status_payload(self, profile: str) -> dict[str, Any]:
        profile = validate_profile(profile)
        env = command_environment()
        env["GPLAY_PROFILE"] = profile
        env["GPLAY_NO_UPDATE"] = "1"
        result = subprocess.run(
            [str(self.config.gplay_dir / "gplay"), "auth", "status", "--output", "json"],
            cwd=self.config.jobs_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _key_path(self, profile: str) -> Optional[Path]:
        for item in self._status_payload(profile).get("profiles", []):
            if item.get("name") == profile:
                raw_path = item.get("key_path", "")
                return Path(raw_path) if raw_path else None
        return None

    def status(self, profile: str) -> dict[str, Any]:
        profile = validate_profile(profile)
        key_path = self._key_path(profile)
        return {
            "profile": profile,
            "configured": bool(key_path and key_path.is_file()),
        }

    def data(self, profile: str) -> dict[str, Any]:
        profile = validate_profile(profile)
        key_path = self._key_path(profile)
        if key_path is None or not key_path.is_file():
            raise RequestError("这个 profile 还没有可用的服务账号文件")
        return safe_credential_metadata(profile, key_path)

    def check(self, profile: str, package: str) -> dict[str, Any]:
        metadata = self.data(profile)
        package = validate_package(package)
        env = command_environment()
        env["GPLAY_PROFILE"] = profile
        env["GPLAY_NO_UPDATE"] = "1"
        env["GPLAY_MAX_RETRIES"] = "0"
        detail = ""
        was_transient = False
        for attempt in range(3):
            result = subprocess.run(
                [
                    str(self.config.gplay_dir / "gplay"),
                    "onetimeproducts",
                    "list",
                    "--package",
                    package,
                    "--page-size",
                    "1",
                ],
                cwd=self.config.jobs_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=90,
            )
            if result.returncode == 0:
                return {
                    **metadata,
                    "package": package,
                    "api_access": "通过",
                    "checked_at": utc_now(),
                }
            detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
            transient = is_transient_google_error(detail)
            was_transient = was_transient or transient
            if not transient or attempt == 2:
                break
            time.sleep(2**attempt)
        detail = detail.replace(str(Path.home()), "<本机目录>")
        return {
            **metadata,
            "package": package,
            "api_access": "暂时无法验证" if was_transient else "失败",
            "check_error": detail[-1000:],
            "checked_at": utc_now(),
        }

    def install(self, profile: str, raw: bytes) -> dict[str, Any]:
        profile = validate_profile(profile)
        validate_service_account(raw, profile)
        with self._lock:
            key_dir = self.config.credentials_dir or (
                Path.home() / ".gplay" / "credentials"
            )
            key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            key_path = key_dir / f"{profile}.json"
            previous = key_path.read_bytes() if key_path.is_file() else None
            temporary = key_dir / f".{profile}-{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(raw)
            temporary.chmod(0o600)
            os.replace(temporary, key_path)
            command = [
                str(self.config.gplay_dir / "gplay"),
                "auth",
                "login",
                "--service-account",
                str(key_path),
                "--profile",
                profile,
                "--set-default=false",
            ]
            try:
                self.runner.run(command)
            except Exception:
                if previous is None and key_path.is_file():
                    key_path.unlink()
                elif previous is not None:
                    key_path.write_bytes(previous)
                    key_path.chmod(0o600)
                raise
        return self.data(profile)


class JobService:
    def __init__(self, config: Config, store: JobStore, runner: CommandRunner):
        self.config = config
        self.store = store
        self.runner = runner
        self._state_lock = threading.Lock()
        self._job_locks: dict[str, threading.Lock] = {}
        self._apply_lock = threading.Lock()

    def _lock_for(self, job_id: str) -> threading.Lock:
        with self._state_lock:
            return self._job_locks.setdefault(job_id, threading.Lock())

    def start_prepare(self, job_id: str) -> None:
        self._start(job_id, "prepare", self._prepare, uncertain_on_error=False)

    def start_plan(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job.get("validation"):
            raise RequestError("请返回步骤 2 上传并完成 Excel 本地校验")
        self._start(
            job_id,
            "plan",
            self._plan,
            uncertain_on_error=False,
            start_changes={
                "plan": None,
                "plan_file": None,
                "active_batch": None,
            },
        )

    def start_apply(self, job_id: str, confirmation: str) -> None:
        self._start_apply_operation(job_id, confirmation, "apply", self._apply)

    def start_apply_all(self, job_id: str, confirmation: str) -> None:
        self._start_apply_operation(
            job_id,
            confirmation,
            "apply_all",
            self._apply_all,
        )

    def resume_interrupted_apply_all(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job.get("resume_operation") != "apply_all":
            return
        if not self._apply_lock.acquire(blocking=False):
            raise RequestError("另一个 Google 同步任务正在执行", HTTPStatus.CONFLICT)

        def reconcile_and_continue(value: dict[str, Any]) -> None:
            try:
                self.store.update(
                    value["id"],
                    resume_operation=None,
                    stage="服务已恢复，正在核对 Google Play 后自动续传",
                    error=None,
                )
                try:
                    self._plan(self.store.get(value["id"]))
                except Exception as error:
                    if not is_transient_google_error(error):
                        raise
                    self._recover_transient_apply(value["id"], error, 0)
                current = self.store.get(value["id"])
                self._apply_all(current, current["package"])
            finally:
                self._apply_lock.release()

        try:
            self._start(
                job_id,
                "apply_all",
                reconcile_and_continue,
                uncertain_on_error=True,
            )
        except Exception:
            self._apply_lock.release()
            raise

    def _start_apply_operation(
        self,
        job_id: str,
        confirmation: str,
        operation: str,
        callback: Callable[[dict[str, Any], str], None],
    ) -> None:
        job = self.store.get(job_id)
        if confirmation != job["package"]:
            raise RequestError("确认包名与目标包名不一致")
        if job.get("status") != "ready" or not job.get("plan"):
            raise RequestError("请返回步骤 3 重新检查线上状态，完成后再提交")
        if job["plan"].get("blocked"):
            raise RequestError("计划存在阻塞项，不能提交")
        if not job["plan"].get("actionable"):
            raise RequestError("当前没有待提交项目")
        if not self._apply_lock.acquire(blocking=False):
            raise RequestError("另一个 Google 同步任务正在执行，完成后本任务才能开始", HTTPStatus.CONFLICT)

        def apply_with_lock(value: dict[str, Any]) -> None:
            try:
                callback(value, confirmation)
            finally:
                self._apply_lock.release()

        try:
            self._start(job_id, operation, apply_with_lock, uncertain_on_error=True)
        except Exception:
            self._apply_lock.release()
            raise

    def start_verify(self, job_id: str) -> None:
        job = self.store.get(job_id)
        plan = job.get("plan")
        if job.get("status") != "ready" or not plan:
            raise RequestError("请先完成步骤 3 的只读检查和步骤 4 的全部同步")
        if plan.get("blocked"):
            raise RequestError("计划存在阻塞项，不能执行最终验证")
        if plan.get("actionable"):
            raise RequestError("仍有待提交项目，完成全部同步后才能最终核对")
        self._start(job_id, "verify", self._verify, uncertain_on_error=False)

    def _start(
        self,
        job_id: str,
        operation: str,
        callback: Callable[[dict[str, Any]], None],
        uncertain_on_error: bool,
        start_changes: Optional[dict[str, Any]] = None,
    ) -> None:
        lock = self._lock_for(job_id)
        if not lock.acquire(blocking=False):
            raise RequestError("这个任务已有操作正在执行", HTTPStatus.CONFLICT)
        try:
            job = self.store.get(job_id)
            changes = {
                "status": "running",
                "stage": {
                    "prepare": "正在校验 Excel",
                    "plan": "正在读取 Google 差异",
                    "apply": "正在向 Google Play 同步",
                    "apply_all": "正在自动同步全部内容",
                    "verify": "正在最终核对 Google Play 实际结果",
                }[operation],
                "operation": operation,
                "error": None,
            }
            changes.update(start_changes or {})
            self.store.update(job_id, **changes)
        except Exception:
            lock.release()
            raise

        def target() -> None:
            try:
                callback(self.store.get(job_id))
            except Exception as error:
                traceback.print_exc()
                self.store.update(
                    job_id,
                    status="uncertain" if uncertain_on_error else "error",
                    stage="写入结果需要重新检查" if uncertain_on_error else "操作失败",
                    active_batch=None,
                    error=str(error),
                )
            finally:
                self.store.update(job_id, operation=None)
                lock.release()

        threading.Thread(target=target, name=f"gplay-{operation}-{job_id[:8]}", daemon=True).start()

    def _job_dir(self, job: dict[str, Any]) -> Path:
        return self.config.jobs_dir / job["id"]

    def _prepare(self, job: dict[str, Any]) -> None:
        directory = self._job_dir(job)
        source = directory / "source.xlsx"
        catalog = directory / "catalog.xlsx"
        sheets = xlsx_sheet_names(source)
        source_format = source_workbook_format(sheets)
        if source_format == "google_catalog":
            shutil.copy2(source, catalog)
        elif source_format == "appleparty":
            command = [
                str(self.config.catalog_python),
                str(self.config.catalog_script),
                "--package",
                job["package"],
                "--profile",
                job["profile"],
                "import-appleparty",
                str(source),
                "--output",
                str(catalog),
            ]
            self.runner.run(command)
        else:
            raise RequestError(
                "Excel 需要包含 AppleParty 与 PricePoints，或包含 products、localizations 与 metadata；请下载示例核对页签"
            )
        command = build_catalog_command(
            self.config,
            "validate",
            catalog,
            None,
            job["package"],
            job["profile"],
        )
        validation = parse_last_json(self.runner.run(command).stdout)
        validation["source_format"] = source_format
        self.store.update(
            job["id"],
            status="validated",
            validation=validation,
            preview={
                "source": workbook_preview(source),
                "catalog": workbook_preview(catalog),
                "offers": workbook_preview(catalog, preferred_sheet="offers"),
            },
            plan=None,
            stage="Excel 本地校验通过",
            error=None,
        )

    def _next_dir(self, job: dict[str, Any], prefix: str) -> Path:
        directory = self._job_dir(job)
        index = 1
        while (directory / f"{prefix}-{index}").exists():
            index += 1
        output = directory / f"{prefix}-{index}"
        output.mkdir()
        return output

    def _read_plan(self, output_dir: Path) -> dict[str, Any]:
        path = output_dir / "plan.json"
        if not path.is_file():
            raise RuntimeError("上传器没有生成 plan.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def _saved_plan_path(self, job: dict[str, Any]) -> Optional[Path]:
        relative = job.get("plan_file")
        directory = self._job_dir(job).resolve()
        if relative:
            candidate = (directory / str(relative)).resolve()
            if os.path.commonpath((str(directory), str(candidate))) != str(directory):
                raise RequestError("任务保存的计划路径无效")
            if not candidate.is_file():
                raise RequestError("任务的同步计划已丢失，请返回步骤 3 重新检查差异")
            return candidate

        expected = job.get("plan") or {}
        candidates = sorted(
            directory.glob("plan-*/plan.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                summary = summarize_plan(json.loads(candidate.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if all(
                int(summary.get(key) or 0) == int(expected.get(key) or 0)
                for key in ("total", "actionable", "blocked")
            ):
                return candidate.resolve()
        return None

    def _plan(self, job: dict[str, Any]) -> None:
        directory = self._job_dir(job)
        catalog = directory / "catalog.xlsx"
        if not catalog.is_file():
            raise RequestError("请先上传并校验 Excel")
        output = self._next_dir(job, "plan")
        command = build_catalog_command(
            self.config,
            "plan",
            catalog,
            output,
            job["package"],
            job["profile"],
            allow_one_time_price_changes=job.get("allow_one_time_price_changes", False),
        )
        self.runner.run(command, accepted=(0, 2))
        summary = summarize_plan(self._read_plan(output))
        if summary["blocked"]:
            status, stage = "blocked", "发现不安全变更，不能提交"
        elif summary["actionable"]:
            status, stage = "ready", f"差异已生成，待提交 {summary['actionable']} 项"
        else:
            status, stage = "verified", "核对通过，Excel 与 Google Play 已一致"
        self.store.update(
            job["id"],
            status=status,
            stage=stage,
            plan=summary,
            plan_file=str((output / "plan.json").relative_to(directory)),
            active_batch=None,
            error=None,
        )

    def _apply(self, job: dict[str, Any], confirmation: str) -> None:
        if not job.get("plan"):
            raise RequestError("请先生成只读计划")
        if job["plan"].get("blocked"):
            raise RequestError("计划存在阻塞项，不能提交")
        batch_number = int(job.get("apply_runs") or 0) + 1
        active_batch = {
            "number": batch_number,
            "phase": "applying",
            "items": selected_batch_items(job["plan"]),
            "started_at": utc_now(),
        }
        self.store.update(
            job["id"],
            active_batch=active_batch,
            stage=f"正在向 Google Play 同步 {len(active_batch['items'])} 项",
        )
        directory = self._job_dir(job)
        output = self._next_dir(job, "apply")
        command = build_catalog_command(
            self.config,
            "apply",
            directory / "catalog.xlsx",
            output,
            job["package"],
            job["profile"],
            confirmation=confirmation,
            allow_one_time_price_changes=job.get("allow_one_time_price_changes", False),
            input_plan=self._saved_plan_path(job),
        )
        self.runner.run(command)
        applied_plan = summarize_plan(self._read_plan(output))
        selected = min(applied_plan["actionable"], BATCH_SIZE)
        remaining_path = output / "remaining-plan.json"
        if not remaining_path.is_file():
            raise RuntimeError("上传器没有生成剩余同步计划")
        remaining_plan = json.loads(remaining_path.read_text(encoding="utf-8"))
        remaining_summary = summarize_plan(remaining_plan)
        self.store.update(
            job["id"],
            status="ready",
            apply_runs=int(job.get("apply_runs") or 0) + 1,
            applied_total=int(job.get("applied_total") or 0) + selected,
            last_apply={"selected": selected, "completed_at": utc_now()},
            plan=remaining_summary,
            plan_file=str(remaining_path.relative_to(directory)),
            active_batch=None,
            stage=f"已成功同步 {selected} 项，剩余 {remaining_summary['actionable']} 项",
        )

    def _apply_all(self, job: dict[str, Any], confirmation: str) -> None:
        transient_recoveries = 0
        while True:
            current = self.store.get(job["id"])
            plan = current.get("plan")
            if not plan:
                raise RequestError("请先生成只读计划")
            if plan.get("blocked"):
                self.store.update(job["id"], active_batch=None)
                return
            remaining = int(plan.get("actionable") or 0)
            if remaining == 0:
                if current.get("status") == "verified":
                    self.store.update(
                        job["id"], active_batch=None, error=None
                    )
                    return
                self.store.update(
                    job["id"],
                    stage="所有内容已写入，正在最终核对 Google Play 实际结果",
                    active_batch=None,
                    error=None,
                )
                self._verify(self.store.get(job["id"]))
                return

            self.store.update(
                job["id"],
                stage=f"正在自动同步，剩余 {remaining} 项",
                error=None,
            )
            try:
                self._apply(self.store.get(job["id"]), confirmation)
            except Exception as error:
                if not is_transient_google_error(error):
                    raise
                transient_recoveries = self._recover_transient_apply(
                    job["id"],
                    error,
                    transient_recoveries,
                )
                recovered_plan = self.store.get(job["id"]).get("plan") or {}
                recovered_remaining = int(recovered_plan.get("actionable") or 0)
                if recovered_remaining < remaining:
                    transient_recoveries = 0
                continue
            transient_recoveries = 0
            refreshed = self.store.get(job["id"])
            refreshed_plan = refreshed.get("plan") or {}
            if refreshed_plan.get("blocked"):
                self.store.update(job["id"], active_batch=None)
                return
            after = int(refreshed_plan.get("actionable") or 0)
            if after >= remaining:
                raise RuntimeError(
                    "同步后待处理项没有减少，已停止以避免重复写入；请返回步骤 3 重新检查线上状态"
                )

    def _recover_transient_apply(
        self,
        job_id: str,
        error: Exception,
        recovery_count: int,
    ) -> int:
        latest_error = error
        while recovery_count < MAX_TRANSIENT_APPLY_RECOVERIES:
            recovery_count += 1
            delay = TRANSIENT_APPLY_RETRY_DELAYS[recovery_count - 1]
            current = self.store.get(job_id)
            active_batch = current.get("active_batch")
            if active_batch:
                active_batch = {**active_batch, "phase": "refreshing"}
            self.store.update(
                job_id,
                status="running",
                stage=(
                    f"Google Play 暂时不可用，{delay} 秒后自动重新检查并继续"
                    f"（自动恢复 {recovery_count}/{MAX_TRANSIENT_APPLY_RECOVERIES}）"
                ),
                active_batch=active_batch,
                error=None,
            )
            time.sleep(delay)
            self.store.update(
                job_id,
                stage=(
                    "正在重新检查线上状态并继续"
                    f"（自动恢复 {recovery_count}/{MAX_TRANSIENT_APPLY_RECOVERIES}）"
                ),
            )
            try:
                self._plan(self.store.get(job_id))
                return recovery_count
            except Exception as reconcile_error:
                if not is_transient_google_error(reconcile_error):
                    raise
                latest_error = reconcile_error

        raise RuntimeError(
            f"Google Play 服务连续不可用，已自动恢复 {MAX_TRANSIENT_APPLY_RECOVERIES} 次："
            f"{latest_error}"
        ) from latest_error

    def _verify(self, job: dict[str, Any]) -> None:
        directory = self._job_dir(job)
        output = self._next_dir(job, "verify")
        command = build_catalog_command(
            self.config,
            "verify",
            directory / "catalog.xlsx",
            output,
            job["package"],
            job["profile"],
        )
        result = self.runner.run(command, accepted=(0, 3))
        summary = summarize_plan(self._read_plan(output))
        if result.returncode == 0:
            self.store.update(
                job["id"],
                status="verified",
                stage="验证通过，Excel 与 Google Play 完全一致",
                plan=summary,
                plan_file=str((output / "plan.json").relative_to(directory)),
                active_batch=None,
                error=None,
            )
        else:
            self.store.update(
                job["id"],
                status="ready",
                stage=f"验证未通过，仍有 {summary['actionable']} 项差异",
                plan=summary,
                plan_file=str((output / "plan.json").relative_to(directory)),
                active_batch=None,
                error="还有差异未成功同步；请查看失败项，修正后重新提交。",
            )


class WebApplication:
    def __init__(self, config: Config):
        self.config = config
        self.store = JobStore(config.jobs_dir)
        resumable_jobs = self.store.recover_interrupted()
        self.runner = CommandRunner(config)
        self.auth = AuthManager(config, self.runner)
        self.jobs = JobService(config, self.store, self.runner)
        for job_id in resumable_jobs:
            self.jobs.resume_interrupted_apply_all(job_id)
        self.csrf_token = secrets.token_urlsafe(32)
        self.static_dir = Path(__file__).resolve().parent / "static"

    def handler(self) -> type[BaseHTTPRequestHandler]:
        application = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GooglePlayExcelWeb/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:
                sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

            def _headers(self, status: int, content_type: str, length: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'",
                )
                self.end_headers()

            def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
                raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self._headers(status, "application/json; charset=utf-8", len(raw))
                self.wfile.write(raw)

            def _error(self, error: Exception) -> None:
                status = error.status if isinstance(error, RequestError) else 500
                if not isinstance(error, RequestError):
                    traceback.print_exc()
                self._json({"error": str(error)}, status)

            def _require_csrf(self) -> None:
                if not secrets.compare_digest(
                    self.headers.get("X-CSRF-Token", ""), application.csrf_token
                ):
                    raise RequestError("页面会话已失效，请刷新后重试", HTTPStatus.FORBIDDEN)

            def _json_body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 64 * 1024:
                    raise RequestError("请求内容无效")
                try:
                    value = json.loads(self.rfile.read(length))
                except json.JSONDecodeError as error:
                    raise RequestError("请求 JSON 无效") from error
                if not isinstance(value, dict):
                    raise RequestError("请求 JSON 无效")
                return value

            def _multipart(self, max_bytes: int) -> cgi.FieldStorage:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > max_bytes + 1024 * 1024:
                    raise RequestError("上传文件过大", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data"):
                    raise RequestError("上传请求格式无效")
                return cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": content_type,
                        "CONTENT_LENGTH": str(length),
                    },
                    keep_blank_values=True,
                )

            def _serve_file(self, path: Path, download_name: Optional[str] = None) -> None:
                if not path.is_file():
                    raise RequestError("文件不存在", HTTPStatus.NOT_FOUND)
                raw = path.read_bytes()
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                if download_name:
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{download_name}"'
                    )
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                try:
                    parsed = urlparse(self.path)
                    path = parsed.path
                    if path == "/api/config":
                        self._json(public_config(application.config, application.csrf_token))
                        return
                    if path == "/api/apps":
                        self._json({"apps": load_app_profiles(application.config.apps_file)})
                        return
                    if path == "/api/demo":
                        self._json(workbook_preview(application.config.demo_workbook))
                        return
                    if path == "/api/auth/status":
                        profile = parse_qs(parsed.query).get("profile", [""])[0]
                        if not profile:
                            raise RequestError("请先选择 App")
                        self._json(application.auth.status(profile))
                        return
                    if path == "/api/auth/data":
                        profile = parse_qs(parsed.query).get("profile", [""])[0]
                        if not profile:
                            raise RequestError("请先选择 App")
                        self._json(application.auth.data(profile))
                        return
                    if path == "/api/jobs":
                        self._json(
                            {
                                "jobs": [
                                    public_job_status(job)
                                    for job in application.store.list_recent()
                                ]
                            }
                        )
                        return
                    match = re.fullmatch(r"/api/jobs/([^/]+)/status", path)
                    if match:
                        job_id = validate_job_id(match.group(1))
                        raw_after = parse_qs(parsed.query).get("after", ["-1"])[0]
                        try:
                            after = int(raw_after)
                        except ValueError as error:
                            raise RequestError("状态版本无效") from error
                        deadline = time.monotonic() + 20
                        while True:
                            job = application.store.get(job_id)
                            revision = int(job.get("state_revision") or 0)
                            if revision > after or not job.get("operation"):
                                break
                            if time.monotonic() >= deadline:
                                break
                            time.sleep(0.25)
                        self._json(public_job_status(job))
                        return
                    match = re.fullmatch(r"/api/jobs/([^/]+)", path)
                    if match:
                        self._json(public_job(application.store.get(match.group(1))))
                        return
                    match = re.fullmatch(r"/api/jobs/([^/]+)/download", path)
                    if match:
                        job_id = validate_job_id(match.group(1))
                        kind = parse_qs(parsed.query).get("file", ["catalog"])[0]
                        names = {
                            "source": ("source.xlsx", "source.xlsx"),
                            "catalog": ("catalog.xlsx", "google-play-result.xlsx"),
                        }
                        if kind not in names:
                            raise RequestError("不支持的下载类型")
                        relative, download_name = names[kind]
                        self._serve_file(
                            application.config.jobs_dir / job_id / relative, download_name
                        )
                        return
                    if path == "/demo.xlsx":
                        self._serve_file(application.config.demo_workbook, "demo.xlsx")
                        return
                    if path == "/":
                        self._serve_file(application.static_dir / "index.html")
                        return
                    static_match = re.fullmatch(r"/static/([A-Za-z0-9._-]+)", path)
                    if static_match:
                        self._serve_file(application.static_dir / static_match.group(1))
                        return
                    if path in {"/styles.css", "/app.js"}:
                        self._serve_file(application.static_dir / path.lstrip("/"))
                        return
                    raise RequestError("页面不存在", HTTPStatus.NOT_FOUND)
                except Exception as error:
                    self._error(error)

            def do_POST(self) -> None:
                try:
                    self._require_csrf()
                    parsed = urlparse(self.path)
                    if parsed.path == "/api/auth":
                        if any(job.get("operation") for job in application.store.list_recent()):
                            raise RequestError("当前有任务正在运行，完成后才能更换服务账号", HTTPStatus.CONFLICT)
                        form = self._multipart(MAX_CREDENTIAL_BYTES)
                        upload = form["file"] if "file" in form else None
                        if upload is None or not getattr(upload, "file", None):
                            raise RequestError("请选择服务账号 JSON")
                        raw = upload.file.read(MAX_CREDENTIAL_BYTES + 1)
                        target = resolve_app_target(
                            application.config.apps_file,
                            form.getfirst("app", ""),
                            profile=form.getfirst("profile", ""),
                        )
                        self._json(
                            application.auth.install(target["name"], raw),
                            HTTPStatus.CREATED,
                        )
                        return
                    if parsed.path == "/api/auth/check":
                        body = self._json_body()
                        target = resolve_app_target(
                            application.config.apps_file,
                            str(body.get("app", "")),
                            profile=str(body.get("profile", "")),
                            package=str(body.get("package", "")),
                            require_package=True,
                        )
                        self._json(
                            application.auth.check(
                                target["name"],
                                target["pack"],
                            )
                        )
                        return
                    if parsed.path == "/api/jobs":
                        form = self._multipart(MAX_UPLOAD_BYTES)
                        upload = form["file"] if "file" in form else None
                        if upload is None or not getattr(upload, "file", None):
                            raise RequestError("请选择 Excel 文件")
                        target = resolve_app_target(
                            application.config.apps_file,
                            form.getfirst("app", ""),
                            profile=form.getfirst("profile", ""),
                            package=form.getfirst("package", ""),
                            require_package=True,
                        )
                        job = application.store.create(
                            upload.filename or "",
                            upload.file,
                            package=target["pack"],
                            profile=target["name"],
                            allow_one_time_price_changes=(
                                form.getfirst("allow_one_time_price_changes", "false") == "true"
                            ),
                        )
                        application.jobs.start_prepare(job["id"])
                        self._json(public_job(application.store.get(job["id"])), HTTPStatus.CREATED)
                        return
                    match = re.fullmatch(
                        r"/api/jobs/([^/]+)/(plan|apply|apply-all|verify)",
                        parsed.path,
                    )
                    if match:
                        job_id = validate_job_id(match.group(1))
                        action = match.group(2)
                        if action == "plan":
                            application.jobs.start_plan(job_id)
                        elif action == "verify":
                            application.jobs.start_verify(job_id)
                        else:
                            body = self._json_body()
                            confirmation = str(body.get("confirm_package", ""))
                            if action == "apply-all":
                                application.jobs.start_apply_all(job_id, confirmation)
                            else:
                                application.jobs.start_apply(job_id, confirmation)
                        self._json(public_job(application.store.get(job_id)), HTTPStatus.ACCEPTED)
                        return
                    match = re.fullmatch(r"/api/jobs/([^/]+)/clear", parsed.path)
                    if match:
                        job_id = validate_job_id(match.group(1))
                        application.store.clear(job_id)
                        self._json({"cleared": True, "id": job_id})
                        return
                    raise RequestError("接口不存在", HTTPStatus.NOT_FOUND)
                except Exception as error:
                    self._error(error)

        return Handler


def create_server(config: Config) -> ThreadingHTTPServer:
    config.validate()
    application = WebApplication(config)
    server = ThreadingHTTPServer((config.host, config.port), application.handler())
    server.daemon_threads = True
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Play Excel 网页上传器")
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认 8787")
    args = parser.parse_args()
    config = Config.from_environment()
    if args.host or args.port:
        config = Config(
            root=config.root,
            catalog_script=config.catalog_script,
            catalog_python=config.catalog_python,
            gplay_dir=config.gplay_dir,
            jobs_dir=config.jobs_dir,
            host=args.host or config.host,
            port=args.port or config.port,
            demo_workbook=config.demo_workbook,
            apps_file=config.apps_file,
        )
    server = create_server(config)
    print(f"Google Play Excel 网页上传器：http://{config.host}:{config.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
