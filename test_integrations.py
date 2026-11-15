"""Run: python3 test_integrations.py. Isolated installer and Codex hook contract tests.

Uses a fake 1Password CLI, temporary settings and synthetic keys; no real agent settings are changed.
"""
import contextlib
import io
import json
import re
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import hook
import install
from test_backends import FAKE_OP

os.environ.pop("AGENT_KEYS_CALLER", None)  # run as a person at a terminal, not as the agent running the tests

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

    def test_claude_plugin_upgrade_is_requested(self):
        repo = Path(self.tmp.name) / "plugin"
        (repo / "hooks").mkdir(parents=True)
        with patch.object(install, "REPO", repo), patch.object(install, "claude", return_value=True) as cli:
            install.install_plugin()
        self.assertIn(unittest.mock.call("plugin", "marketplace", "update", "keywarden"), cli.call_args_list)
        self.assertIn(unittest.mock.call("plugin", "update", "keywarden@keywarden", "--scope", "user"), cli.call_args_list)
        self.assertIn(unittest.mock.call("plugin", "enable", "keywarden@keywarden", "--scope", "user"), cli.call_args_list)

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
        (self.state / "config.json").write_text(json.dumps({"backend": "1password", "strict": True,
                                                          "approvals": "chat", "unlock_1password": False}))
        self.env = dict(os.environ, AGENT_KEYS_STATE=str(self.state), AGENT_KEYS_NS="keywarden-integration-test",
                        AGENT_KEYS_BACKEND="1password", AGENT_KEYS_OP=f'"{sys.executable}" "{self.root / "fakeop.py"}"',
                        FAKE_OP_DB=str(self.root / "op.json"))  # Fake reads succeed; approval tests use chat.
        self.value = "demo_" + "Aq9vJ2xB7p" * 4

    def run_script(self, script, *args, data=None, stdin=None, token=None):
        env = dict(self.env, AGENT_KEYS_CALLER=token) if token else self.env
        return subprocess.run([sys.executable, str(HERE / script), *args], env=env,
                              input=json.dumps(data) if data is not None else stdin,
                              text=True, capture_output=True, timeout=20)

    def token(self, codex=False):
        """The caller token the hook gives this test's chat (no session id, so "claude" or "codex:codex")."""
        result = self.run_script("hook.py", *(["--codex"] if codex else []),
                                 data={"tool_name": "Bash", "tool_input": {"command": "keys status"}})
        command = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        return re.search(r"AGENT_KEYS_CALLER(?:=| = ')([\w-]+)", command).group(1)

    def save(self):
        result = self.run_script("keys.py", "add", "DEMO_API_KEY", stdin=self.value)
        self.assertEqual(result.returncode, 0, result.stderr)

    def message(self, text):
        result = self.run_script("prompt_hook.py", "--codex", data={"prompt": text, "cwd": str(self.root)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout else {}

    def grant(self, token=None):
        return self.run_script("keys.py", "grant", "DEMO_API_KEY", "--for", "testing the API", token=token)

    def test_paste_is_saved_and_continues(self):
        out = self.message("key DEMO_API_KEY " + self.value)
        self.assertNotIn("decision", out)
        self.assertIn("Saved DEMO_API_KEY to keywarden", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("original paste may remain", out["systemMessage"])
        self.assertNotIn("Saved DEMO_API_KEY", out["systemMessage"])
        self.assertNotIn("suppressOriginalPrompt", json.dumps(out))
        self.assertEqual(list(json.loads((self.state / "grants.json").read_text())["callers"]), ["codex:codex"])
        self.assertNotIn(self.value, json.dumps(out))
        self.assertIn("DEMO_API_KEY", self.run_script("keys.py", "list").stdout)
        meta = (self.state / "meta.json").read_text()
        self.assertIn("pasted in a Codex chat", meta)
        self.assertNotIn(self.value, meta)
        self.assertEqual(self.message("use DEMO_API_KEY"), {})

    def test_fallback_privacy_note_is_separate_and_once_per_session(self):
        from prompt_hook import PRIVACY_NOTE
        payload = {"session_id": "S1", "prompt": "key DEMO_API_KEY " + self.value}
        first = json.loads(self.run_script("prompt_hook.py", "--codex", data=payload).stdout)
        second = json.loads(self.run_script("prompt_hook.py", "--codex", data=payload).stdout)
        other = json.loads(self.run_script("prompt_hook.py", "--codex", data=dict(payload, session_id="S2")).stdout)
        self.assertEqual(first["systemMessage"], PRIVACY_NOTE)
        self.assertNotIn("systemMessage", second)
        self.assertEqual(other["systemMessage"], PRIVACY_NOTE)
        self.assertNotIn("Saved", first["systemMessage"])
        self.assertNotIn("UserPromptSubmit says", first["hookSpecificOutput"]["additionalContext"])
        self.assertIn("normal reply", first["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn(self.value, (self.state / "capture-notices.json").read_text())

    def test_task_paste_approves_the_session_then_expiry_requires_chat(self):
        before = time.time()
        out = self.message("Build an app using this key.\nkey DEMO_API_KEY " + self.value)
        self.assertIn("Saved DEMO_API_KEY to keywarden. Approved for this session (12 hours).",
                      out["hookSpecificOutput"]["additionalContext"])
        grants = json.loads((self.state / "grants.json").read_text())["callers"]
        self.assertEqual(list(grants), ["codex:codex"])  # only the chat it was given in
        self.assertGreaterEqual(grants["codex:codex"]["DEMO_API_KEY"], before + 12 * 3600)
        self.assertLess(grants["codex:codex"]["DEMO_API_KEY"], time.time() + 12 * 3600 + 1)
        me = self.token(codex=True)
        self.assertEqual(self.grant(me).returncode, 0)
        self.assertNotEqual(self.grant().returncode, 0)  # a terminal or other agent still asks
        self.assertNotIn(self.value, (self.state / "use.log").read_text())
        self.assertEqual(json.loads((self.state / "config.json").read_text()).get("approvals", "chat"), "chat")
        (self.state / "grants.json").write_text(json.dumps({"callers": {"codex:codex": {"DEMO_API_KEY": time.time() - 1}}}))
        self.assertEqual(self.message("Build another app with DEMO_API_KEY"), {})
        self.assertNotEqual(self.grant(me).returncode, 0)
        out = self.message("approve 30m")
        self.assertIn("for 30 minutes", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.grant(me).returncode, 0)
        self.run_script("keys.py", "lock")
        self.grant(me)
        self.assertIn("for 2 hours", json.dumps(self.message("approve 2h")))
        self.run_script("keys.py", "lock")
        self.grant(me)
        self.assertIn("for 1 hour", json.dumps(self.message("approve")))

    def test_task_paste_respects_stronger_approval_settings(self):
        for extra in ({"protected": ["DEMO_API_KEY"]}, {"approvals": "phone"}):
            with self.subTest(extra=extra):
                (self.state / "config.json").write_text(json.dumps({"backend": "1password", "strict": True, **extra}))
                out = self.message("Build an app with this key.\nkey DEMO_API_KEY " + self.value)
                self.assertNotIn("Approved for this session", out["hookSpecificOutput"]["additionalContext"])
                self.assertIn("Approval needed before use", out["hookSpecificOutput"]["additionalContext"])
                self.assertFalse((self.state / "grants.json").exists())
                self.assertIn("Request approval", out["hookSpecificOutput"]["additionalContext"])

    def test_any_paste_approves_only_the_session_it_was_given_in(self):
        for request in ("Just save this key", "Do not use this key yet", "key BUILD"):
            with self.subTest(request=request):
                (self.state / "grants.json").unlink(missing_ok=True)
                out = self.message(request + ".\nkey DEMO_API_KEY " + self.value)
                self.assertIn("Approved for this session", out["hookSpecificOutput"]["additionalContext"])
                grants = json.loads((self.state / "grants.json").read_text())["callers"]
                self.assertEqual(list(grants), ["codex:codex"])
                self.assertNotEqual(self.grant().returncode, 0)  # anyone else still asks

    def test_claude_task_rewrite_saves_and_approves_before_forwarding(self):
        request = "Build an app with this key.\nkey DEMO_API_KEY " + self.value
        result = self.run_script("prompt_hook.py", "--rewrite", data={"prompt": request})
        out = json.loads(result.stdout)
        self.assertEqual(out["text"], "Build an app with this key.\nkey DEMO_API_KEY [DEMO_API_KEY]")
        self.assertEqual(out["approved"], ["DEMO_API_KEY"])
        self.assertEqual(out["notice"], "Saved DEMO_API_KEY to keywarden. Approved for this session (12 hours).")
        self.assertNotIn(self.value, result.stdout)
        self.assertEqual(self.grant(self.token()).returncode, 0)

    def test_claude_and_codex_share_the_store_but_not_grants(self):
        request = "Build an app with this key.\nkey DEMO_API_KEY " + self.value
        out = self.run_script("prompt_hook.py", "--rewrite", data={"prompt": request})
        self.assertEqual(json.loads(out.stdout)["approved"], ["DEMO_API_KEY"])
        session = self.run_script("session_hook.py", "--codex").stdout
        self.assertIn("DEMO_API_KEY", session)
        command = "python3 -c 'import os; print(\"loaded\" if len(os.environ[\"DEMO_API_KEY\"]) == 45 else \"wrong\")'"

        def use_with(agent_args):
            result = self.run_script("hook.py", *agent_args,
                                     data={"tool_name": "Bash", "tool_input": {"command": command}})
            wrapped = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
            run = subprocess.run(["bash", "-c", wrapped], env=self.env, capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "loaded")

        use_with([])  # The Claude chat the key was given in can use it.
        codex = self.run_script("hook.py", "--codex", data={"tool_name": "Bash", "tool_input": {"command": command}})
        self.assertEqual(codex.stdout, "")  # Codex gets nothing until it asks and the person approves.
        self.assertNotEqual(self.grant(self.token(codex=True)).returncode, 0)
        self.message("approve 30m")
        use_with(["--codex"])
        self.run_script("keys.py", "lock")
        for agent_args in ([], ["--codex"]):
            result = self.run_script("hook.py", *agent_args,
                                     data={"tool_name": "Bash", "tool_input": {"command": command}})
            if result.stdout:
                command = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
                self.assertNotIn(" env)", command)  # Caller tagging alone does not load any keys.

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
        me = self.token(codex=True)
        self.assertNotEqual(self.grant(me).returncode, 0)
        self.assertEqual(self.message("please explain"), {})
        self.assertFalse((self.state / "grants.json").exists())
        out = self.message("approve 1h")
        self.assertIn("approved DEMO_API_KEY for 1 hour", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.grant(me).returncode, 0)
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
        self.env["AGENT_KEYS_OP"] = f'"{sys.executable}" "{self.root / "missing-op.py"}"'
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
