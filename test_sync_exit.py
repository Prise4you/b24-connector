import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import connector


class SyncExitTest(unittest.TestCase):
    def run_sync(self, *, group, fail_project=False, dry_run=False):
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


if __name__ == "__main__":
    unittest.main()
