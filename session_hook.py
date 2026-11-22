#!/usr/bin/env python3
"""Claude Code SessionStart hook from keywarden. It tells Claude which keys are saved, at the start of every
session and after every compaction, so Claude looks here before saying it doesn't have a key."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    try:
        import approve
        import keys
        data = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
        codex = "--codex" in sys.argv[1:]
        names = sorted(keys.existing_names())
        granted = sorted(approve.grants(approve.session_caller(data, codex)))
        meta = keys.load_json(keys.META, {})
    except BaseException:
        sys.exit(0)
    if not names:
        sys.exit(0)
    # Each name with its note or kind, so Claude can tell which key a task needs.
    listed = [n + (f" ({meta[n].get('note') or meta[n].get('kind')})"
                   if n in meta and (meta[n].get("note") or meta[n].get("kind")) else "") for n in names]
    text = (f"keywarden has these keys saved: {', '.join(listed)}. When a task needs a key, use one of these "
            "before saying you don't have it: run keys grant NAME --for \"why\", then use \"$NAME\" in commands. " +
            ("Approval controls are off: ordinary saved keys work automatically for agents and subagents. "
             if approve.settings()[0] == "auto" else
             "Approval is required for this thread; its subagents and subprocesses share approved keys and expiry. "
             "Other saved keys need permission. The user can preapprove named keys with: approve NAME1 NAME2 for 8h. ") +
            ("Approvals are in totp mode: a key request is approved only when the person replies with the 6-digit "
             "code from their authenticator app, and a yes alone doesn't approve. "
             if approve.settings()[0] == "totp" else "") +
            "Load the keywarden skill for the details.")
    if granted:
        text += f" Granted now: {', '.join(granted)}."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


if __name__ == "__main__":
    main()
