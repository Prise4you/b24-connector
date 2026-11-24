import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import connector


class SyncExitTest(unittest.TestCase):
    def run_sync(self, *, group, fail_project=False, dry_run=False,
                 status_post_ok=True):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "webhook_url": "https://example.test/webhook/",
                "projects": [{"group_id": 624, "notebook_name": "Test"}],
                "targets": {"notebooklm": {"enabled": False}},
                "include": {},
                "state_file": str(Path(tmp) / "state.json"),
            }
            argv = [
                "connector.py", "--status-url", "https://example.test/status",
                "--connector-token", "test-token", "--skip-admin-check",
                "--group", str(group),
            ]
            if dry_run:
                argv.append("--dry-run")
            output = io.StringIO()
            with patch.object(sys, "argv", argv), \
                 patch.object(connector, "load_config", return_value=cfg), \
                 patch.object(connector.security, "validate_config", return_value=[]), \
                 patch.object(connector.security, "harden_config_file"), \
                 patch.object(connector.security, "audit_run_start"), \
                 patch.object(connector, "B24Client") as client_class, \
                 patch.object(connector, "post_run_status") as post_status, \
                 patch.object(connector, "_get_group_name") as get_group_name, \
                 contextlib.redirect_stdout(output):
                client_class.return_value.call.return_value = {"NAME": "Test"}
                post_status.return_value = status_post_ok
                if fail_project:
                    get_group_name.side_effect = RuntimeError("project failed")
                try:
                    connector.main()
                    exit_code = 0
                except SystemExit as exc:
                    exit_code = exc.code
                payload = post_status.call_args.args[2]
            return exit_code, payload, output.getvalue(), post_status.call_count

    def test_partial_status_causes_failure_after_single_status_post(self):
        code, payload, output, calls = self.run_sync(group=624, fail_project=True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(calls, 1)
        self.assertNotIn("Всё готово", output)

    def test_clean_sync_stays_successful(self):
        code, payload, output, calls = self.run_sync(group=999)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(calls, 1)
        self.assertIn("Всё готово", output)

    def test_dry_run_keeps_existing_exit_behavior(self):
        code, payload, output, calls = self.run_sync(
            group=624, fail_project=True, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(calls, 1)
        self.assertIn("[dry-run] Готово", output)

    def test_failed_status_delivery_marks_sync_failed(self):
        code, payload, output, calls = self.run_sync(
            group=999, status_post_ok=False)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(calls, 1)
        self.assertNotIn("Всё готово", output)

    def test_crm_upload_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "webhook_url": "https://example.test/webhook/",
                "projects": [{"group_id": 624, "notebook_name": "Test"}],
                "targets": {"notebooklm": {"enabled": True}},
                "include": {"crm": {"enabled": True, "company_ids": [1]}},
                "state_file": str(Path(tmp) / "state.json"),
                "output_dir": tmp,
            }
            argv = ["connector.py", "--status-url", "https://example.test/status",
                    "--connector-token", "test-token", "--skip-admin-check",
                    "--crm-only"]
            output = io.StringIO()
            with patch.object(sys, "argv", argv), \
                 patch.object(connector, "load_config", return_value=cfg), \
                 patch.object(connector.security, "validate_config", return_value=[]), \
                 patch.object(connector.security, "harden_config_file"), \
                 patch.object(connector.security, "audit_run_start"), \
                 patch.object(connector.security, "audit_run_end"), \
                 patch.object(connector, "B24Client") as client_class, \
                 patch.object(connector, "extract_crm_companies", return_value=[{"ID": "1"}]), \
                 patch.object(connector, "build_crm_companies_doc", return_value="doc"), \
                 patch.object(connector, "_redact_and_save", return_value=({"crm.md": "doc"}, 0)), \
                 patch.object(connector, "post_run_status", return_value=True) as post_status, \
                 patch("load_notebooklm.load_to_notebooklm", side_effect=RuntimeError("upload failed")), \
                 patch("load_notebooklm.session_status", return_value={"ok": True}), \
                 patch("load_notebooklm.project_notebooks_snapshot", return_value=[]), \
                 contextlib.redirect_stdout(output):
                client_class.return_value.call.return_value = {"NAME": "Test"}
                with self.assertRaises(SystemExit) as exc:
                    connector.main()
            self.assertEqual(exc.exception.code, 1)
            payload = post_status.call_args.args[2]
            self.assertEqual(payload["status"], "partial")
            self.assertEqual(len(payload["partial_errors"]), 1)
            self.assertEqual(payload["partial_errors"][0]["name"], "CRM АНИТ")

    def test_status_post_requires_server_ack(self):
        with patch.object(connector.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"ok":false}'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(connector.post_run_status(
                    "https://example.test/status", "test-token", {"status": "ok"}))
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
            self.assertTrue(connector.post_run_status(
                "https://example.test/status", "test-token", {"status": "ok"}))


if __name__ == "__main__":
    unittest.main()
