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
        sid = data.get("session_id") or ("codex" if codex else "claude")
        names = sorted(keys.existing_names())
        granted = sorted(approve.grants(f"codex:{sid}" if codex else sid))
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
             "Approvals belong to one session: a subagent you start asks for its own. ") +
            "Load the keywarden skill for the details.")
    if granted:
        text += f" Granted now: {', '.join(granted)}."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


if __name__ == "__main__":
    main()
