#!/usr/bin/env python3
"""Claude Code PreToolUse hook from keywarden.

1. It stops tool calls that would print a key into the chat, like printenv or cat .env.
2. While keys are approved, it adds a line to the start of each Bash or PowerShell command that loads every key
   as an environment variable, so the agent can use "$NAME" with any tool without seeing the value.

The blocking only catches the usual ways a key gets printed. A determined command can still print one.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

GRANTS = Path(os.environ.get("AGENT_KEYS_STATE") or Path.home() / ".agent-keys") / "grants.json"
KEYS = f'"{Path(sys.executable).as_posix()}" "{(Path(__file__).resolve().parent / "keys.py").as_posix()}"'
MASK = f'"{Path(sys.executable).as_posix()}" -u "{(Path(__file__).resolve().parent / "mask.py").as_posix()}"'

# .env, .env.local, .envrc, .dev.vars, name.env, a .secrets folder and what's in it, and the usual files tools keep
# logins in. .env.tpl and .env.example stay readable.
ENV_FILE = (r"(?:\.secrets(?:[\\/][^\s\"']*)?|\.claude\.json|\.git-credentials|\.netrc|\.npmrc|\.pypirc"
            r"|\.aws[\\/]credentials|\.docker[\\/]config\.json|\.kube[\\/]config|id_(?:rsa|ed25519|ecdsa)"
            r"|[\w-]*\.(?:envrc|dev\.vars|env(?:\.(?!tpl\b|example\b|sample\b|template\b)[\w-]+)*))(?![\w.-])")
SHELL_RULES = [
    (r"\bop(?:\.exe)?\s+(?:read|inject)\b", "reads a secret out of 1Password"),
    (r"\bop(?:\.exe)?\s+item\s+get\b", "prints a 1Password item"),
    (r"--reveal\b|--no-masking\b", "turns off secret hiding"),
    (r"\bprintenv\b", "prints environment variables"),
    (r"\bkeys(?:\.py)?\"?\s+env\b", "prints every key"),
    (r"(?:^|[;&|(]\s*)(?:env|set|export\s+-p|declare\s+-x)\s*(?:$|[;&|)])", "prints environment variables"),
    (r"(?i)\b(?:gci|ls|dir|get-childitem|get-item)\s+env:", "prints environment variables"),
    (r"(?i)\[environment\]::getenvironmentvariables", "prints environment variables"),
    (r"(?i)(?:^|[;|]\s*)\$env:[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*(?:$|[;|])", "prints a secret variable"),
    (r"(?i)\b(?:echo|printf|print|write-host|write-output)\b[^|;&]*\$\{?(?:env:)?[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)",
     "prints a secret variable"),
    (r"(?i)\b(?:cat|type|head|tail|less|more|bat|grep|rg|sed|awk|get-content|gc|select-string|findstr|strings|xxd|od"
     r"|base64|cp|copy)\b[^|;&]*(?:^|[\s/\\\"'=])" + ENV_FILE, "reads a .env file"),
    (r"(?i)\bsource\s+[^|;&]*" + ENV_FILE + r"[^|;&]*[;&|]+\s*(?:env|printenv|echo)", "prints a .env file"),
    (r"(?i)docker(?:-|\s+)compose\s+[^|;&]*\bconfig\b", "prints values from a .env file"),
    (r"(?i)find-generic-password|dump-keychain|\bCredRead|CredentialManager|Get-StoredCredential|PasswordVault"
     r"|\bkeyring\s+get\b|(?:import|from)\s+local_store\b|local_store\.(?:read|names)\(",
     "reads a key straight from the key store, skipping approval"),
    (r"(?i)\.agent-keys\b", "touches keywarden's own files, which say who approved what"),
]
SHELL_RULES = [(re.compile(p), why) for p, why in SHELL_RULES]
ENV_PATH = re.compile(r"(?:^|[\\/])" + ENV_FILE + r"$", re.I)
OWN_FILES = re.compile(r"(?i)(?:^|[\\/])\.agent-keys(?:[\\/]|$)")
SETTINGS = re.compile(r"(?i)(?:^|[\\/])\.claude[\\/]settings(?:\.local)?\.json$")
CODEX_SETTINGS = re.compile(r"(?i)(?:^|[\\/])\.codex[\\/](?:hooks\.json|config\.toml)$")
HOME = str(Path.home()).replace("\\", "/").rstrip("/").lower()


def written_key(tool, inp):
    """Name of a key that this call would write into a file or a command."""
    if tool in ("Bash", "PowerShell"):
        texts = [inp.get("command", "")]
    elif tool == "Write":
        texts = [inp.get("content", "")]
    elif tool == "Edit":
        texts = [inp.get("new_string", "")]
    elif tool == "MultiEdit":
        texts = [e.get("new_string", "") for e in inp.get("edits", [])]
    elif tool == "NotebookEdit":
        texts = [inp.get("new_source", "")]
    else:
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import keys
    for t in texts:
        name = keys.key_in(t or "")
        if name:
            return name
    return None


def check(tool, inp):
    if tool == "apply_patch":
        return check_patch(inp.get("command", ""))
    name = written_key(tool, inp)
    if name:
        return (f"would put the key {name} into a {'command' if tool in ('Bash', 'PowerShell') else 'file'}. "
                f"Use the environment variable ${name} instead (run keys status), or save a new key with keys add")
    if tool in ("Bash", "PowerShell"):
        cmd = inp.get("command", "")
        for rx, why in SHELL_RULES:
            if rx.search(cmd):
                return why
    elif tool == "Read":
        path = inp.get("file_path", "")
        if ENV_PATH.search(path):
            return "opens a file that holds keys or logins"
        if OWN_FILES.search(path):
            return "opens keywarden's own files"
    elif tool == "Grep":
        path = inp.get("path", "") or ""
        norm = path.replace("\\", "/").rstrip("/").lower()
        if ENV_PATH.search(path) or OWN_FILES.search(path) or ".env" in inp.get("glob", "").replace(".env.tpl", ""):
            return "searches files that hold keys"
        if norm in (HOME, "~", "/", "") and path or re.fullmatch(r"[a-z]:", norm or "x"):
            return "searches your whole home folder or drive, which has key files in it. Search a narrower folder"
    elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path", "") or inp.get("notebook_path", "")
        if OWN_FILES.search(path):
            return "changes who can approve key use"
        if SETTINGS.search(path) or CODEX_SETTINGS.search(path):
            removed = inp.get("old_string", "") + "".join(e.get("old_string", "") for e in inp.get("edits", []))
            ours = lambda t: any(w in t.lower() for w in ("keywarden", "agent-keys", "prompt_hook.py", "session_hook.py"))
            if ours(removed) or (tool == "Write" and not ours(inp.get("content", ""))):
                return "removes the keywarden hooks from the agent's settings"
    return None


def check_patch(patch):
    """Check Codex's patch format using the same file and key rules as Claude edits."""
    sections = re.split(r"(?m)^\*\*\* (Add|Update|Delete) File: (.+)\n", patch)
    for i in range(1, len(sections), 3):
        action, path, body = sections[i:i + 3]
        added = "\n".join(line[1:] for line in body.splitlines() if line.startswith("+"))
        removed = "\n".join(line[1:] for line in body.splitlines() if line.startswith("-"))
        tool = "Edit" if action == "Update" else "Write"
        why = check(tool, {"file_path": path, "content": added, "old_string": removed, "new_string": added})
        if why:
            return why
        move = re.search(r"(?m)^\*\*\* Move to: (.+)$", body)
        if move:
            why = check("Write", {"file_path": move[1], "content": added})
            if why:
                return why
    return None


