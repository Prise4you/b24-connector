import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import connector
import load_notebooklm


class SessionRestoreTest(unittest.TestCase):
    def test_expired_remote_session_keeps_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "storage_state.json"
            storage.write_bytes(b"fresh-seed")
            with patch.object(load_notebooklm, "DEFAULT_STORAGE", str(storage)), patch.object(
                connector, "fetch_remote_nlm_session", return_value=base64.b64encode(b"expired").decode()
            ), patch.object(load_notebooklm, "session_status", return_value={"ok": False}):
                self.assertFalse(connector.restore_nlm_session("https://example.test", "token"))
            self.assertEqual(storage.read_bytes(), b"fresh-seed")

    def test_live_remote_session_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "storage_state.json"
            storage.write_bytes(b"old-seed")
            with patch.object(load_notebooklm, "DEFAULT_STORAGE", str(storage)), patch.object(
                connector, "fetch_remote_nlm_session", return_value=base64.b64encode(b"live").decode()
            ), patch.object(load_notebooklm, "session_status", return_value={"ok": True}):
                self.assertTrue(connector.restore_nlm_session("https://example.test", "token"))
            self.assertEqual(storage.read_bytes(), b"live")


if __name__ == "__main__":
    unittest.main()
