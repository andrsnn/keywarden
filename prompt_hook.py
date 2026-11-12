#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook from keywarden. It sees every message you send before Claude does.

1. If the message has a key in it, it saves the key and stops the message, so the key never reaches Claude or
   the model provider. Claude Code still logs the stopped message, so a background job blanks the key out of
   that log. See keys.find_keys for what counts.
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
NOTICES = ("<task-notification", "<system-reminder", "<command-", "<local-command", "<bash-", "<user-memory",
           "Another Claude session", "[SYSTEM NOTIFICATION")
TIME = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\b")


def block(reason):
    # Claude Code shows this under "UserPromptSubmit operation blocked by hook", so the reason has to say the
    # save worked. suppressOriginalPrompt stops it repeating the message, key and all, as "Original prompt".
    # Claude Code still puts the message back in the input box, so the reason says how to clear it.
    print(json.dumps({"decision": "block", "reason": reason,
                      "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}}))
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


def scrub_later(transcript, values):
    """Claude Code writes a blocked message into the session log ("Original prompt: ...") after this hook
    returns. Start a background job that blanks the keys out of that log once the line lands."""
    if not transcript:
        return
    import subprocess
    # On a session's first message the log file doesn't exist yet. The job waits for it.
    start = max(0, Path(transcript).stat().st_size - 65536) if Path(transcript).exists() else 0
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "keys.py"), "_scrub", transcript, str(start)]
    kw = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":  # keep running after the hook exits, with no console window
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            p = subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            p = subprocess.Popen(cmd, creationflags=flags, **kw)
    else:
        p = subprocess.Popen(cmd, start_new_session=True, **kw)
    p.stdin.write("\n".join(values).encode())
    p.stdin.close()


def main():
    try:
        data = json.load(sys.stdin)
        text = data.get("prompt") or ""
        if text.lstrip().startswith(NOTICES):
            sys.exit(0)  # Claude Code's own notices aren't the person typing, and their long IDs aren't keys
        import keys
        found = keys.find_keys(text)
    except BaseException:
        sys.exit(0)  # never get in the way of a message because of a bug here
    if found:
        try:
            results = keys.save_found(found, text, data.get("cwd") or "")
        except BaseException as e:  # keys.die() raises SystemExit
            block(f"keywarden couldn't save the key ({e}). Your message wasn't sent.")
        try:
            scrub_later(data.get("transcript_path"), [v for _, v, _ in found])
        except BaseException:
            pass
        words = {"saved": "Saved your key as", "replaced": "Replaced the old", "already saved": "Already had"}
        done = ". ".join(f"{words[how]} {name}" for name, how in results)
        block(f"{done}. This is keywarden working, not an error: it held your message back on purpose, so "
              f"Claude never saw the key. Press Ctrl+C to clear the key from the input box, then ask Claude to "
              f"use {results[0][0]}. To change the name, ask Claude to rename it.")
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
