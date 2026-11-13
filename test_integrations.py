"""Run: python3 test_integrations.py. Isolated installer and Codex hook contract tests.

Uses a fake 1Password CLI, temporary settings and synthetic keys; no real agent settings are changed.
"""
import contextlib
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hook
import install
from test_backends import FAKE_OP

HERE = Path(__file__).resolve().parent


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="keywarden-install-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in {"HOME": root, "BIN": root / "bin", "SKILL": root / "claude/skills/keywarden",
                            "OLD_SKILL": root / "claude/skills/agent-keys", "SETTINGS": root / "claude/settings.json",
                            "CODEX_HOOKS": root / "codex/hooks.json", "CODEX_SKILL": root / "agents/skills/keywarden"}.items():
            p = patch.object(install, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_codex_install_update_uninstall_preserves_other_hooks(self):
        other = {"type": "command", "command": "python unrelated.py"}
        original = {"description": "keep this", "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [other, {"type": "command", "command": install.HOOK_CMD}]}]}}
        install.write_settings(original, install.CODEX_HOOKS)
        install.install_codex()
        once = install.load_settings(install.CODEX_HOOKS)
        install.install_codex()
        self.assertEqual(once, install.load_settings(install.CODEX_HOOKS))
        self.assertEqual(once["hooks"]["PreToolUse"][0]["hooks"], [other])
        self.assertEqual(once["description"], "keep this")
        self.assertTrue(install.CODEX_HOOKS.with_name("hooks.json.bak-agent-keys").exists())
        self.assertTrue((install.CODEX_SKILL / "SKILL.md").exists())
        for event, groups in once["hooks"].items():
            own = [h for g in groups for h in g["hooks"] if "--codex" in h["command"]]
            self.assertEqual(len(own), 1, event)
        self.assertIn("apply_patch", once["hooks"]["PreToolUse"][-1]["matcher"])
        self.assertIn("review and trust", self.output.getvalue())
        install.uninstall("codex")
        self.assertFalse(install.CODEX_SKILL.exists())
        self.assertEqual(install.load_settings(install.CODEX_HOOKS)["hooks"]["PreToolUse"][0]["hooks"], [other])

    def test_claude_install_is_idempotent(self):
        with patch.object(install, "install_plugin"):
            install.install_claude()
            once = install.load_settings()
            install.install_claude()
        self.assertEqual(once, install.load_settings())
        self.assertEqual(set(once["hooks"]), {"PreToolUse", "UserPromptSubmit", "SessionStart"})
        self.assertTrue((install.SKILL / "SKILL.md").exists())
        self.assertNotIn("--codex", json.dumps(once))

    def test_default_installs_both(self):
        with patch.object(install, "install_op"), patch.object(install, "install_plugin"), \
                patch.object(install.subprocess, "run"), patch.object(install.shutil, "which", return_value=None):
            install.install()
        self.assertTrue(install.SETTINGS.exists())
        self.assertTrue(install.CODEX_HOOKS.exists())
        self.assertTrue((install.BIN / "keys").exists())
        install.uninstall()
        self.assertFalse((install.BIN / "keys").exists())
        self.assertFalse(install.CODEX_SKILL.exists())


class CodexFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="keywarden-codex-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.root / "fakeop.py").write_text(FAKE_OP)
        (self.root / "op.json").write_text(json.dumps({"vaults": {"Agent Keys": {}}}))
        (self.state / "config.json").write_text(json.dumps({"backend": "1password", "strict": True}))
        self.env = dict(os.environ, AGENT_KEYS_STATE=str(self.state), AGENT_KEYS_NS="keywarden-integration-test",
                        AGENT_KEYS_BACKEND="1password", AGENT_KEYS_OP=f'"{sys.executable}" "{self.root / "fakeop.py"}"',
                        FAKE_OP_DB=str(self.root / "op.json"))
        self.value = "demo_" + "Aq9vJ2xB7p" * 4

    def run_script(self, script, *args, data=None, stdin=None):
        return subprocess.run([sys.executable, str(HERE / script), *args], env=self.env,
                              input=json.dumps(data) if data is not None else stdin,
                              text=True, capture_output=True, timeout=20)

    def save(self):
        result = self.run_script("keys.py", "add", "DEMO_API_KEY", stdin=self.value)
        self.assertEqual(result.returncode, 0, result.stderr)

    def message(self, text):
        result = self.run_script("prompt_hook.py", "--codex", data={"prompt": text, "cwd": str(self.root)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout else {}

    def grant(self):
        return self.run_script("keys.py", "grant", "DEMO_API_KEY", "--for", "testing the API")

    def test_paste_is_saved_and_blocked(self):
        out = self.message("key DEMO_API_KEY " + self.value)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Saved your key as DEMO_API_KEY", out["reason"])
        self.assertIn("ask Codex to use DEMO_API_KEY", out["reason"])
        self.assertNotIn("hookSpecificOutput", out)  # No Claude-only suppressOriginalPrompt field.
        self.assertNotIn(self.value, json.dumps(out))
        self.assertIn("DEMO_API_KEY", self.run_script("keys.py", "list").stdout)
        meta = (self.state / "meta.json").read_text()
        self.assertIn("pasted in a Codex chat", meta)
        self.assertNotIn(self.value, meta)
        self.assertEqual(self.message("use DEMO_API_KEY"), {})

    def test_claude_explicit_save_rewrite_passes_fallback(self):
        result = self.run_script("prompt_hook.py", "--rewrite", data={"prompt": "key DEMO_API_KEY " + self.value})
        out = json.loads(result.stdout)
        self.assertEqual(out.get("text"), "key DEMO_API_KEY [DEMO_API_KEY]")
        result = self.run_script("prompt_hook.py", data={"prompt": out["text"]})
        self.assertEqual(result.stdout, "")
        db = json.loads((self.root / "op.json").read_text())
        self.assertEqual(db["vaults"]["Agent Keys"]["DEMO_API_KEY"], self.value)

    def test_approve_run_mask_and_lock(self):
        self.save()
        self.assertNotEqual(self.grant().returncode, 0)
        self.assertEqual(self.message("please explain"), {})
        self.assertFalse((self.state / "grants.json").exists())
        out = self.message("approve 1h")
        self.assertIn("approved DEMO_API_KEY for 1 hour", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.grant().returncode, 0)
        tool_input = {"command": "python3 -c 'import os; print(os.environ[\"DEMO_API_KEY\"])'", "description": "demo"}
        result = self.run_script("hook.py", "--codex", data={"tool_name": "Bash", "tool_input": tool_input})
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"]["description"], "demo")
        self.assertNotIn(self.value, result.stdout)
        for shell in ("bash", "zsh"):
            if shutil.which(shell):
                with self.subTest(shell=shell):
                    result = subprocess.run([shell, "-c", out["updatedInput"]["command"]], env=self.env,
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "<concealed by keywarden>")
        self.run_script("keys.py", "lock")
        self.assertEqual(self.run_script("keys.py", "env").stdout, "")

    def test_deny_does_not_grant(self):
        self.save()
        self.grant()
        self.assertIn("said no to DEMO_API_KEY", json.dumps(self.message("no")))
        self.assertEqual(self.message("approve"), {})
        self.assertEqual(self.run_script("keys.py", "env").stdout, "")

    def test_session_lists_names_without_values(self):
        self.save()
        result = self.run_script("session_hook.py", "--codex")
        self.assertIn("DEMO_API_KEY", result.stdout)
        self.assertNotIn(self.value, result.stdout)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_block_exposure_before_injection(self):
        result = self.run_script("hook.py", "--codex", data={"tool_name": "Bash", "tool_input": {"command": "printenv"}})
        self.assertEqual(result.returncode, 2)
        self.assertIn("prints environment variables", result.stderr)

    def test_save_failure_blocks_prompt(self):
        (self.state / "config.json").write_text('{"backend":"unknown"}')
        self.env["AGENT_KEYS_BACKEND"] = "unknown"
        out = self.message("key DEMO_API_KEY " + self.value)
        self.assertEqual(out["decision"], "block")
        self.assertNotIn(self.value, json.dumps(out))


class PatchTests(unittest.TestCase):
    def test_patch_rules(self):
        key = "sk-" + secrets.token_urlsafe(40)
        cases = [
            ("*** Add File: /proj/a.py\n+token = '" + key + "'\n", True),
            ("*** Update File: /home/u/.agent-keys/grants.json\n-{}\n+{}\n", True),
            ("*** Delete File: /home/u/.codex/hooks.json\n", True),
            ("*** Delete File: .codex/hooks.json\n", True),
            ("*** Update File: .agent-keys/grants.json\n-{}\n+{}\n", True),
            ("*** Update File: /home/u/.codex/config.toml\n-command = 'python keywarden/hook.py'\n", True),
            ("*** Add File: /proj/a.py\n+import os\n+token = os.environ['DEMO_API_KEY']\n", False),
            ("*** Update File: /proj/a.py\n-token = '" + key + "'\n+token = None\n", False),
            ("*** Update File: /proj/a.py\n*** Move to: /home/u/.agent-keys/grants.json\n+{}\n", True),
            ("*** Add File: /proj/a.py\n+pass\n*** Add File: /home/u/.agent-keys/grants.json\n+{}\n", True),
        ]
        for body, blocked in cases:
            with self.subTest(body=body):
                self.assertEqual(bool(hook.check("apply_patch", {"command": "*** Begin Patch\n" + body + "*** End Patch"})), blocked)


if __name__ == "__main__":
    unittest.main()
