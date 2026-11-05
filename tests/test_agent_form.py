"""Local credential form behavior, persistence and request boundaries."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from agent_form import Forms, make_server
from workflow import Task


class FormTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.task = Task(self.root)
        self.task.start("金币", [{"kind": "coins", "price": "12.99", "reference_name": "Coins 12.99_1"}])
        self.task.set_step(2, {"app": "ExampleApp"})
        self.task.set_step(3, {"platforms": ["appstore"]})
        self.forms = Forms(self.root)

    def test_only_selected_platform_data_is_exposed_and_accepted(self):
        state = self.forms.snapshot()
        self.assertEqual(state["platforms"], ["appstore"])
        self.assertNotIn("googleplay", state["credentials"])
        with self.assertRaises(ValueError):
            self.forms.action({"action": "save", "platform": "googleplay", "config": {"package": "com.wrong.app"}})

    def test_confirming_local_root_key_persists_metadata_not_private_contents(self):
        (self.root / "AuthKey_TEST.p8").write_text("PRIVATE_TEST_VALUE")
        response = self.forms.action({"action": "save", "platform": "appstore",
            "config": {"issuer_id": "issuer", "key_id": "TEST", "app_id": "123"},
            "file": str(self.root / "AuthKey_TEST.p8")})
        self.assertNotIn("PRIVATE_TEST_VALUE", json.dumps(response))
        self.assertTrue((self.root / "AuthKey_TEST.p8").exists())
        cached = Task(self.root).cached_credentials("appstore")
        self.assertEqual(cached["key_id"], "TEST")
        self.assertTrue(Path(cached["p8_path"]).is_file())
        self.assertNotIn("PRIVATE_TEST_VALUE", self.task.path.read_text())
        self.assertFalse((self.task.credential_dir / "registry.json").exists())

    def test_notes_save_without_credential_fields(self):
        response = self.forms.action({"action": "arrange", "notes": "先跳过App Store，先做Excel"})
        self.assertEqual(response["state"]["execution"]["mode"], "excel_only")
        self.assertEqual(response["state"]["active_platforms"], [])
        self.assertEqual(response["state"]["credentials"]["appstore"], {})

    def test_later_keeps_apple_selected_and_final_check_lists_missing(self):
        response = self.forms.action({"action": "later", "step": 5})
        self.assertTrue(response["state"]["materials_later"]["5"])
        self.assertEqual(response["state"]["active_platforms"], ["appstore"])
        self.forms.action({"action": "later", "step": 7})
        result = self.forms.action({"action": "check-intake"})["result"]
        self.assertFalse(result["ready"])
        self.assertTrue(any("审核截图" in row["item"] for row in result["missing"]))
        self.assertTrue(any("Issuer ID" in row["item"] for row in result["missing"]))

    def test_read_access_check_is_independent_and_never_grants_submission(self):
        key = Path(self.temp.name) / "key.p8"
        key.write_text("TEST")
        self.task.credentials("appstore", {"issuer_id": "issuer", "key_id": "KEY", "app_id": "123", "p8_path": str(key)})
        with mock.patch("workflow.apple_read_check", return_value={"app_name": "ExampleApp"}):
            response = self.forms.action({"action": "check", "platform": "appstore", "phase": "access"})
        self.assertTrue(response["result"]["ready"])
        state = self.task.load()
        self.assertFalse(state["steps"]["1"]["confirmed"])
        self.assertFalse(state["steps"]["7"]["confirmed"])
        self.assertIsNone(state["authorization"])

    def test_cache_is_a_proposal_for_same_app_new_task(self):
        self.task.enable_cache()
        self.task.credentials("appstore", {"issuer_id": "issuer", "key_id": "KEY", "app_id": "123"})
        self.task.start("下一次", [{"kind": "coins", "price": "1.99"}], replace=True)
        self.task.set_step(2, {"app": "ExampleApp"})
        self.task.set_step(3, {"platforms": ["appstore"]})
        self.assertEqual(self.forms.snapshot()["credentials"]["appstore"], {})
        self.task.enable_cache()
        self.assertEqual(self.forms.snapshot()["credentials"]["appstore"]["key_id"], "KEY")
        self.assertNotIn("credentials", self.task.load()["steps"]["7"]["data"])
        self.assertFalse(self.task.load()["steps"]["7"]["confirmed"])

    def test_file_options_only_include_root_files(self):
        external = Path(self.temp.name) / "outside.p8"
        external.write_text("test key")
        self.task.enable_cache()
        self.task.cache_credentials("ExampleApp", "appstore", {"p8_path": str(external)})
        self.assertEqual(self.forms.snapshot()["files"]["appstore"], [])
        (self.root / "AuthKey.p8").write_text("test key")
        files = self.forms.snapshot()["files"]["appstore"]
        self.assertEqual([item["name"] for item in files], ["AuthKey.p8"])

    def test_http_token_origin_and_no_store(self):
        server = make_server(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        token = re.search(r"const token = '([^']+)'", html)[1]
        connection.request("GET", "/api/state")
        denied = connection.getresponse()
        denied.read()
        self.assertEqual(denied.status, 404)
        headers = {"X-Form-Token": token, "Content-Type": "application/json", "Origin": "https://other.invalid"}
        connection.request("POST", "/api/action", json.dumps({"action": "pause"}), headers)
        denied = connection.getresponse()
        denied.read()
        self.assertEqual(denied.status, 403)
        connection.request("GET", "/api/state", headers={"X-Form-Token": token})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["app"], "ExampleApp")


if __name__ == "__main__":
    unittest.main()
