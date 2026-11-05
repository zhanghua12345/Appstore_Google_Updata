"""Workflow gates and recovery, without remote writes or persistent test data."""
import copy
import base64
import io
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PIL import Image
from openpyxl import load_workbook

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("store_workflow", PROJECT / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)

ITEMS = [
    {"kind": "coins", "price": "12.99", "reference_name": "Coins 12.99_1"},
    {"kind": "bag", "intro_price": "2.99", "renewal_price": "12.99", "period": "ONE_WEEK", "reference_name": "Bag Week 2.99_1"},
    {"kind": "membership", "intro_price": "2.99", "renewal_price": "12.99", "period": "ONE_WEEK", "reference_name": "Membership Week 2.99_1"},
]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        shutil.copyfile(PROJECT / "demo.xlsx", self.root / "demo.xlsx")
        (self.root / "GooglePlay").symlink_to(PROJECT / "GooglePlay", target_is_directory=True)
        self.task = workflow.Task(self.root)
        self.task.start("三种商品", copy.deepcopy(ITEMS))

    def first_four(self, platforms=None):
        self.task.set_step(2, {"app": "ExampleApp"})
        self.task.set_step(1, self.task.load()["steps"]["1"]["data"])
        self.task.set_step(3, {"platforms": platforms or ["appstore", "googleplay"]})
        self.task.set_step(4, {"enabled": True, "prefix": "example"})
        self.task.set_step(1, self.task.load()["steps"]["1"]["data"])

    def materials(self):
        self.first_four()
        Image.new("RGB", (32, 32), "white").save(self.root / "review.png")
        self.task.screenshots(["review.png"])
        self.task.mark_screenshot_content()
        apple = Path(self.temp.name) / "AuthKey.p8"
        apple.write_text("test fixture only")
        google = Path(self.temp.name) / "account.json"
        google.write_text("{}")
        self.task.credentials("appstore", {"issuer_id": "issuer", "key_id": "key", "app_id": "123", "p8_path": str(apple)})
        self.task.credentials("googleplay", {"package": "com.example.app", "service_account_path": str(google)})
        # Only API responses are mocked; actual file/Excel/conversion logic runs.
        self.apple_check = mock.patch.object(workflow, "apple_read_check", return_value={"app_id": "123"})
        self.google_check = mock.patch.object(workflow, "google_read_check", return_value={"package": "com.example.app"})
        self.apple_check.start()
        self.google_check.start()
        self.addCleanup(self.apple_check.stop)
        self.addCleanup(self.google_check.stop)
        self.assertTrue(self.task.preflight("appstore")["ready"])
        self.assertTrue(self.task.preflight("googleplay")["ready"])

    def authorize(self):
        self.materials()
        self.task.generate()
        self.task.mark_shown()
        self.task.approve("我已经确认")

    def test_prefix_normalizes_once(self):
        for value in ("dinreel", "dinreel.", " dinreel. "):
            self.assertEqual(workflow.normalize_prefix(value), "dinreel.")
        with self.assertRaises(workflow.WorkflowError):
            workflow.normalize_prefix("another..app")

    def test_preview_lists_every_row_with_demo_candidate_ids(self):
        self.first_four()
        state = self.task.load()
        items = state["steps"]["1"]["data"]["items"]
        items.extend(copy.deepcopy(items) * 10)
        self.task.save(state)
        preview = self.task.preview()
        self.assertEqual(len(preview["rows"]), 33)
        self.assertEqual(preview["columns"][1], "ID")
        self.assertEqual(preview["rows"][0]["ID"], "example.coins_12.99_1")
        self.assertEqual(preview["rows"][1]["ID"], "example.bag_week_2.99_12.99_1")
        self.assertEqual(preview["rows"][2]["ID"], "example.vip_week_2.99_12.99_1")
        self.assertEqual(preview["rows"][0]["一次性价"], "12.99")
        self.assertEqual(preview["rows"][2]["首周价"], "2.99")
        self.assertEqual(preview["rows"][2]["续订价"], "12.99")
        self.assertEqual(preview["app"], "ExampleApp")
        text = json.dumps(preview)
        self.assertNotIn("appstore", text)
        self.assertNotIn("googleplay", text)

    def test_preview_asks_for_app_before_showing_ids(self):
        preview = self.task.preview()
        self.assertEqual(preview["rows"], [])
        self.assertEqual(preview["columns"][1], "ID")
        self.assertIn("本次 App 叫什么", preview["question"])
        self.task.set_step(2, {"app": "DinReel"})
        self.assertEqual(self.task.preview()["rows"][1]["ID"], "dinreel.bag_week_2.99_12.99_1")

    def test_app_or_prefix_change_requires_fresh_id_confirmation(self):
        self.task.set_step(2, {"app": "DinReel"})
        self.task.set_step(1, self.task.load()["steps"]["1"]["data"])
        self.task.set_step(3, {"platforms": ["googleplay"]})
        self.task.set_step(4, {"enabled": True, "prefix": "dinreel"})
        self.assertTrue(self.task.load()["steps"]["1"]["confirmed"])
        self.task.set_step(4, {"enabled": True, "prefix": "custom"})
        self.assertFalse(self.task.load()["steps"]["1"]["confirmed"])
        self.assertEqual(self.task.preview()["rows"][0]["ID"], "custom.coins_12.99_1")
        self.task.set_step(1, self.task.load()["steps"]["1"]["data"])
        self.task.set_step(2, {"app": "OtherApp"})
        self.assertFalse(self.task.load()["steps"]["1"]["confirmed"])
        self.assertEqual(self.task.preview()["rows"][0]["ID"], "otherapp.coins_12.99_1")

    def test_preview_requests_legal_prefix_for_app_name_with_spaces(self):
        self.task.set_step(2, {"app": "My App"})
        preview = self.task.preview()
        self.assertEqual(preview["rows"], [])
        self.assertIn("前缀", preview["question"])

    def test_preview_preserves_source_ids_and_fills_blank_with_candidate(self):
        state = self.task.load()
        state["steps"]["1"]["data"]["items"][0]["source_product_id"] = "otherapp.coins_12.99_1"
        state["steps"]["1"]["data"]["items"][1]["source_product_id"] = "bag_week_2.99_12.99_1"
        state["steps"]["1"]["data"]["items"][2]["source_product_id"] = "  "
        self.task.save(state)
        self.task.set_step(2, {"app": "ExampleApp"})
        rows = self.task.preview()["rows"]
        self.assertEqual(rows[0]["ID"], "otherapp.coins_12.99_1")
        self.assertIn("不符", rows[0]["ID说明"])
        self.assertEqual(rows[1]["ID"], "exampleapp.bag_week_2.99_12.99_1")
        self.assertEqual(rows[2]["ID"], "exampleapp.vip_week_2.99_12.99_1")
        self.assertEqual(self.task.load()["steps"]["1"]["data"]["items"][0]["source_product_id"], "otherapp.coins_12.99_1")

    def test_new_task_has_no_default_app_or_platform(self):
        self.first_four()
        self.task.start("新任务", copy.deepcopy(ITEMS), replace=True)
        state = self.task.load()
        self.assertEqual(state["steps"]["2"]["data"], {})
        self.assertEqual(state["steps"]["3"]["data"], {})
        self.assertFalse(state["steps"]["1"]["confirmed"])

    def test_preanswered_app_platform_survive_same_interpretation_confirmation(self):
        self.task.set_step(2, {"app": "ChosenApp"})
        self.task.set_step(3, {"platforms": ["googleplay", "appstore"]})
        self.task.set_step(1, self.task.load()["steps"]["1"]["data"])
        self.assertEqual(self.task.current(self.task.load()), 4)

    def test_remembered_platform_and_prefix_are_suggestions_not_defaults(self):
        self.task.set_step(2, {"app": "ExampleApp"})
        self.task.enable_cache()
        self.first_four()
        self.task.start("新请求", copy.deepcopy(ITEMS), replace=True)
        self.assertEqual(self.task.suggestions(), {})
        self.task.set_step(2, {"app": "ExampleApp"})
        self.assertEqual(self.task.suggestions(), {})
        self.task.enable_cache()
        suggestion = self.task.suggestions()
        self.assertEqual(suggestion["prefix"]["prefix"], "example.")
        self.assertEqual(suggestion["platforms"], ["appstore", "googleplay"])
        state = self.task.load()
        self.assertEqual(state["steps"]["3"]["data"], {})
        self.assertFalse(state["steps"]["4"]["confirmed"])

    def test_missing_screenshot_can_be_filled_later(self):
        self.first_four()
        self.task.screenshots(["missing.png"], count_failure=False)
        self.assertEqual(self.task.load()["screenshot_failures"], 0)
        for attempt in range(1, 4):
            result = self.task.screenshots(["missing.png"])
            self.assertEqual(result["attempts"], attempt)
        restored = workflow.Task(self.root)
        self.assertIsNone(restored.load()["paused_step"])
        restored.later(5)
        self.assertEqual(restored.current(restored.load()), 7)
        Image.new("RGB", (32, 32)).save(self.root / "review.png")
        self.assertTrue(restored.screenshots()["ok"])
        self.assertEqual(restored.load()["screenshot_failures"], 0)

    def test_external_screenshot_is_copied_to_root(self):
        self.first_four()
        path = Path(self.temp.name) / "outside.png"
        Image.new("RGB", (32, 32)).save(path)
        self.assertTrue(self.task.screenshots([str(path)])["ok"])
        self.assertTrue(path.is_file())
        self.assertTrue((self.root / "outside.png").is_file())

    def test_later_materials_are_listed_without_skipping_platforms(self):
        self.first_four()
        for step in (5, 7, 8):
            self.task.later(step)
        state = self.task.load()
        self.assertEqual(self.task.current(state), 9)
        self.assertEqual(self.task.active_platforms(state), ["appstore", "googleplay"])
        result = self.task.check_intake()
        self.assertFalse(result["ready"])
        items = [row["item"] for row in result["missing"]]
        self.assertIn("App Store 购买页审核截图（PNG/JPEG，根目录）", items)
        self.assertTrue(any("Issuer ID" in item for item in items))
        self.assertTrue(any("服务账号 JSON" in item for item in items))
        self.assertIsNone(self.task.load()["authorization"])

    def test_early_questions_and_excel_can_be_answered_later(self):
        for step in (1, 2, 3, 4):
            self.task.later(step)
        self.assertEqual(self.task.current(self.task.load()), 5)
        self.authorize()
        workbook = self.task.load()["workbook"]
        self.task.later(9)
        state = self.task.load()
        self.assertEqual(state["workbook"], workbook)
        self.assertIsNone(state["authorization"])
        self.assertFalse(self.task.can_submit())

    def test_complete_intake_creates_review_workbook(self):
        self.materials()
        result = self.task.check_intake()
        self.assertTrue(result["ready"])
        self.assertTrue(Path(result["file"]).is_file())
        self.assertFalse(self.task.load()["workbook"]["shown"])
        self.assertIsNone(self.task.load()["authorization"])

    def test_screenshot_changes_and_missing_file_revoke_authorization(self):
        self.authorize()
        self.assertFalse(self.task.screenshots(["review.png"])["needs_visual_check"])
        self.assertIsNotNone(self.task.load()["authorization"])
        Image.new("RGB", (32, 32), "black").save(self.root / "review.png")
        self.assertTrue(self.task.screenshots(["review.png"])["needs_visual_check"])
        self.assertIsNone(self.task.load()["authorization"])
        self.assertFalse(self.task.load()["steps"]["5"]["confirmed"])

    def test_removed_screenshot_invalidates_even_after_approval(self):
        self.authorize()
        (self.root / "review.png").unlink()
        self.assertFalse(self.task.screenshots()["ok"])
        self.assertIsNone(self.task.load()["authorization"])

    def test_pause_resume_and_edit_rewinds(self):
        self.authorize()
        self.task.pause()
        self.assertFalse(self.task.can_submit())
        self.task.resume()
        self.task.set_step(2, {"app": "ChangedApp"})
        state = self.task.load()
        self.assertFalse(state["steps"]["1"]["confirmed"])
        self.assertFalse(state["steps"]["3"]["confirmed"])
        self.assertIsNone(state["authorization"])
        self.assertIsNone(state["workbook"])

    def test_cannot_generate_before_materials_or_approve_before_shown(self):
        with self.assertRaises(workflow.WorkflowError):
            self.task.generate()
        self.materials()
        self.task.generate()
        with self.assertRaises(workflow.WorkflowError):
            self.task.approve("继续")

    def test_negative_confirmation_rejected(self):
        self.materials()
        self.task.generate()
        self.task.mark_shown()
        for message in ("还没有确认", "不要继续", "确认前改成9.99", "继续吗", "不确认", "确认有误"):
            with self.subTest(message=message), self.assertRaises(workflow.WorkflowError):
                self.task.approve(message)
        self.task.approve("继续")
        self.assertTrue(self.task.load()["steps"]["9"]["confirmed"])

    def test_manual_excel_edit_requires_new_confirmation(self):
        self.authorize()
        path = self.root / self.task.load()["workbook"]["file"]
        with path.open("ab") as stream:
            stream.write(b"modified")
        with self.assertRaises(workflow.WorkflowError):
            self.task.can_submit()

    def test_fresh_checks_bind_revision_and_file(self):
        self.authorize()
        self.assertFalse(self.task.can_submit())
        self.task.preflight("appstore", "final")
        self.assertFalse(self.task.can_submit())
        self.task.preflight("googleplay", "final")
        self.assertTrue(self.task.can_submit())
        state = self.task.load()
        state["preflight"]["appstore_final"]["workbook_sha256"] = "other"
        self.task.save(state)
        self.assertFalse(self.task.can_submit())
        self.task.preflight("appstore", "final")
        state = self.task.load()
        state["preflight"]["appstore_final"]["checked_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        self.task.save(state)
        self.assertFalse(self.task.can_submit())

    def test_excel_semantics_and_production_conversion(self):
        self.materials()
        self.task.generate()
        state = self.task.load()
        w = load_workbook(self.root / state["workbook"]["file"])
        self.addCleanup(w.close)
        ids = [row[1] for row in list(w["AppleParty"].values)[1:]]
        self.assertTrue(all(pid.startswith("example.") for pid in ids))
        self.assertEqual(len(ids), 3)
        self.assertEqual(w["PricePoints"]["C2"].value, "12.99")
        self.assertEqual(w["IntroductoryOffers"].max_row, 3)
        self.assertEqual(w["IntroductoryOffers"]["C2"].value, "2.99")
        self.assertTrue(all(row[5] == "review.png" for row in list(w["AppleParty"].values)[1:]))
        self.assertEqual(state["steps"]["9"]["data"]["google_conversion"], {"products": 3, "offers": 2})
        self.assertEqual(set(w.sheetnames), {"商品规划", "AppleParty", "PricePoints", "IntroductoryOffers", "Territories"})

    def test_no_credentials_in_project_or_secret_body_in_state(self):
        with self.assertRaises(workflow.WorkflowError):
            self.task.credentials("appstore", {"private_key": "secret"})
        with self.assertRaises(workflow.WorkflowError):
            self.task.credentials("googleplay", {"service_account_path": "key.json"})

    def test_google_only_skips_apple_requirements(self):
        self.first_four(["googleplay"])
        self.assertTrue(self.task.preflight("appstore")["skipped"])
        self.assertEqual(self.task.current(self.task.load()), 8)

    def test_excel_only_requires_no_platform_keys_or_screenshot(self):
        self.first_four()
        self.task.arrange("先跳过appstore，先做excl，不要提交")
        self.task.generate()
        self.task.mark_shown()
        self.task.approve("确认无误")
        state = self.task.load()
        self.assertEqual(self.task.active_platforms(state), [])
        self.assertEqual(state["steps"]["9"]["data"]["products"], 3)
        self.assertIn("pending", state["steps"]["9"]["data"]["google_conversion"])
        self.assertFalse(self.task.can_submit())

    def test_platform_skip_requests_are_distinct_and_survive_restart(self):
        self.first_four()
        for note, active in [
            ("先跳过appstore，我先提交google", ["googleplay"]),
            ("先跳过google，先提交appstore", ["appstore"]),
            ("不提交App Store，先上传Google Play", ["googleplay"]),
        ]:
            self.task.arrange("恢复全部")
            self.task.arrange(note)
            restored = workflow.Task(self.root)
            self.assertEqual(restored.active_platforms(restored.load()), active)

    def test_google_can_proceed_while_apple_materials_are_missing(self):
        self.first_four()
        self.task.arrange("先跳过App Store，先提交Google Play")
        credential = Path(self.temp.name) / "google.json"
        credential.write_text("{}")
        self.task.credentials("googleplay", {"package": "com.example.app", "service_account_path": str(credential)})
        with mock.patch.object(workflow, "google_read_check", return_value={}):
            self.assertTrue(self.task.preflight("googleplay")["ready"])
            self.task.generate()
            self.task.mark_shown()
            self.task.approve("继续")
            self.task.preflight("googleplay", "final")
            self.assertTrue(self.task.can_submit())
        self.assertEqual(self.task.load()["authorization"]["platforms"], ["googleplay"])

    def test_changing_app_clears_old_targets_and_isolates_cache(self):
        self.materials()
        self.task.arrange("先跳过Google，先提交App Store")
        self.task.set_step(2, {"app": "DifferentApp"})
        self.assertEqual(self.task.cached_credentials("appstore"), {})
        self.assertEqual(self.task.load()["steps"]["8"]["data"], {})
        self.assertEqual(self.task.load()["steps"]["3"]["data"], {})
        self.assertEqual(self.task.load()["steps"]["4"]["data"], {})
        self.assertFalse(self.task.load()["steps"]["1"]["confirmed"])
        self.assertEqual(self.task.load()["steps"]["6"]["data"], {"mode": "submit", "deferred": [], "notes": ""})
        self.task.set_step(2, {"app": "ExampleApp"})
        self.assertEqual(self.task.cached_credentials("appstore"), {})
        self.assertFalse(self.task.load()["steps"]["7"]["confirmed"])

    def test_root_inventory_and_explicit_cache_clear_restart(self):
        (self.root / "review.png").write_bytes(b"image placeholder")
        (self.root / "AuthKey.p8").write_text("test key")
        inventory = self.task.inventory()["files"]
        self.assertIn({"name": "review.png", "kind": "image"}, inventory)
        self.assertIn({"name": "AuthKey.p8", "kind": "apple_key"}, inventory)
        self.task.set_step(2, {"app": "ExampleApp"})
        self.task.enable_cache()
        self.task.set_step(3, {"platforms": ["googleplay"]})
        self.assertTrue((self.task.credential_dir / "registry.json").is_file())
        self.task.clear_cache()
        self.assertFalse(self.task.load()["steps"]["2"]["confirmed"])
        self.assertFalse(self.task.load()["reuse_cache"])
        self.assertTrue((self.root / "AuthKey.p8").is_file())

    def test_unknown_notes_are_retained_for_agent_not_auto_approved(self):
        self.authorize()
        self.task.arrange("价格要按另一个市场的特殊合同处理")
        self.assertTrue(self.task.load()["steps"]["6"]["data"]["needs_agent"])
        self.assertIsNone(self.task.load()["authorization"])

    def test_root_key_discovery_and_copy_never_exposes_contents(self):
        self.first_four()
        secret = "THIS_IS_PRIVATE_TEST_CONTENT"
        (self.root / "apple.p8").write_text(secret)
        key = {"type": "service_account", "client_email": "test@example.invalid", "private_key": secret}
        (self.root / "account.json").write_text(json.dumps(key))
        self.assertEqual([f["name"] for f in self.task.credential_files("googleplay")], ["account.json"])
        files = self.task.credential_files("appstore")
        self.assertNotIn(secret, json.dumps(files))
        copied = Path(self.task.accept_credential_file("appstore", files[0]["path"]))
        self.assertTrue((self.root / "apple.p8").exists())
        self.assertEqual(copied.read_text(), secret)
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)

    def test_partial_apple_fields_are_saved_without_losing_prior_answers(self):
        self.task.credentials("appstore", {"issuer_id": "ISSUER"})
        self.task.credentials("appstore", {"key_id": "KEY"})
        saved = self.task.load()["steps"]["7"]["data"]["credentials"]
        self.assertEqual(saved, {"issuer_id": "ISSUER", "key_id": "KEY"})

    def test_schema_one_migrates_without_inventing_confirmation(self):
        self.task.path.unlink()
        legacy = {
            "schema": 1, "task_id": "legacy", "revision": 3, "status": "paused", "paused_step": 6,
            "steps": {str(n): {"name": "legacy", "confirmed": n < 5, "data": {}} for n in range(1, 8)},
            "preflight": {}, "authorization": None, "workbook": None, "screenshot_failures": 0,
        }
        legacy["steps"]["5"]["data"]["credentials"] = {"key_id": "KEY"}
        legacy["steps"]["6"]["data"]["credentials"] = {"package": "com.old.app"}
        self.task.save(legacy)
        state = self.task.load()
        self.assertEqual(state["schema"], 2)
        self.assertEqual(state["steps"]["7"]["data"]["credentials"]["key_id"], "KEY")
        self.assertEqual(state["steps"]["8"]["data"]["credentials"]["package"], "com.old.app")
        self.assertFalse(state["steps"]["7"]["confirmed"])
        self.assertEqual(state["paused_step"], 7)

    def test_unverified_online_access_cannot_be_ready(self):
        self.materials()
        result = self.task.preflight("googleplay", online=False)
        self.assertFalse(result["ready"])
        self.assertFalse(self.task.load()["steps"]["8"]["confirmed"])

    def test_apple_preflight_uses_valid_es256_and_only_read_requests(self):
        key = Path(self.temp.name) / "apple.p8"
        private = subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout"], capture_output=True, check=True).stdout
        key.write_bytes(private)
        config = {"app_id": "123", "bundle_id": "com.example.app", "key_id": "KEY", "issuer_id": "ISSUER", "p8_path": str(key)}
        requests = []
        def respond(request, **kwargs):
            requests.append(request)
            return io.BytesIO(json.dumps({"data": {"attributes": {"name": "ExampleApp", "bundleId": "com.example.app"}}}).encode())
        with mock.patch.object(workflow, "urlopen", side_effect=respond):
            self.assertEqual(workflow.apple_read_check(config)["app_name"], "ExampleApp")
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request.get_method() == "GET" for request in requests))
        token = requests[0].get_header("Authorization").split()[1]
        header, claims, signature = token.split(".")
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        self.assertEqual(json.loads(decode(header))["alg"], "ES256")
        self.assertEqual(json.loads(decode(claims))["iss"], "ISSUER")
        raw = decode(signature)
        self.assertEqual(len(raw), 64)
        def integer(value):
            value = value.lstrip(b"\0") or b"\0"
            if value[0] & 0x80:
                value = b"\0" + value
            return bytes([2, len(value)]) + value
        integers = integer(raw[:32]) + integer(raw[32:])
        der = bytes([0x30, len(integers)]) + integers
        signature_path = Path(self.temp.name) / "signature.der"
        signature_path.write_bytes(der)
        public_path = Path(self.temp.name) / "public.pem"
        public_path.write_bytes(subprocess.run(["openssl", "pkey", "-pubout", "-in", str(key)], capture_output=True, check=True).stdout)
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public_path), "-signature", str(signature_path)], input=(header + "." + claims).encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0)
        config["bundle_id"] = "com.different.app"
        with mock.patch.object(workflow, "urlopen", side_effect=respond), self.assertRaises(workflow.WorkflowError):
            workflow.apple_read_check(config)


if __name__ == "__main__":
    unittest.main()
