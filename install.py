#!/usr/bin/env python3
"""Install keywarden for Claude Code and local Codex.

  python install.py              install, or update after a git pull
  python install.py --uninstall  remove the command, skill and hook
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()
BIN = HOME / ".local" / "bin"
SKILL = HOME / ".claude" / "skills" / "keywarden"
OLD_SKILL = HOME / ".claude" / "skills" / "agent-keys"  # the name before keywarden
SETTINGS = HOME / ".claude" / "settings.json"
CODEX_HOOKS = Path(os.environ.get("CODEX_HOME") or HOME / ".codex") / "hooks.json"
CODEX_SKILL = HOME / ".agents" / "skills" / "keywarden"
IS_WIN = os.name == "nt"
PY = Path(sys.executable).as_posix()
HOOK_CMD = f'"{PY}" "{(REPO / "hook.py").as_posix()}"'
MATCHER = "Bash|PowerShell|Read|Grep|Write|Edit|MultiEdit|NotebookEdit"


def say(msg):
    print(f"  {msg}")


def load_settings(path=None):
    path = path or SETTINGS
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_settings(d, path=None):
    path = path or SETTINGS
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-agent-keys"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def drop_our_hook(d):
    def ours(h):
        command = h.get("command", "")
        return "hook.py" in command and any(part in command for part in (REPO.as_posix(), "agent-keys", "keywarden"))

    for event in ("PreToolUse", "UserPromptSubmit", "SessionStart"):
        groups = d.get("hooks", {}).get(event, [])
        keep = []
        for g in groups:
            handlers = [h for h in g.get("hooks", []) if not ours(h)]
            if handlers:
                keep.append(dict(g, hooks=handlers))
        if groups:
            d["hooks"][event] = keep
    return d


def install_op():
    if shutil.which("op") or (IS_WIN and list((Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet")
                                         .glob("Packages/AgileBits.1Password.CLI*/op.exe"))):
        say("1Password CLI: already installed")
        return
    if IS_WIN and shutil.which("winget"):
        cmd = ["winget", "install", "--id", "AgileBits.1Password.CLI", "-e", "--silent",
               "--accept-package-agreements", "--accept-source-agreements"]
    elif sys.platform == "darwin" and shutil.which("brew"):
        cmd = ["brew", "install", "1password-cli"]
    else:
        say("1Password CLI: install it from https://developer.1password.com/docs/cli/get-started/")
        return
    say("1Password CLI: installing...")
    subprocess.run(cmd)


def claude(*args):
    exe = shutil.which("claude")
    return exe and subprocess.run([exe, *args], capture_output=True, text=True).returncode == 0


def install_plugin():
    """The Claude Code plugin swaps a pasted key for its name and lets the message through. Without it, the
    UserPromptSubmit hook stops any message with a key in it."""
    (REPO / "hooks" / "python-path.txt").write_text(PY + "\n", encoding="utf-8")
    claude("plugin", "marketplace", "add", str(REPO))
    if claude("plugin", "install", "keywarden@keywarden", "--scope", "user"):
        say("Claude plugin: installed (a pasted key is swapped for its name, and your message still goes through)")
    else:
        say("Claude plugin: not installed. Run  claude plugin marketplace add " + str(REPO) +
            "  and then  claude plugin install keywarden@keywarden")


def install(agent="both"):
    print("Installing keywarden")
    install_op()

    try:
        import cryptography  # noqa: F401  (phone approval checks passkey signatures with it)
        say("cryptography: already installed")
    except ImportError:
        say("cryptography: installing (needed for phone approval)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "cryptography"])

    BIN.mkdir(parents=True, exist_ok=True)
    shim = BIN / "keys"
    shim.write_text(f'#!/usr/bin/env bash\nexec "{PY}" "{(REPO / "keys.py").as_posix()}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    if IS_WIN:
        (BIN / "keys.cmd").write_text(f'@"{sys.executable}" "{REPO / "keys.py"}" %*\n', encoding="utf-8")
    say(f"command: {shim}")
    path_dirs = [Path(p).resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if BIN.resolve() not in path_dirs:
        if IS_WIN:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "$p=[Environment]::GetEnvironmentVariable('Path','User');"
                            f"[Environment]::SetEnvironmentVariable('Path', $p + ';{BIN}', 'User')"])
        else:
            for rc in (HOME / ".zshrc", HOME / ".bashrc"):
                if rc.exists() or rc.name == ".zshrc":
                    with open(rc, "a") as f:
                        f.write('\nexport PATH="$HOME/.local/bin:$PATH"  # keywarden\n')
        say(f"added {BIN} to PATH (open a new terminal)")

    if sys.platform == "darwin":
        if shutil.which("swiftc"):
            (HOME / ".agent-keys").mkdir(exist_ok=True)
            r = subprocess.run(["swiftc", "-O", str(REPO / "touchid.swift"), "-o", str(HOME / ".agent-keys" / "touchid")])
            say("Touch ID: ready" if r.returncode == 0 else "Touch ID: the helper didn't build (see the error above)")
        else:
            say("Touch ID: run  xcode-select --install  and then run install.py again")

    if agent in ("both", "claude"):
        install_claude()
    if agent in ("both", "codex"):
        install_codex()

    print("\nNext: run  keys setup  to keep keys on this computer, or  keys setup 1password  to use 1Password.")
    print("Then run  keys phone  to approve key use from your phone.")


def install_claude():
    shutil.rmtree(OLD_SKILL, ignore_errors=True)
    SKILL.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "skill" / "SKILL.md", SKILL / "SKILL.md")
    say(f"Claude skill: {SKILL}")

    d = drop_our_hook(load_settings())
    d.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": MATCHER, "hooks": [{"type": "command", "command": HOOK_CMD}]})
    d["hooks"].setdefault("UserPromptSubmit", []).append(
        {"hooks": [{"type": "command", "command": f'"{PY}" "{(REPO / "prompt_hook.py").as_posix()}"'}]})
    d["hooks"].setdefault("SessionStart", []).append(
        {"hooks": [{"type": "command", "command": f'"{PY}" "{(REPO / "session_hook.py").as_posix()}"'}]})
    write_settings(d)
    say(f"Claude hook: added to {SETTINGS}")
    install_plugin()


def install_codex():
    CODEX_SKILL.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "skill" / "SKILL.md", CODEX_SKILL / "SKILL.md")
    say(f"Codex skill: {CODEX_SKILL}")
    d = drop_our_hook(load_settings(CODEX_HOOKS))
    for event, script in (("PreToolUse", "hook.py"), ("UserPromptSubmit", "prompt_hook.py"),
                          ("SessionStart", "session_hook.py")):
        group = {"hooks": [{"type": "command", "command": f'"{PY}" "{(REPO / script).as_posix()}" --codex'}]}
        if event == "PreToolUse":
            group["matcher"] = MATCHER + "|apply_patch"
        d.setdefault("hooks", {}).setdefault(event, []).append(group)
    write_settings(d, CODEX_HOOKS)
    say(f"Codex hooks: added to {CODEX_HOOKS}")
    say("Codex: restart, then review and trust keywarden in /hooks (desktop: Settings > Hooks).")
    say("Codex: pasted keys are saved and the message is blocked. Resend using the saved name.")


def uninstall(agent="both"):
    print("Removing keywarden")
    if agent == "both":
        for f in (BIN / "keys", BIN / "keys.cmd"):
            f.unlink(missing_ok=True)
    if agent in ("both", "claude"):
        shutil.rmtree(SKILL, ignore_errors=True)
        shutil.rmtree(OLD_SKILL, ignore_errors=True)
        if SETTINGS.exists():
            write_settings(drop_our_hook(load_settings()))
        claude("plugin", "uninstall", "keywarden@keywarden")
    if agent in ("both", "codex"):
        shutil.rmtree(CODEX_SKILL, ignore_errors=True)
        if CODEX_HOOKS.exists():
            write_settings(drop_our_hook(load_settings(CODEX_HOOKS)), CODEX_HOOKS)
    say("removed the selected integrations. Your saved keys are unchanged.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("both", "claude", "codex"), default="both")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    uninstall(args.agent) if args.uninstall else install(args.agent)