def main():
    try:
        data = json.load(sys.stdin)
        why = check(data.get("tool_name", ""), data.get("tool_input") or {})
    except Exception as e:
        # Don't break every Claude session over a bug here, but leave a record so the gap gets noticed.
        try:
            with open(GRANTS.parent / "hook-errors.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(e).__name__}: {e}\n")
        except OSError:
            pass
        sys.exit(0)
    if why:
        print(f"keywarden blocked this because it {why}. Key values stay out of the chat. "
              "To use a key, run the program with: keys run KEY_NAME -- <command>. "
              "To see which keys exist: keys list.", file=sys.stderr)
        sys.exit(2)
    # While keys are approved, put every key in the command's environment, and send the command's output through
    # mask.py so a key value that gets printed (curl -v, env, a debug line) shows as <concealed by keywarden>.
    # The command text only gets the lines that load the keys, so values never appear in the chat.
    inp, tool = data.get("tool_input") or {}, data.get("tool_name")
    if tool in ("Bash", "PowerShell") and inp.get("command") and approved():
        codex = "--codex" in sys.argv[1:]
        shell = "PowerShell" if codex and os.name == "nt" and tool == "Bash" else tool
        out = {"hookEventName": "PreToolUse", "updatedInput": dict(inp, command=wrap(shell, inp["command"]))}
        if codex:
            out["permissionDecision"] = "allow"  # Codex requires this with updatedInput.
        print(json.dumps({"hookSpecificOutput": out}))
    sys.exit(0)


def wrap(tool, cmd):
    if tool == "Bash":
        # The filters run as process substitutions on fds 8 and 9. After the command, close them and wait up to
        # 5 seconds for the filters to finish writing, or Bash returns before the output arrives. A background job
        # the command started can keep a filter open, so the wait has a limit instead of using `wait`.
        return (f'eval "$({KEYS} env)"; exec 8> >({MASK}); __kw1=$!; exec 9> >({MASK} >&2); __kw2=$!; '
                f'{{ {cmd}\n}} >&8 2>&9; __kws=$?; exec 8>&- 9>&-; __kwi=0; '
                'while { kill -0 $__kw1 || kill -0 $__kw2; } 2>/dev/null && [ $__kwi -lt 50 ]; '
                'do sleep 0.1; __kwi=$((__kwi+1)); done; (exit $__kws)')
    ps_keys, ps_mask = KEYS.replace('"', "'"), MASK.replace('"', "'")
    return (f"& {ps_keys} env --ps | Out-String | Invoke-Expression; "
            f"& {{ {cmd}\n}} 2>&1 | ForEach-Object {{ \"$_\" }} | & {ps_mask}")


def approved():
    try:
        now = time.time()
        return any(isinstance(v, (int, float)) and v > now for v in json.loads(GRANTS.read_text()).values())
    except Exception:
        return False


if __name__ == "__main__":
    main()
