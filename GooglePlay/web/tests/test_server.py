import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import (  # noqa: E402
    AuthManager,
    BATCH_SIZE,
    CommandRunner,
    command_environment,
    Config,
    JobService,
    JobStore,
    RequestError,
    build_catalog_command,
    load_app_profiles,
    is_transient_google_error,
    public_config,
    public_job,
    public_job_status,
    resolve_app_target,
    safe_credential_metadata,
    summarize_plan,
    source_workbook_format,
    workbook_preview,
    validate_service_account,
    validate_job_id,
    validate_upload,
)


class WebUploaderTest(unittest.TestCase):
    def test_command_runner_uses_external_jobs_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory) / "jobs"
            config = Config(
                root=Path("/repo/GooglePlay"),
                catalog_script=Path("/repo/GooglePlay/web/catalog/catalog.py"),
                catalog_python=Path(sys.executable),
                gplay_dir=Path("/repo/GooglePlay"),
                jobs_dir=jobs,
                host="127.0.0.1",
                port=8787,
            )
            with mock.patch("server.command_environment", return_value={}), mock.patch(
                "server.subprocess.run"
            ) as run:
                run.return_value = mock.Mock(returncode=0)
                CommandRunner(config).run(["gplay", "auth", "status"])

            self.assertEqual(run.call_args.kwargs["cwd"], jobs)

    def test_default_config_uses_bundled_catalog_and_shared_demo(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = Config.from_environment()

        project = Path(__file__).resolve().parents[2]
        self.assertEqual(config.catalog_script, project / "web" / "catalog" / "catalog.py")
        self.assertEqual(config.demo_workbook, project.parent / "demo.xlsx")
        self.assertEqual(config.apps_file, project.parent / "apps.json")
        self.assertEqual(
            config.jobs_dir,
            Path.home() / ".appstore_google_updata" / "GooglePlay" / "jobs",
        )
        config.validate()

    def test_command_environment_uses_macos_system_proxy_when_not_exported(self):
        scutil = mock.Mock(
            return_value=mock.Mock(
                returncode=0,
                stdout="""
                <dictionary> {
                  HTTPEnable : 1
                  HTTPPort : 7890
                  HTTPProxy : 127.0.0.1
                  HTTPSEnable : 1
                  HTTPSPort : 7890
                  HTTPSProxy : 127.0.0.1
                }
                """,
            )
        )

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "server.sys.platform", "darwin"
        ):
            environment = command_environment(run=scutil)

        self.assertEqual(environment["HTTP_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(environment["NO_PROXY"], "127.0.0.1,localhost,::1")
        scutil.assert_called_once()

    def test_command_environment_preserves_explicit_proxy(self):
        scutil = mock.Mock()
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://proxy.example:8080"},
            clear=True,
        ):
            environment = command_environment(run=scutil)

        self.assertEqual(environment["HTTPS_PROXY"], "http://proxy.example:8080")
        scutil.assert_not_called()

    def test_oauth_eof_is_a_transient_google_error(self):
        self.assertTrue(
            is_transient_google_error(
                "oauth2: cannot fetch token: Post https://oauth2.googleapis.com/token: EOF"
            )
        )

    def test_apps_mapping_contains_requested_mock_profiles(self):
        mapping = Path(__file__).resolve().parents[3] / "apps.json"

        self.assertEqual(
            load_app_profiles(mapping),
            {
                "DinReel": {"name": "dinreel", "pack": "com.dinereel.tv"},
                "MiniTV": {"name": "minitv", "pack": ""},
                "Epilora": {
                    "name": "epilora",
                    "pack": "com.epilora.drama.android",
                },
            },
        )

    def test_apps_mapping_normalizes_legacy_string_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "apps.json"
            mapping.write_text(
                json.dumps(
                    {
                        "Legacy": "legacy",
                        "Configured": {
                            "name": "configured",
                            "pack": "com.example.configured",
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_app_profiles(mapping),
                {
                    "Legacy": {"name": "legacy", "pack": ""},
                    "Configured": {
                        "name": "configured",
                        "pack": "com.example.configured",
                    },
                },
            )

    def test_app_target_rejects_profile_and_package_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "apps.json"
            mapping.write_text(
                json.dumps(
                    {
                        "DinReel": {
                            "name": "dinreel",
                            "pack": "com.dinereel.tv",
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                resolve_app_target(
                    mapping,
                    "DinReel",
                    profile="dinreel",
                    package="com.dinereel.tv",
                ),
                {
                    "app": "DinReel",
                    "name": "dinreel",
                    "pack": "com.dinereel.tv",
                },
            )
            with self.assertRaisesRegex(RequestError, "profile"):
                resolve_app_target(mapping, "DinReel", profile="wrong")
            with self.assertRaisesRegex(RequestError, "包名"):
                resolve_app_target(
                    mapping, "DinReel", package="com.example.wrong"
                )

    def test_config_preserves_virtualenv_python_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "catalog.py"
            script.touch()
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable))
            with mock.patch.dict(
                os.environ,
                {"GOOGLE_PLAY_CATALOG_SCRIPT": str(script)},
                clear=False,
            ):
                config = Config.from_environment()

            self.assertEqual(config.catalog_python.name, "python")
            self.assertEqual(config.catalog_python.parent.name, "bin")
            self.assertEqual(config.catalog_python.parent.parent.name, ".venv")

    def test_upload_accepts_only_real_xlsx_files(self):
        validate_upload("demo.xlsx", b"PK\x03\x04workbook", 1024)

        with self.assertRaisesRegex(RequestError, "xlsx"):
            validate_upload("demo.xls", b"PK\x03\x04workbook", 1024)
        with self.assertRaisesRegex(RequestError, "Excel"):
            validate_upload("demo.xlsx", b"not-a-zip", 1024)

    def test_upload_rejects_oversized_files(self):
        with self.assertRaisesRegex(RequestError, "20 MB"):
            validate_upload("demo.xlsx", b"PK\x03\x04", 20 * 1024 * 1024 + 1)

    def test_job_store_never_uses_uploaded_filename_as_a_path(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create(
                "../../service-account.json.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )

            job_dir = Path(directory) / job["id"]
            self.assertEqual((job_dir / "source.xlsx").read_bytes(), b"PK\x03\x04content")
            self.assertEqual(job["original_name"], "service-account.json.xlsx")
            self.assertFalse((Path(directory).parent / "service-account.json.xlsx").exists())

    def test_job_id_must_be_uuid(self):
        self.assertEqual(
            validate_job_id("123e4567-e89b-12d3-a456-426614174000"),
            "123e4567-e89b-12d3-a456-426614174000",
        )
        for value in ("../secret", "not-a-job", "123e4567e89b12d3a456426614174000"):
            with self.subTest(value=value), self.assertRaises(RequestError):
                validate_job_id(value)

    def test_apply_command_reuses_existing_plan_and_requires_exact_package(self):
        config = Config(
            root=Path("/repo"),
            catalog_script=Path("/uploader/catalog.py"),
            catalog_python=Path("/uploader/python"),
            gplay_dir=Path("/repo"),
            jobs_dir=Path("/repo/web/jobs"),
            host="127.0.0.1",
            port=8787,
        )
        command = build_catalog_command(
            config,
            action="apply",
            workbook=Path("/jobs/id/catalog.xlsx"),
            output_dir=Path("/jobs/id/apply-1"),
            package="com.example.app",
            profile="production",
            confirmation="com.example.app",
            allow_one_time_price_changes=False,
            input_plan=Path("/jobs/id/plan-1/plan.json"),
        )

        self.assertIn("--batch-size", command)
        self.assertEqual(command[command.index("--batch-size") + 1], str(BATCH_SIZE))
        self.assertEqual(BATCH_SIZE, 100)
        self.assertEqual(
            command[command.index("--input-plan") + 1],
            "/jobs/id/plan-1/plan.json",
        )
        self.assertEqual(command[command.index("--confirm-package") + 1], "com.example.app")
        with self.assertRaisesRegex(RequestError, "包名"):
            build_catalog_command(
                config,
                action="apply",
                workbook=Path("/jobs/id/catalog.xlsx"),
                output_dir=Path("/jobs/id/apply-2"),
                package="com.example.app",
                profile="production",
                confirmation="com.wrong.app",
                allow_one_time_price_changes=False,
            )

    def test_browser_config_contains_no_credentials_or_paths(self):
        config = Config(
            root=Path("/secret/repo"),
            catalog_script=Path("/secret/catalog.py"),
            catalog_python=Path("/secret/python"),
            gplay_dir=Path("/secret/gplay"),
            jobs_dir=Path("/secret/jobs"),
            host="127.0.0.1",
            port=8787,
        )

        payload = public_config(config, csrf_token="token")
        encoded = json.dumps(payload).lower()
        self.assertEqual(payload["batch_size"], 100)
        self.assertEqual(payload["csrf_token"], "token")
        self.assertTrue(payload["lightweight_job_status"])
        self.assertNotIn("default_package", payload)
        self.assertNotIn("default_profile", payload)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("credential", encoded)
        self.assertNotIn("service_account", encoded)

    def test_catalog_cli_requires_explicit_app(self):
        script = Path(__file__).resolve().parents[1] / "catalog" / "catalog.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **os.environ,
                "GPLAY_PACKAGE": "com.dinereel.tv",
                "GPLAY_PROFILE": "dinreel",
            }
            result = subprocess.run(
                [sys.executable, str(script), "template", "--output", str(Path(directory) / "demo.xlsx")],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("--package", result.stderr)
            self.assertFalse((Path(directory) / "demo.xlsx").exists())

    def test_service_account_upload_requires_google_key_shape(self):
        value = validate_service_account(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "example-project",
                    "private_key_id": "key-id",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n",
                    "client_email": "uploader@example-project.iam.gserviceaccount.com",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            ).encode("utf-8")
        )
        self.assertEqual(value["project_id"], "example-project")

        for payload in (b"not-json", b"{}", b'{"type":"authorized_user"}'):
            with self.subTest(payload=payload), self.assertRaises(RequestError):
                validate_service_account(payload)

    def test_credential_metadata_never_exposes_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "key.json"
            key_path.write_text(
                json.dumps(
                    {
                        "type": "service_account",
                        "project_id": "example-project",
                        "private_key_id": "visible-key-id",
                        "private_key": "-----BEGIN PRIVATE KEY-----\nnever-render-this\n-----END PRIVATE KEY-----\n",
                        "client_email": "uploader@example-project.iam.gserviceaccount.com",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                ),
                encoding="utf-8",
            )

            metadata = safe_credential_metadata("example-project", key_path)

            encoded = json.dumps(metadata)
            self.assertEqual(metadata["project_id"], "example-project")
            self.assertEqual(metadata["client_email"], "uploader@example-project.iam.gserviceaccount.com")
            self.assertNotIn("never-render-this", encoded)
            for technical_field in (
                "private_key",
                "private_key_id",
                "client_id",
                "key_path",
                "token_uri",
                "auth_uri",
                "client_x509_cert_url",
            ):
                self.assertNotIn(technical_field, encoded)

    def test_uploaded_credentials_are_stored_outside_the_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            credentials = Path(directory) / "runtime" / "credentials"
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
                credentials_dir=credentials,
            )
            runner = mock.Mock()
            auth = AuthManager(config, runner)
            raw = json.dumps(
                {
                    "type": "service_account",
                    "project_id": "epilora",
                    "private_key_id": "key-id",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n",
                    "client_email": "uploader@example-project.iam.gserviceaccount.com",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            ).encode("utf-8")

            with mock.patch.object(
                auth,
                "data",
                return_value={"profile": "epilora", "file_name": "epilora.json"},
            ):
                auth.install("epilora", raw)

            key_path = credentials / "epilora.json"
            self.assertTrue(key_path.is_file())
            self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
            command = runner.run.call_args.args[0]
            self.assertEqual(
                Path(command[command.index("--service-account") + 1]), key_path
            )
            self.assertFalse((root / "key" / "epilora.json").exists())

    def test_uploaded_credential_project_id_must_match_selected_app_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            credentials = Path(directory) / "runtime" / "credentials"
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
                credentials_dir=credentials,
            )
            runner = mock.Mock()
            auth = AuthManager(config, runner)
            raw = json.dumps(
                {
                    "type": "service_account",
                    "project_id": "another-project",
                    "private_key_id": "key-id",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n",
                    "client_email": "uploader@another-project.iam.gserviceaccount.com",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            ).encode("utf-8")

            with self.assertRaisesRegex(
                RequestError,
                "project_id 必须为 epilora",
            ):
                auth.install("epilora", raw)

            self.assertFalse((credentials / "epilora.json").exists())
            runner.run.assert_not_called()

    def test_demo_workbook_preview_contains_product_rows(self):
        demo = Path(__file__).resolve().parents[3] / "demo.xlsx"

        preview = workbook_preview(demo)

        self.assertEqual(preview["sheet"], "AppleParty")
        self.assertEqual(len(preview["rows"]), 3)
        self.assertIn("Product ID", preview["columns"])

        introductory = workbook_preview(
            demo, preferred_sheet="IntroductoryOffers"
        )
        self.assertEqual(introductory["sheet"], "IntroductoryOffers")
        self.assertGreaterEqual(len(introductory["rows"]), 1)

    def test_one_time_appleparty_workbook_does_not_require_introductory_offers(self):
        self.assertEqual(
            source_workbook_format({"AppleParty", "PricePoints", "Territories"}),
            "appleparty",
        )

    def test_long_workflow_tables_have_bounded_scroll_regions(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        css = (static / "styles.css").read_text(encoding="utf-8")

        self.assertGreaterEqual(html.count("table-scroll"), 6)
        self.assertIn(".table-scroll { max-height: 700px;", css)
        self.assertNotIn("max-height: 420px", css)
        self.assertNotIn("max-height: 230px", css)
        self.assertIn(".table-scroll .data-table th", css)

    def test_tables_show_consistent_useful_business_fields(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn("原价", html)
        self.assertIn("首周价", html)
        self.assertIn("本地校验", html)
        self.assertIn("groupCatalogRows", javascript)
        self.assertIn("job.preview?.offers?.rows", javascript)
        self.assertNotIn("<th>Profile</th>", html)

    def test_demo_is_clearly_labeled_as_example_only(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn("Excel 格式示例（不会上传）", html)
        self.assertIn("仅供格式参考", javascript)
        self.assertNotIn("Mock", html + javascript)

    def test_checking_view_explains_the_slow_google_work(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")
        page = html + javascript

        self.assertIn("商品与 Base plan", page)
        self.assertIn("首周优惠", page)
        self.assertIn("激活状态", page)
        self.assertIn("有效价格点", page)
        self.assertIn("逐项读取", page)

    def test_browser_persists_confirmation_and_guards_disruptive_switches(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("confirmationJobId", javascript)
        self.assertIn("confirmPackage", javascript)
        self.assertIn("saved.confirmationJobId === job.id", javascript)
        self.assertIn("localStorage.removeItem(PREVIOUS_STORAGE_KEY)", javascript)
        self.assertNotIn("appName: state.appName", javascript)
        self.assertNotIn("saved.appName", javascript)
        self.assertIn("confirmDisruptiveAction", javascript)
        self.assertIn("window.confirm", javascript)

    def test_recheck_locks_step_four_and_refreshes_stale_submit_state(self):
        static = Path(__file__).resolve().parents[1] / "static"
        css = (static / "styles.css").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn('state.job?.operation === "plan"', javascript)
        self.assertIn(
            "state.job = await api(`/api/jobs/${state.job.id}`)",
            javascript,
        )
        self.assertIn("height: 42px", css)
        self.assertIn("align-self: center", css)
        status_rule = next(
            line for line in css.splitlines() if line.startswith(".status-pill {")
        )
        self.assertIn("white-space: nowrap", status_rule)

    def test_restored_credential_shows_last_google_access_result(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'credential.api_access || (state.credentialChecked ? "通过" : "未检查")',
            javascript,
        )
        self.assertIn('"Google Play 访问"', javascript)

    def test_app_and_local_credential_can_be_selected_in_either_order(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("selectedCredentialFile: null", javascript)
        self.assertIn("async function processCredentialSelection()", javascript)
        self.assertIn("if (!file || !appName || !profile) {", javascript)
        self.assertIn("state.selectedCredentialFile = file", javascript)
        self.assertGreaterEqual(
            javascript.count("await processCredentialSelection()"),
            2,
        )
        self.assertNotIn("loadConfiguredCredential", javascript)
        self.assertNotIn("/api/auth/data?profile=", javascript)
        self.assertNotIn("credentialReady: state.credentialReady", javascript)

    def test_browser_uses_app_profile_and_package_configuration(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn('state.apps[nextApp]?.name', javascript)
        self.assertIn('state.apps[nextApp]?.pack', javascript)
        self.assertIn('form.set("app", appName)', javascript)
        self.assertIn('form.set("file", file)', javascript)
        self.assertIn('id="package-input"', html)
        self.assertIn("readonly", html)

    def test_browser_rejects_a_credential_with_another_apps_project_id(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "async function validateServiceAccountFile(file, expectedProjectId)",
            javascript,
        )
        self.assertIn("JSON.parse(await file.text())", javascript)
        self.assertIn("credential.project_id !== expectedProjectId", javascript)
        self.assertIn(
            "await validateServiceAccountFile(file, profile)",
            javascript,
        )

    def test_submit_package_confirmation_does_not_reveal_or_autofill_package(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn("<span>请输入包名</span>", html)
        self.assertIn('placeholder="请输入包名"', html)
        self.assertNotIn("el.confirmPackageInput.placeholder = job.package", javascript)
        self.assertNotIn("isSubmitOperation() ? job.package", javascript)
        self.assertNotIn("isSubmitOperation() ? state.job.package", javascript)
        self.assertNotIn("页面显示的完整包名", javascript)

    def test_submit_ui_starts_the_automatic_apply_workflow(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn("自动提交到 Google Play", html)
        self.assertIn("自动提交全部", javascript)
        self.assertIn("/apply-all", javascript)
        self.assertNotIn("每批最多 10", html + javascript)
        self.assertIn("已成功 <span id=\"submit-successful\">0</span> 项", html)

    def test_render_skips_hidden_workflow_tables(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("if (state.currentStep === 0) renderCredential();", javascript)
        self.assertIn("if (state.currentStep === 1) {", javascript)
        self.assertIn("if (state.currentStep === 2) renderPlan();", javascript)
        self.assertIn("if (state.currentStep === 3) renderSubmit();", javascript)
        self.assertNotIn("renderCredential();\n    renderDemo();", javascript)

    def test_submit_ui_uses_server_batch_state_instead_of_guessing_rows(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("job.active_batch", javascript)
        self.assertIn("正在写入 Google", javascript)
        self.assertIn("结果不确定，正在自动核对", javascript)
        self.assertNotIn("已提交，正在确认", javascript)
        self.assertNotIn("activeBatchSlots", javascript)

    def test_submit_ui_groups_original_and_introductory_price_results(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn("提交结果日志", html)
        self.assertIn("submit-log-table", html)
        self.assertIn("groupSubmissionItems", javascript)
        self.assertIn("原价上传成功", javascript)
        self.assertIn("首周价上传成功", javascript)
        self.assertIn(
            'job.status === "verified" ? groupSubmissionItems(plan.items).length : applied',
            javascript,
        )

    def test_new_uploads_do_not_delete_finished_submission_logs(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertNotIn("oldJob.id}/clear", javascript)
        self.assertNotIn("Replacing credentials still resets", javascript)
        self.assertIn("Excel", javascript)
        self.assertIn("Google 有效价", javascript)

    def test_summarized_plan_preserves_requested_and_effective_prices(self):
        summary = summarize_plan(
            {
                "counts": {"noop": 2},
                "items": [
                    {
                        "product_type": "subscription",
                        "product_id": "weekly_49_69",
                        "action": "noop",
                        "activate_required": False,
                        "changes": [
                            "Google price point maps workbook price 69.90 to 69.99"
                        ],
                        "base_price": {
                            "currencyCode": "USD",
                            "units": "69",
                            "nanos": 900000000,
                        },
                    }
                ],
                "offers": [
                    {
                        "product_id": "weekly_49_69",
                        "base_plan_id": "weekly",
                        "offer_id": "weekly-first",
                        "action": "noop",
                        "activate_required": False,
                        "changes": [],
                        "base_price": {
                            "currencyCode": "USD",
                            "units": "49",
                            "nanos": 900000000,
                        },
                    }
                ],
            }
        )

        self.assertEqual(summary["items"][0]["requested_price"], "69.90")
        self.assertEqual(summary["items"][0]["effective_price"], "69.99")
        self.assertEqual(summary["items"][0]["currency_code"], "USD")
        self.assertEqual(summary["items"][1]["requested_price"], "49.90")
        self.assertEqual(summary["items"][1]["effective_price"], "49.90")

    def test_navigation_explains_background_work_and_required_next_action(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("步骤 4 正在自动提交", javascript)
        self.assertIn("查看提交进度", javascript)
        self.assertIn("请先在步骤 2 上传并校验 Excel", javascript)
        self.assertIn("请返回步骤 3 点击“重新检查线上状态”", javascript)
        self.assertIn("el.runPlanButton.disabled = jobRunning", javascript)

    def test_polling_uses_lightweight_job_status(self):
        javascript = (
            Path(__file__).resolve().parents[1] / "static" / "app.js"
        ).read_text(encoding="utf-8")
        job = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "original_name": "large.xlsx",
            "package": "com.example.app",
            "profile": "production",
            "status": "running",
            "stage": "正在提交",
            "operation": "apply_all",
            "error": None,
            "validation": {"large": "x" * 100_000},
            "preview": {"large": "x" * 100_000},
            "plan_revision": 7,
            "plan": {
                "total": 312,
                "actionable": 164,
                "blocked": 0,
                "items": [{"large": "x" * 100_000}],
            },
            "active_batch": {"number": 2, "phase": "applying", "items": []},
            "apply_runs": 1,
            "applied_total": 10,
            "updated_at": "2026-08-28T00:00:00+00:00",
        }

        payload = public_job_status(job)

        self.assertNotIn("validation", payload)
        self.assertNotIn("preview", payload)
        self.assertNotIn("items", payload["plan"])
        self.assertEqual(payload["plan_revision"], 7)
        self.assertLess(len(json.dumps(payload)), 3000)
        self.assertIn("/status", javascript)
        self.assertNotIn("}, 1300);", javascript)

    def test_clear_removes_only_finished_local_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(job["id"], status="ready")

            store.clear(job["id"])

            with self.assertRaisesRegex(RequestError, "不存在"):
                store.get(job["id"])

    def test_clear_refuses_to_remove_running_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(job["id"], status="running", operation="plan")

            with self.assertRaisesRegex(RequestError, "运行"):
                store.clear(job["id"])

    def test_starting_recheck_immediately_clears_stale_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="ready",
                validation={"valid": True},
                plan={"total": 114, "blocked": 0, "actionable": 114, "items": []},
                plan_file="plan-1/plan.json",
            )
            entered = threading.Event()
            release = threading.Event()

            def hold_recheck(_job):
                entered.set()
                release.wait(timeout=2)

            service = JobService(config, store, mock.Mock())
            with mock.patch.object(service, "_plan", side_effect=hold_recheck):
                service.start_plan(job["id"])
                self.assertTrue(entered.wait(timeout=1))
                checking = store.get(job["id"])
                self.assertEqual(checking["operation"], "plan")
                self.assertIsNone(checking["plan"])
                self.assertIsNone(checking["plan_file"])
                self.assertIsNone(checking["active_batch"])
                self.assertGreater(checking["plan_revision"], 1)
                release.set()
                for _ in range(100):
                    if store.get(job["id"])["operation"] is None:
                        break
                    time.sleep(0.01)
                self.assertIsNone(store.get(job["id"])["operation"])

    def test_interrupted_automatic_sync_is_queued_for_safe_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(job["id"], status="running", operation="apply_all")

            resumable = store.recover_interrupted()

            updated = store.get(job["id"])
            self.assertEqual(resumable, [job["id"]])
            self.assertEqual(updated["status"], "uncertain")
            self.assertEqual(updated["resume_operation"], "apply_all")
            self.assertIsNone(updated["operation"])

    def test_clean_legacy_plan_is_promoted_to_verified_on_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="ready",
                operation=None,
                plan={"blocked": 0, "actionable": 0, "items": []},
            )

            store.recover_interrupted()

            updated = store.get(job["id"])
            self.assertEqual(updated["status"], "verified")
            self.assertEqual(updated["stage"], "核对通过，Excel 与 Google Play 已一致")

    def test_interrupted_automatic_sync_rechecks_then_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="uncertain",
                operation=None,
                resume_operation="apply_all",
                plan={"blocked": 0, "actionable": 3, "items": []},
            )
            service = JobService(config, store, mock.Mock())

            def reconcile(current):
                store.update(
                    current["id"],
                    status="ready",
                    plan={"blocked": 0, "actionable": 2, "items": []},
                )

            with (
                mock.patch.object(service, "_start") as start,
                mock.patch.object(service, "_plan", side_effect=reconcile) as plan_call,
                mock.patch.object(service, "_apply_all") as apply_all,
            ):
                service.resume_interrupted_apply_all(job["id"])
                callback = start.call_args.args[2]
                callback(store.get(job["id"]))

            plan_call.assert_called_once()
            apply_all.assert_called_once()
            self.assertEqual(apply_all.call_args.args[1], "com.example.app")
            self.assertIsNone(store.get(job["id"])["resume_operation"])

    def test_apply_continues_old_job_with_null_progress(self):
        class FakeRunner:
            def run(self, command, accepted=(0,)):
                output_dir = Path(command[command.index("--output-dir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                plan = {
                    "counts": {"create": 12},
                    "items": [
                        {
                            "product_type": "one_time",
                            "product_id": f"coins_{index}",
                            "action": "create",
                            "activate_required": True,
                            "changes": ["product does not exist"],
                        }
                        for index in range(12)
                    ],
                    "offers": [],
                }
                (output_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
                for item in plan["items"]:
                    item["action"] = "noop"
                    item["activate_required"] = False
                (output_dir / "remaining-plan.json").write_text(
                    json.dumps(plan), encoding="utf-8"
                )
                return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            job_dir = config.jobs_dir / job["id"]
            (job_dir / "catalog.xlsx").write_bytes(b"catalog")
            job = store.update(
                job["id"],
                status="ready",
                plan={"blocked": 0, "actionable": 12, "items": []},
                applied_total=None,
            )
            service = JobService(config, store, FakeRunner())

            with mock.patch.object(service, "_plan") as refresh:
                service._apply(job, "com.example.app")

            updated = store.get(job["id"])
            self.assertEqual(updated["applied_total"], 12)
            self.assertEqual(updated["apply_runs"], 1)
            self.assertEqual(updated["plan"]["actionable"], 0)
            refresh.assert_not_called()

    def test_apply_reuses_plan_and_does_not_rescan_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            items = [
                {
                    "kind": "one_time",
                    "product_id": f"coins_{index}",
                    "base_plan_id": "",
                    "offer_id": "",
                    "action": "create",
                    "activate_required": True,
                    "changes": ["product does not exist"],
                }
                for index in range(12)
            ]
            plan_directory = job_dir = config.jobs_dir / job["id"] / "plan-1"
            plan_directory.mkdir(parents=True)
            input_plan = plan_directory / "plan.json"
            input_plan.write_text("{}", encoding="utf-8")
            store.update(
                job["id"],
                status="ready",
                plan={"blocked": 0, "actionable": 12, "items": items},
                plan_file="plan-1/plan.json",
            )
            observed = []
            commands = []

            class FakeRunner:
                def run(self, command, accepted=(0,)):
                    observed.append(store.get(job["id"])["active_batch"])
                    commands.append(command)
                    output_dir = Path(command[command.index("--output-dir") + 1])
                    output_dir.mkdir(parents=True, exist_ok=True)
                    plan = {
                        "counts": {"create": 12},
                        "items": [
                            {
                                "product_type": "one_time",
                                "product_id": f"coins_{index}",
                                "action": "create",
                                "activate_required": True,
                                "changes": ["product does not exist"],
                            }
                            for index in range(12)
                        ],
                        "offers": [],
                    }
                    (output_dir / "plan.json").write_text(
                        json.dumps(plan), encoding="utf-8"
                    )
                    for item in plan["items"]:
                        item["action"] = "noop"
                        item["activate_required"] = False
                    (output_dir / "remaining-plan.json").write_text(
                        json.dumps(plan), encoding="utf-8"
                    )
                    return mock.Mock(returncode=0, stdout="", stderr="")

            service = JobService(config, store, FakeRunner())

            with mock.patch.object(service, "_plan") as refresh:
                service._apply(store.get(job["id"]), "com.example.app")

            self.assertEqual(observed[0]["phase"], "applying")
            self.assertEqual(observed[0]["number"], 1)
            self.assertEqual(
                [item["product_id"] for item in observed[0]["items"]],
                [f"coins_{index}" for index in range(12)],
            )
            updated = store.get(job["id"])
            self.assertIsNone(updated["active_batch"])
            self.assertEqual(updated["plan"]["actionable"], 0)
            self.assertTrue(updated["plan_file"].endswith("remaining-plan.json"))
            self.assertEqual(
                Path(commands[0][commands[0].index("--input-plan") + 1]),
                input_plan.resolve(),
            )
            refresh.assert_not_called()
            self.assertEqual(
                public_job({"id": job["id"], "active_batch": observed[0]})[
                    "active_batch"
                ],
                observed[0],
            )

    def test_saved_plan_path_recovers_a_matching_legacy_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            raw_plan = {
                "counts": {"create": 1},
                "items": [
                    {
                        "product_type": "one_time",
                        "product_id": "coins_1",
                        "action": "create",
                        "activate_required": True,
                        "changes": [],
                    }
                ],
                "offers": [],
            }
            plan_dir = config.jobs_dir / job["id"] / "plan-1"
            plan_dir.mkdir()
            plan_path = plan_dir / "plan.json"
            plan_path.write_text(json.dumps(raw_plan), encoding="utf-8")
            job = store.update(
                job["id"], status="ready", plan=summarize_plan(raw_plan)
            )

            service = JobService(config, store, mock.Mock())

            self.assertEqual(service._saved_plan_path(job), plan_path.resolve())

    def test_clean_read_only_plan_finishes_without_a_duplicate_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            job_dir = config.jobs_dir / job["id"]
            (job_dir / "catalog.xlsx").write_bytes(b"catalog")

            class FakeRunner:
                def run(self, command, accepted=(0,)):
                    output_dir = Path(command[command.index("--output-dir") + 1])
                    output_dir.mkdir(parents=True, exist_ok=True)
                    (output_dir / "plan.json").write_text(
                        json.dumps(
                            {"counts": {"noop": 1}, "items": [], "offers": []}
                        ),
                        encoding="utf-8",
                    )
                    return mock.Mock(returncode=0, stdout="", stderr="")

            service = JobService(config, store, FakeRunner())
            service._plan(job)

            updated = store.get(job["id"])
            self.assertEqual(updated["status"], "verified")
            self.assertEqual(updated["stage"], "核对通过，Excel 与 Google Play 已一致")

    def test_apply_all_runs_every_batch_and_then_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="ready",
                plan={"blocked": 0, "actionable": 25, "items": []},
            )
            service = JobService(config, store, mock.Mock())
            batches = []

            def apply_batch(current, confirmation):
                remaining = current["plan"]["actionable"]
                selected = min(BATCH_SIZE, remaining)
                batches.append((remaining, confirmation))
                store.update(
                    current["id"],
                    status="ready",
                    apply_runs=int(current.get("apply_runs") or 0) + 1,
                    applied_total=int(current.get("applied_total") or 0) + selected,
                    plan={
                        "blocked": 0,
                        "actionable": remaining - selected,
                        "items": [],
                    },
                )

            def verify(current):
                store.update(current["id"], status="verified", stage="验证通过")

            with (
                mock.patch.object(service, "_apply", side_effect=apply_batch),
                mock.patch.object(service, "_verify", side_effect=verify) as verify_call,
            ):
                service._apply_all(store.get(job["id"]), "com.example.app")

            self.assertEqual(
                batches,
                [
                    (25, "com.example.app"),
                ],
            )
            self.assertEqual(store.get(job["id"])["status"], "verified")
            verify_call.assert_called_once()

    def test_apply_all_reconciles_and_retries_transient_google_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="ready",
                plan={"blocked": 0, "actionable": 10, "items": []},
            )
            service = JobService(config, store, mock.Mock())
            attempts = []
            recovery_stages = []

            def apply_batch(current, _confirmation):
                attempts.append(current["plan"]["actionable"])
                if len(attempts) == 1:
                    raise RuntimeError(
                        "googleapi: Error 503: The service is currently unavailable., backendError"
                    )
                store.update(
                    current["id"],
                    status="ready",
                    plan={"blocked": 0, "actionable": 0, "items": []},
                )

            def reconcile(current):
                recovery_stages.append(store.get(current["id"])["stage"])
                store.update(
                    current["id"],
                    status="ready",
                    plan={"blocked": 0, "actionable": 10, "items": []},
                    active_batch=None,
                )

            def verify(current):
                store.update(current["id"], status="verified", stage="验证通过")

            with (
                mock.patch.object(service, "_apply", side_effect=apply_batch),
                mock.patch.object(service, "_plan", side_effect=reconcile) as plan_call,
                mock.patch.object(service, "_verify", side_effect=verify),
                mock.patch("server.time.sleep") as sleep,
            ):
                service._apply_all(store.get(job["id"]), "com.example.app")

            self.assertEqual(attempts, [10, 10])
            plan_call.assert_called_once()
            sleep.assert_called_once_with(2)
            self.assertIn("自动恢复 1/5", recovery_stages[0])
            self.assertEqual(store.get(job["id"])["status"], "verified")

    def test_apply_all_does_not_retry_non_transient_write_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="ready",
                plan={"blocked": 0, "actionable": 10, "items": []},
            )
            service = JobService(config, store, mock.Mock())

            with (
                mock.patch.object(
                    service,
                    "_apply",
                    side_effect=RuntimeError("googleapi: Error 400: invalid request"),
                ),
                mock.patch.object(service, "_plan") as plan_call,
                mock.patch("server.time.sleep") as sleep,
            ):
                with self.assertRaisesRegex(RuntimeError, "Error 400"):
                    service._apply_all(store.get(job["id"]), "com.example.app")

            plan_call.assert_not_called()
            sleep.assert_not_called()

    def test_apply_all_limits_consecutive_transient_recovery_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="ready",
                plan={"blocked": 0, "actionable": 10, "items": []},
            )
            service = JobService(config, store, mock.Mock())
            transient = RuntimeError("googleapi: Error 503: backendError")

            with (
                mock.patch.object(service, "_apply", side_effect=transient),
                mock.patch.object(service, "_plan", side_effect=transient) as plan_call,
                mock.patch("server.time.sleep") as sleep,
            ):
                with self.assertRaisesRegex(RuntimeError, "已自动恢复 5 次"):
                    service._apply_all(store.get(job["id"]), "com.example.app")

            self.assertEqual(plan_call.call_count, 5)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [2, 4, 8, 15, 30],
            )

    def test_apply_all_stops_when_a_batch_makes_no_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            store.update(
                job["id"],
                status="ready",
                plan={"blocked": 0, "actionable": 10, "items": []},
            )
            service = JobService(config, store, mock.Mock())

            with mock.patch.object(service, "_apply"):
                with self.assertRaisesRegex(RuntimeError, "没有减少"):
                    service._apply_all(store.get(job["id"]), "com.example.app")

    def test_final_verify_requires_a_clean_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                root=root,
                catalog_script=root / "catalog.py",
                catalog_python=Path(sys.executable),
                gplay_dir=root,
                jobs_dir=root / "jobs",
                host="127.0.0.1",
                port=8787,
            )
            store = JobStore(config.jobs_dir)
            job = store.create(
                "demo.xlsx",
                io.BytesIO(b"PK\x03\x04content"),
                package="com.example.app",
                profile="production",
            )
            service = JobService(config, store, mock.Mock())

            for plan in (
                {"blocked": 1, "actionable": 1, "items": []},
                {"blocked": 0, "actionable": 1, "items": []},
            ):
                store.update(job["id"], status="ready", plan=plan)
                with self.subTest(plan=plan), mock.patch.object(service, "_start") as start:
                    with self.assertRaises(RequestError):
                        service.start_verify(job["id"])
                    start.assert_not_called()

    def test_credential_check_marks_exhausted_transient_errors_as_temporary(self):
        config = Config(
            root=Path("/repo"),
            catalog_script=Path("/repo/catalog.py"),
            catalog_python=Path(sys.executable),
            gplay_dir=Path("/repo"),
            jobs_dir=Path("/repo/jobs"),
            host="127.0.0.1",
            port=8787,
        )
        auth = AuthManager(config, mock.Mock())
        failure = mock.Mock(returncode=1, stdout="", stderr="Error 429: rate limited")

        with (
            mock.patch.object(auth, "data", return_value={"profile": "production"}),
            mock.patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://proxy.example:8080"},
                clear=True,
            ),
            mock.patch("server.subprocess.run", return_value=failure) as run,
            mock.patch("server.time.sleep") as sleep,
        ):
            result = auth.check("production", "com.example.app")

        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["api_access"], "暂时无法验证")


if __name__ == "__main__":
    unittest.main()
