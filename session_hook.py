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
        names = sorted(keys.existing_names())
        granted = sorted(approve.grants())
    except BaseException:
        sys.exit(0)
    if not names:
        sys.exit(0)
    text = (f"keywarden has these keys saved: {', '.join(names)}. When a task needs a key, use one of these "
            "before saying you don't have it: run keys grant NAME --for \"why\", then use \"$NAME\" in commands. "
            "Load the keywarden skill for the details.")
    if granted:
        text += f" Granted now: {', '.join(granted)}."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


if __name__ == "__main__":
    main()
