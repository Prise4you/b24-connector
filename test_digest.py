import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import connector
import load_notebooklm


class DigestTest(unittest.TestCase):
    def run_digest(self, projects, group=None, answers=None):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"projects": projects, "output_dir": tmp}), encoding="utf-8")
            argv = ["connector.py", "--config", str(cfg), "--digest"]
            if group is not None:
                argv += ["--group", str(group)]
            with patch.object(sys, "argv", argv), patch.object(
                load_notebooklm, "ask_notebook", side_effect=answers
            ) as ask:
                try:
                    connector.main()
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
            payload = json.loads((Path(tmp) / "digest_payload.json").read_text())
            return error, payload, ask.call_args_list

    def test_monthly_keeps_successes_and_reports_failures(self):
        projects = [
            {"group_id": i, "notebook_id": f"nb-{i}", "notebook_name": f"Project {i}",
             "digest": {"enabled": True, "question": "Report {month}"}}
            for i in (1, 2)
        ]
        error, payload, calls = self.run_digest(
            projects, answers=[{"answer": "Done"}, TimeoutError("slow")]
        )
        self.assertIn("group_id: 2", error)
        self.assertEqual([x["group_id"] for x in payload["digests"]], [1])
        self.assertEqual(payload["overviews"], [])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.kwargs["timeout"] == 360 for c in calls))

    def test_group_onboarding_includes_overview(self):
        project = {"group_id": 3, "notebook_id": "nb-3", "notebook_name": "Project 3",
                   "digest": {"enabled": True, "question": "Report {month}"}}
        error, payload, calls = self.run_digest(
            [project], group=3, answers=[{"answer": "Month"}, {"answer": "Overview"}]
        )
        self.assertIsNone(error)
        self.assertEqual(len(payload["digests"]), 1)
        self.assertEqual(len(payload["overviews"]), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.kwargs["timeout"] == 360 for c in calls))


if __name__ == "__main__":
    unittest.main()
