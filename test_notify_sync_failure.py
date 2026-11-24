import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import notify_sync_failure as alert


def sync_job(conclusion, connector=None, extra=None):
    steps = [{"name": "Set up job", "conclusion": "success"}]
    if connector is not None:
        steps.append({"name": "Run connector", "conclusion": connector})
    if extra is not None:
        steps.append({"name": "Drain digest queue", "conclusion": extra})
    return [{"name": "sync", "conclusion": conclusion, "steps": steps}]


class AlertTest(unittest.TestCase):
    def test_classifies_exchange_setup_and_late_failure(self):
        self.assertEqual(alert.classify_sync(sync_job("failure", "failure")), "обмен")
        self.assertEqual(alert.classify_sync(sync_job("cancelled", "cancelled")), "обмен")
        self.assertEqual(alert.classify_sync(sync_job("timed_out", "timed_out")), "обмен")
        self.assertEqual(alert.classify_sync(sync_job("failure")), "подготовка")
        self.assertEqual(alert.classify_sync(sync_job("failure", "success", "failure")),
                         "после обмена")

    def test_ignores_success_other_jobs_and_pending_cancellation(self):
        self.assertIsNone(alert.classify_sync(sync_job("success", "success")))
        self.assertIsNone(alert.classify_sync([{"name": "ask", "conclusion": "failure",
                                                "steps": [{"conclusion": "failure"}]}]))
        self.assertIsNone(alert.classify_sync([{"name": "sync", "conclusion": "cancelled",
                                                "steps": []}]))

    def test_only_trusted_main_can_alert(self):
        source = {
            "ALERT_EVENT": "workflow_run", "ALERT_SOURCE_REPO": alert.REPO,
            "ALERT_HEAD_REPO": alert.REPO, "ALERT_HEAD_BRANCH": "main",
            "ALERT_WORKFLOW_NAME": alert.WORKFLOW,
            "ALERT_WORKFLOW_PATH": alert.WORKFLOW_PATH,
        }
        self.assertTrue(alert.trusted_source(source))
        self.assertFalse(alert.trusted_source({**source, "ALERT_HEAD_BRANCH": "diagnose"}))
        self.assertFalse(alert.trusted_source({**source, "ALERT_HEAD_REPO": "other/repo"}))
        self.assertTrue(alert.trusted_source({
            "ALERT_EVENT": "workflow_dispatch", "GITHUB_REPOSITORY": alert.REPO,
            "GITHUB_REF": "refs/heads/main"}))
        self.assertFalse(alert.trusted_source({
            "ALERT_EVENT": "workflow_dispatch", "GITHUB_REPOSITORY": alert.REPO,
            "GITHUB_REF": "refs/heads/diagnose"}))

    def test_classify_uses_specific_attempt_and_writes_only_controlled_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            env = {
                "ALERT_EVENT": "workflow_run", "ALERT_SOURCE_REPO": alert.REPO,
                "ALERT_HEAD_REPO": alert.REPO, "ALERT_HEAD_BRANCH": "main",
                "ALERT_WORKFLOW_NAME": alert.WORKFLOW,
                "ALERT_WORKFLOW_PATH": alert.WORKFLOW_PATH,
                "ALERT_RUN_CONCLUSION": "failure", "ALERT_RUN_ID": "123",
                "ALERT_RUN_ATTEMPT": "2", "GITHUB_TOKEN": "fake",
                "GITHUB_OUTPUT": str(output),
            }
            with patch.dict("os.environ", env, clear=True), \
                 patch.object(alert, "fetch_attempt_jobs",
                              return_value=sync_job("failure", "failure")) as fetch:
                alert.classify()
            fetch.assert_called_once_with(123, 2, "fake")
            self.assertEqual(output.read_text(encoding="utf-8"),
                             "send=true\nphase=обмен\n")

    def test_send_posts_only_to_fixed_portal_and_chat(self):
        webhook = "https://anit.bitrix24.ru/rest/1/exampletoken/"
        with patch.object(alert.urllib.request, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = (
                b'{"result":321}')
            message_id = alert.send_message(webhook, "Test alert")
            req = build.return_value.open.call_args.args[0]
        self.assertEqual(message_id, 321)
        self.assertEqual(req.full_url, webhook + "im.message.add.json")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"DIALOG_ID": "chat37500", "MESSAGE": "Test alert"})
        with self.assertRaises(ValueError):
            alert.send_message("https://evil.example/rest/1/token/", "Test alert")

    def test_message_identifies_attempt_without_business_data(self):
        msg = alert.message_text("обмен", 123, 2, False)
        self.assertIn("#123", msg)
        self.assertIn("попытка 2", msg)
        self.assertIn("Ошибка обмена", msg)


if __name__ == "__main__":
    unittest.main()
