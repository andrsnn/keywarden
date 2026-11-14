#!/usr/bin/env python3
"""Save keys supplied in user prompts and approve task use for one hour.

Claude's plugin replaces detected values before forwarding. Command hooks let the task continue, attach
saved-name guidance, and attempt local transcript cleanup; the original prompt can still reach the model.
Only messages submitted by the user handle later approval replies.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

APPROVE = re.compile(r"(?i)^\s*(?:approve[sd]?|yes|yep|yeah|y|ok|okay|allow|allowed|sure|go ahead|do it|grant)\b")
APPROVE_WORD = re.compile(r"(?i)^\s*approve[sd]?\b")
DENY = re.compile(r"(?i)^\s*(?:no|nope|deny|denied|don'?t|stop)\b")
NOTICES = ("<task-notification", "<system-reminder", "<command-", "<local-command", "<bash-", "<user-memory",
           "Another Claude session", "[SYSTEM NOTIFICATION")
TIME = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\b")
IS_CODEX = "--codex" in sys.argv[1:]
AGENT = "Codex" if IS_CODEX else "Claude"


def block(reason):
    # Claude Code shows this under "UserPromptSubmit operation blocked by hook", so the reason has to say the
    # save worked. suppressOriginalPrompt stops it repeating the message, key and all, as "Original prompt".
    # Claude Code still puts the message back in the input box, so the reason says how to clear it.
    out = {"decision": "block", "reason": reason}
    if not IS_CODEX:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}
    print(json.dumps(out))
    sys.exit(0)


def tell_agent(text, notice=None):
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    if notice:
        out["systemMessage"] = notice
    print(json.dumps(out))
    sys.exit(0)


def minutes_in(text):
    m = TIME.search(text)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    return n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)


def scrub_later(transcript, values):
    """Best-effort cleanup of the supplied local transcript, including writes after the hook returns."""
    if not transcript:
        return
    import subprocess
    # On a session's first message the log file doesn't exist yet. The job waits for it.
    start = 0  # Include earlier copies of the same key, including long JSON lines or private key blocks.
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
    p.stdin.write(json.dumps(values).encode())
    p.stdin.close()
    return p


def session_caller(data, agent):
    """The caller a pasted key goes to: the session the person typed into. The same name hook.py gives that
    session's own commands (a subagent's commands carry its agent id too, so they don't match)."""
    sid = data.get("session_id") or ("codex" if agent == "Codex" else "claude")
    return f"codex:{sid}" if agent == "Codex" else sid


def capture(data, agent):
    """Save detected values, then derive consent from the user's sanitized request. Never return values."""
    import approve
    import keys
    text = data.get("prompt") or ""
    found = [] if text.lstrip().startswith(NOTICES) else keys.find_keys(text)
    if not found:
        return {"text": text, "saved": []}
    results = keys.save_found(found, text, data.get("cwd") or "", agent=agent)
    for (_, value, _), (name, _) in zip(found, results):
        text = text.replace(value, f"[{name}]")
    if keys.find_keys(text):
        raise ValueError("a detected value remained after replacement")
    names = [name for name, _ in results]
    who = session_caller(data, agent)
    # Giving a session a key is the approval: that session can use it. Protected keys and phone mode still ask.
    approved = approve.grant_from_prompt(names, text, who)
    pending = [name for name in names if name not in approved]
    notice = ". ".join(f"Saved as {name}" for name in names) + "."
    span = approve.span(approve.paste_minutes())
    if approved:
        notice += (f" This session can use it for {span}." if not pending
                   else f" This session can use {', '.join(approved)} for {span}.")
    context = (f"keywarden: {notice} Tell the person this confirmation, then continue their task. "
               f"Use the saved names {', '.join(names)}; never repeat or write the values. "
               'Run keys status, then use "$NAME" in commands. ')
    if pending:
        context += f"{', '.join(pending)} has no new grant from this paste. Request approval with keys grant before use. "
    context += "Local transcript redaction is best-effort; do not claim that all copies were erased."
    try:
        scrub_later(data.get("transcript_path"), [v for _, v, _ in found])
    except Exception:
        pass
    return {"text": text, "saved": [list(r) for r in results], "approved": approved,
            "notice": notice, "context": context}


def rewrite():
    """Called by keywarden's Claude Code plugin (hooks/register.ts) before Claude Code's own hooks run. Saves any
    key in the message and swaps it for [NAME], so the message still goes to Claude, just without the key.
    Prints {"text", "saved": [[name, how]]}, or {"drop": reason} if a key couldn't be saved."""
    data = json.load(sys.stdin)
    try:
        out = capture(data, "Claude")
    except BaseException:
        out = {"drop": "keywarden couldn't finish saving or approving the key. Your message wasn't sent. Check keys setup."}
    print(json.dumps(out))


def main():
    if sys.argv[1:] == ["--rewrite"]:
        return rewrite()
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
            out = capture(data, AGENT)
        except BaseException:
            block("keywarden couldn't finish saving or approving the key. Your message wasn't sent. Check keys setup.")
        disclosure = (f" {AGENT}'s prompt hook cannot replace your original message; the pasted value can reach "
                      "the model and local history. Local redaction is best-effort.")
        tell_agent(out["context"] + disclosure, out["notice"] + disclosure)
    try:
        import approve
        # The word approve (or no) answers a request from up to 12 hours ago. A loose yes or ok only
        # answers one from the last 30 minutes, so it can't grant an old request by accident.
        window = approve.APPROVE_WORD_MINUTES if APPROVE_WORD.match(text) or DENY.match(text) else approve.PENDING_MINUTES
        session = session_caller(data, AGENT) if data.get("session_id") else None
        if len(text) <= 80 and approve.pending(window, session):
            if APPROVE.match(text):
                minutes = minutes_in(text)
                names = approve.answer_pending(True, minutes, window, session)
                span = approve.span(minutes or approve.settings()[2])
                tell_agent(f"keywarden: the person approved {', '.join(names)} for {span}. "
                            "Run the command that asked for it again.")
            if DENY.match(text):
                names = approve.answer_pending(False, max_age=window, session=session)
                tell_agent(f"keywarden: the person said no to {', '.join(names)}. Don't ask again.")
    except SystemExit:
        raise
    except BaseException:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
