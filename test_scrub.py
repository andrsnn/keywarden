"""Transcript cleanup checks: same-length writes, JSON escaping, live appends, and late creation."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import keys
import prompt_hook


class ScrubTests(unittest.TestCase):
    def test_unicode_escaped_and_multiline_json_remains_valid(self):
        values = ['demo_Aq9v7"\\résumé123', '-----BEGIN PRIVATE KEY-----\nQ8r5T7u2i9o4P6s3a0D1f8G7\n-----END PRIVATE KEY-----']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            for ascii_only in (True, False):
                with self.subTest(ascii_only=ascii_only):
                    original = "\n".join(json.dumps({"prompt": v, "other": "keep this"}, ensure_ascii=ascii_only) for v in values) + "\n"
                    path.write_text(original)
                    size = path.stat().st_size
                    self.assertGreater(keys.scrub_in_place(path, keys.scrub_values(values)), 0)
                    self.assertEqual(path.stat().st_size, size)
                    text = path.read_text()
                    for line in text.splitlines():
                        obj = json.loads(line)
                        self.assertEqual(obj["other"], "keep this")
                    for v in values:
                        self.assertNotIn(v, text)
                        self.assertNotIn(json.dumps(v)[1:-1], text)

    def test_append_descriptor_still_writes_to_same_transcript(self):
        value = "demo_Aq9v7B2m4K6p8H0t1"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text(json.dumps({"prompt": value}) + "\n")
            inode = path.stat().st_ino
            with path.open("a") as appender:
                keys.scrub_in_place(path, keys.scrub_values([value]))
                appender.write(json.dumps({"continued": True}) + "\n")
                appender.flush()
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(json.loads(path.read_text().splitlines()[-1]), {"continued": True})
            self.assertNotIn(value, path.read_text())

    def test_background_helper_cleans_late_file_and_repeated_appends(self):
        value = "demo_Aq9v7B2m4K6p8H0t1"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            process = prompt_hook.scrub_later(str(path), [value])
            try:
                for _ in range(2):
                    with path.open("a") as out:
                        out.write(json.dumps({"prompt": value}) + "\n")
                    deadline = time.monotonic() + 5
                    while value in path.read_text() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertNotIn(value, path.read_text())
                    for line in path.read_text().splitlines():
                        json.loads(line)
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_large_existing_entry_is_not_skipped(self):
        value = "demo_Aq9v7B2m4K6p8H0t1"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text(json.dumps({"prompt": value + "x" * 70000}) + "\n")
            process = prompt_hook.scrub_later(str(path), [value])
            try:
                deadline = time.monotonic() + 5
                while value in path.read_text() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertNotIn(value, path.read_text())
                json.loads(path.read_text())
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_rescue_finds_and_redacts_a_private_key_block(self):
        # A throwaway key made now. Claude Code logs an attached file as is, and a file viewer's copy with line numbers.
        import base64
        import contextlib
        import io
        from unittest.mock import patch
        lines = [base64.b64encode(os.urandom(48)).decode() for _ in range(3)]
        pem = "\n".join(["-----BEGIN PRIVATE KEY-----", *lines, "-----END PRIVATE KEY-----"])
        numbered = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(pem.splitlines(), 1))
        stand_in = "-----BEGIN RSA PRIVATE KEY-----\nabc123\n-----END RSA PRIVATE KEY-----"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            log = home / ".claude" / "projects" / "p" / "s.jsonl"
            log.parent.mkdir(parents=True)
            rows = [{"type": "attachment", "attachment": {"content": {"file": {"content": pem + "\n"}}}},
                    {"type": "user", "message": {"content": [{"type": "tool_result", "content": numbered}]}},
                    {"type": "user", "message": {"content": "keep " + stand_in}}]
            log.write_text("".join(json.dumps(r) + "\n" for r in rows))
            saved = home / "saved.json"
            saved.write_text(json.dumps({keys.fingerprint(pem): "TEST_PRIVATE_KEY"}))
            found = None
            with patch.object(keys, "HOME", home), patch.object(keys, "SAVED", saved):
                found = keys.scan(0, generic=False)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    keys.cmd_rescue(["--hours", "0", "--redact"])
            self.assertEqual({i["value"] for i in found.values()}, {pem})
            self.assertIn("saved as TEST_PRIVATE_KEY", out.getvalue())
            self.assertNotIn(lines[0], out.getvalue())
            text = log.read_text()
            for line in lines:
                self.assertNotIn(line, text)
            after = [json.loads(line) for line in text.splitlines()]
            self.assertIn("[key moved to keywarden: TEST_PRIVATE_KEY]", json.dumps(after))
            self.assertEqual(after[2], rows[2])


if __name__ == "__main__":
    unittest.main()
