#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook from keywarden. It sees every message you send before Claude does.

1. If the message has a key in it, it saves the key and stops the message, so the key never reaches Claude,
   its chat log or the model provider. See keys.find_keys for what counts.
2. If Claude asked for a key and you reply approve (or yes, ok, approve 1h), it creates the grant. If you reply
   no, it drops the request. Only a message you typed can do this, so Claude can't approve itself.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

APPROVE = re.compile(r"(?i)^\s*(?:approve[sd]?|yes|yep|yeah|y|ok|okay|allow|allowed|sure|go ahead|do it|grant)\b")
DENY = re.compile(r"(?i)^\s*(?:no|nope|deny|denied|don'?t|stop)\b")
TIME = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\b")


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def tell_claude(text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}))
    sys.exit(0)


def minutes_in(text):
    m = TIME.search(text)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    return n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)


def main():
    try:
        text = json.load(sys.stdin).get("prompt") or ""
        import keys
        found = keys.find_keys(text)
    except BaseException:
        sys.exit(0)  # never get in the way of a message because of a bug here
    if found:
        try:
            results = keys.save_found(found)
        except BaseException as e:  # keys.die() raises SystemExit
            block(f"keywarden couldn't save the key ({e}). Your message wasn't sent.")
        words = {"saved": "Saved", "replaced": "Replaced the old", "already saved": "Already had"}
        done = ". ".join(f"{words[how]} {name}" for name, how in results)
        block(f"{done}. Your message wasn't sent, so the key never reached Claude. Now ask Claude to use "
              f"{results[0][0]}. To change the name, ask Claude to rename it.")
    try:
        import approve
        if len(text) <= 80 and approve.pending():
            if APPROVE.match(text):
                minutes = minutes_in(text)
                names = approve.answer_pending(True, minutes)
                span = approve.span(minutes or approve.settings()[2])
                tell_claude(f"keywarden: the person approved {', '.join(names)} for {span}. "
                            "Run the command that asked for it again.")
            if DENY.match(text):
                names = approve.answer_pending(False)
                tell_claude(f"keywarden: the person said no to {', '.join(names)}. Don't ask again.")
    except SystemExit:
        raise
    except BaseException:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
