#!/usr/bin/env python3
"""Save keys supplied in user prompts, with automatic access or optional caller-bound approvals.

Claude's plugin replaces detected values before forwarding. Command hooks let the task continue, attach
saved-name guidance, and attempt local transcript cleanup; the original prompt can still reach the model.
Only messages submitted by the user handle later approval replies.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# A reply to a waiting key request approves it when its intent is a clear yes, in any words: "ok, test that
# it works", "go ahead", "get this fixed and deploy it". A clear no turns it down. Anything unsure, or a reply
# that never answers (a question, an unrelated task), leaves the request waiting.
# "approve" as people type it in a hurry: aprove, apprve, approv, appove (owner, Nov 21: "make it work based on my
# intent, not the exact string").
APPROVE_WORD = r"a+p+r*o*v+e*[sd]?"
YES_START = re.compile(r"^\s*(?:" + APPROVE_WORD + r"|go(?:\s+go)*|yes|yep|yup|yeah|ya|y|ok|okay|k|kk|sure|fine|alright|all right|allow(?:ed)?"
                       r"|grant(?:ed)?|go ahead|go for it|do it|sounds good)\b")
GO_ON = re.compile(r"\b(?:" + APPROVE_WORD + r"|go(?! away)|yes|yep|yeah|sure|go ahead|go for it|do it|proceed|carry on|keep going|continue"
                   r"|ship it|use (?:it|them|the keys?|that key)|you can use|you have (?:my )?(?:ok|okay|permission|approval)"
                   r"|(?:get|have) (?:this|it|that|them) (?:fixed|done|working|deployed|shipped|running)"
                   r"|make sure (?:it|this|that)|run it|test (?:it|that|this)|that'?s fine|fine by me)\b")
NO_START = re.compile(r"^\s*(?:no|nope|nah|deny|denied|don't|do not|stop|cancel|never|not|hold off)\b")
REFUSE = re.compile(r"\b(?:deny|denied|reject(?:ed)?|refuse[sd]?|hold off|no way|never ?mind|cancel|don't go|do not go"
                    r"|not (?:that|this|those|these|yet|now|right now|for this)"
                    r"|(?:don't|do not|didn't|never|not|no) (?:" + APPROVE_WORD + r"|use|grant|give|allow|do (?:it|that)|touch|want|need))\b")
# Words that mean "not yet". A bare "not" or "no" elsewhere in a reply ("based on my intent, not the exact string")
# no longer blocks a clear yes: a real refusal of the key is caught by REFUSE and NO_START first.
UNSURE = re.compile(r"\b(?:wait|hold on|stop|cancel|maybe|later)\b")
FRIENDLY_NO = re.compile(r"\b(?:no problem|no worries|no prob|not a problem)\b")
# The word approve anywhere in a reply lets it answer a request up to 12 hours old.
APPROVE_ANY = re.compile(r"(?i)\b" + APPROVE_WORD + r"\b")
NOTICES = ("<task-notification", "<system-reminder", "<command-", "<local-command", "<bash-", "<user-memory",
           "Another Claude session", "[SYSTEM NOTIFICATION")
TIME = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\b")
IS_CODEX = "--codex" in sys.argv[1:]
AGENT = "Codex" if IS_CODEX else "Claude"
PRIVACY_NOTE = "The original paste may remain in chat history or with the model provider, even after saving the key."
# Accept a complete, explicit directive line, rather than guessing consent from task prose or key mentions.
NAMED_APPROVAL = re.compile(
    r"(?m)^[ \t]*(?i:approve|use|you['’]ll need(?: these keys)?|you (?:can|may) use|this thread (?:can|may) use)"
    r"[ \t:]+(?P<names>[A-Z][A-Z0-9_]*(?:(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+|[ \t]+)"
    r"[A-Z][A-Z0-9_]*)*)(?:[ \t]+(?i:for)[ \t]+(?P<duration>\d+(?:\.\d+)?[ \t]*"
    r"(?i:m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)))?[ \t]*[.!]?[ \t]*$"
)


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


def answer_in(text):
    """True if a reply approves a waiting request, False if it turns it down, None if it does neither."""
    if len(text) > 400:
        return None
    t = FRIENDLY_NO.sub("fine", re.sub(r"\s+", " ", text.lower().replace("’", "'")))
    if REFUSE.search(t) or NO_START.match(t):
        return False
    if UNSURE.search(t):
        return None
    if YES_START.match(t) or GO_ON.search(t):
        return True
    return None


def minutes_in(text):
    m = TIME.search(text)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    return n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)


def approve_named_keys(text, data, agent):
    """User-only explicit consent to saved names, scoped to this thread and a finite duration."""
    import approve
    import keys
    directives, fence = [], None
    for line in text.splitlines():
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if fence is None and (directive := NAMED_APPROVAL.fullmatch(line)):
            directives.append(directive)
    if not directives:
        return ""
    contexts = []
    saved = set(keys.existing_names())
    for directive in directives:
        names = re.findall(r"\b[A-Z][A-Z0-9_]*\b", directive["names"])
        # Unknown words/names invalidate the line, so qualifiers cannot accidentally expand consent.
        if not names or not set(names) <= saved:
            continue
        minutes = minutes_in(directive["duration"]) if directive["duration"] else approve.settings()[2]
        if minutes is None or minutes <= 0:
            continue
        allowed = approve.grant_from_prompt(names, directive[0], session_caller(data, agent), minutes)
        if allowed:
            if approve.settings()[0] == "auto":
                contexts.append("keywarden: approvals are off; ordinary saved keys are available automatically.")
            else:
                contexts.append(f"keywarden: approved {', '.join(allowed)} for this thread, including its subagents "
                                f"and subprocesses, for {approve.span(minutes)}. Confirm briefly, then continue. "
                                "Other saved keys still require approval.")
        pending = sorted(set(names) - set(allowed))
        if pending:
            contexts.append(f"keywarden: {', '.join(pending)} still requires the configured extra approval; "
                            "request it with keys grant before use.")
    return " ".join(contexts)


def totp_reply(text, data, named=""):
    """Totp mode: a waiting request is approved only by the current code from the person's authenticator app.
    A wrong or reused code says so and leaves the request waiting. A plain yes doesn't approve. A no still says no."""
    import approve
    import totp
    session = session_caller(data, AGENT) if data.get("session_id") else None
    window = approve.APPROVE_WORD_MINUTES  # a code is proof enough to answer a request up to 12 hours old
    if not approve.pending(window, session):
        if named:
            tell_agent(named)
        sys.exit(0)
    lead = (named + " ") if named else ""
    code = totp.code_in(text)
    if code:
        good, why = totp.verify(code)
        if not good:
            tell_agent(f"{lead}keywarden: the code didn't work ({why}). The key request is still waiting. Tell the "
                       "person, and ask them to reply with the current 6-digit code from their authenticator app.")
        minutes = minutes_in(totp.CODE.sub(" ", text))
        names = approve.answer_pending(True, minutes, window, session)
        span = approve.span(minutes or approve.settings()[2])
        tell_agent(f"{lead}keywarden: the person approved {', '.join(names)} for {span} with their authenticator "
                   "code. Run the command that asked for it again.")
    answer = answer_in(text)
    if answer is False:
        names = approve.answer_pending(False, max_age=window, session=session)
        tell_agent(f"{lead}keywarden: the person said no to {', '.join(names)}. Don't ask again.")
    if answer is True:
        tell_agent(f"{lead}keywarden: approvals need the code from the person's authenticator app, so a yes doesn't "
                   "approve. The key request is still waiting. Ask: \"Reply with the 6-digit code from your "
                   "authenticator app.\"")
    if named:
        tell_agent(named)
    sys.exit(0)


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
    """The thread the person typed into, shared with its subagents and subprocesses."""
    import approve
    return approve.session_caller(data, agent == "Codex")


def privacy_notice(data, agent):
    """Show the fallback privacy limitation once per session, separately from successful-save confirmation."""
    if not data.get("session_id"):
        return PRIVACY_NOTE
    import approve
    path = approve.STATE / "capture-notices.json"
    now = time.time()
    who = session_caller(data, agent)
    try:
        seen = approve._load(path, {})
        seen = {k: at for k, at in seen.items() if isinstance(at, (int, float)) and now - at < 30 * 86400}
        if who in seen:
            return None
        seen[who] = now
        approve._save(path, seen)
    except Exception:
        pass  # If the local record fails, display the note rather than silently suppressing it.
    return PRIVACY_NOTE


# An @-mention attaches a file. Claude Code reads it before any prompt hook runs and sends it with the message.
MENTION = re.compile(r'(?<!\S)@(?:"([^"\n]+)"(?:#L\d+(?:-\d+)?)?|([^\s"]+))')


def attached_keys(text, cwd=""):
    """Keys in files the message attaches with @: [((start, end), path, [(NAME, value, named)])].

    Looks for private key blocks and the well-known formats. A private key block is named after its file."""
    import keys
    out = []
    for m in MENTION.finditer(text):
        raw = re.sub(r"#L\d+(?:-\d+)?$", "", m.group(1) or m.group(2))
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(cwd or ".") / path
        try:
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stem = re.sub(r"^[0-9a-f]{8}-", "", path.stem)  # Claude's uploads folder puts 8 hex digits before the name
        stem = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").upper()
        name = stem if re.match(r"[A-Z]", stem) else "PRIVATE_KEY"
        found = [(name, keys.pem_value(b.group(0)), False) for b in keys.PEM.finditer(body) if keys.real_pem(b.group(0))]
        found += [(keys.PATTERNS[int(k.lastgroup[1:])][0], k.group(0), False)
                  for k in keys.KNOWN.finditer(body) if keys.plausible(k.group(0))]
        if found:
            out.append((m.span(), path, found))
    return out


def drop_upload(path):
    """Delete Claude's own copy of an uploaded file, so sending the message again can't attach the key."""
    parts = [p.lower() for p in path.parts]
    if any(a == ".claude" and b == "uploads" for a, b in zip(parts, parts[1:])):
        try:
            path.unlink()
        except OSError:
            pass


def replacement_plan(text, found, results, spans=()):
    """Return sanitized text and UTF-16 ranges for literal replacement by the JavaScript plugin.

    Ranges contain no secret values. UTF-16 offsets match JavaScript string slicing, including emoji.
    spans are (start, end, name) ranges to replace as well, such as an @-mention of a key file.
    """
    matches = list(spans)
    for (_, value, _), (name, _) in zip(found, results):
        start = 0
        while (at := text.find(value, start)) != -1:
            matches.append((at, at + len(value), name))
            start = at + len(value)
    selected, end = [], -1
    for start, stop, name in sorted(set(matches), key=lambda m: (m[0], -m[1])):
        if start >= end:
            selected.append((start, stop, name))
            end = stop
    edits = [{"start": len(text[:a].encode("utf-16-le")) // 2,
              "end": len(text[:b].encode("utf-16-le")) // 2, "name": name} for a, b, name in selected]
    for start, stop, name in reversed(selected):
        text = text[:start] + f"[{name}]" + text[stop:]
    return text, edits


def capture(data, agent):
    """Save detected values, then derive consent from the user's sanitized request. Never return values."""
    import approve
    import keys
    text = data.get("prompt") or ""
    notice_text = text.lstrip().startswith(NOTICES)
    found = [] if notice_text else keys.find_keys(text)
    attached = [] if notice_text else attached_keys(text, data.get("cwd") or "")
    if not found and not attached:
        return {"text": text, "saved": []}
    results = keys.save_found(found, text, data.get("cwd") or "", agent=agent)
    spans, files = [], []
    for (start, end), path, file_found in attached:
        saved = keys.save_found(file_found, f"attached file {path.name}", data.get("cwd") or "", agent=agent)
        results += saved
        found += file_found
        spans.append((start, end, saved[0][0]))
        files.append(path.name)
        drop_upload(path)
    text, edits = replacement_plan(text, found, results, spans)
    if keys.find_keys(text) or attached_keys(text, data.get("cwd") or ""):
        raise ValueError("a detected value remained after replacement")
    names = list(dict.fromkeys(name for name, _ in results))
    who = session_caller(data, agent)
    # Giving a thread a key approves its descendants too. Protected keys and phone mode still ask.
    approved = approve.grant_from_prompt(names, text, who)
    pending = [name for name in names if name not in approved]
    notice = f"Saved {', '.join(names)} to keywarden."
    span = approve.span(approve.paste_minutes())
    if approved:
        if approve.settings()[0] == "auto":
            notice += " Ready to use." if not pending else f" Ready to use: {', '.join(approved)}."
        else:
            notice += (f" Approved for this thread ({span})." if not pending
                       else f" Approved for this thread ({span}): {', '.join(approved)}.")
    if pending:
        notice += " Approval needed before use." if not approved else f" Approval still needed: {', '.join(pending)}."
    context = (f"keywarden: {notice} Confirm this in one short sentence in your normal reply, then continue their task. "
               "Do not quote hook names or implementation details. "
               f"Use the saved names {', '.join(names)}; never repeat or write the values. "
               "Thread approval includes its subagents and subprocesses, with the same expiry. "
               'Run keys status, then use "$NAME" in commands. ')
    if pending:
        context += f"{', '.join(pending)} has no new grant from this paste. Request approval with keys grant before use. "
    if files:
        context += (f"The person attached {', '.join(files)}, which held a key. keywarden took the file out of the "
                    "message, so you did not see it. ")
    context += "Local transcript redaction is best-effort; do not claim that all copies were erased."
    try:
        scrub_later(data.get("transcript_path"), [v for _, v, _ in found])
    except Exception:
        pass
    return {"text": text, "edits": edits, "saved": [list(r) for r in results], "approved": approved,
            "notice": notice, "context": context, "files": files}


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
        attached = attached_keys(text, data.get("cwd") or "")
    except BaseException:
        sys.exit(0)  # never get in the way of a message because of a bug here
    if attached:
        # The app read the file before this hook ran, so the only way to keep the key out of the chat is to stop
        # the message. Nothing from a stopped message reaches the model or the session log.
        try:
            out = capture(data, AGENT)
        except BaseException:
            block("keywarden couldn't finish saving the key in the attached file. Your message wasn't sent. "
                  "Check keys setup.")
        names = ", ".join(dict.fromkeys(name for name, _ in out["saved"]))
        block(f"keywarden saved {names} from {', '.join(out['files'])}. Your message wasn't sent, so the key "
              f"stays out of the chat. Send it again without the file and ask for {names} by name.")
    if found:
        try:
            out = capture(data, AGENT)
        except BaseException:
            block("keywarden couldn't finish saving or approving the key. Your message wasn't sent. Check keys setup.")
        disclosure = (f" {AGENT}'s prompt hook cannot replace your original message; the pasted value can reach "
                      "the model and local history. Local redaction is best-effort.")
        context = (out["context"] + " " + approve_named_keys(out["text"], data, AGENT) + disclosure +
                   " Any privacy note is handled separately by the app. "
                   "Do not repeat that note or the hook explanation in your reply.")
        tell_agent(context, privacy_notice(data, AGENT))
    try:
        import approve
        named = approve_named_keys(text, data, AGENT)
        if approve.settings()[0] == "totp":
            totp_reply(text, data, named)
        if named:
            tell_agent(named)
        # The word approve (or no) answers a request from up to 12 hours ago. A loose yes or ok only
        # answers one from the last 30 minutes, so it can't grant an old request by accident.
        answer = answer_in(text)
        window = approve.APPROVE_WORD_MINUTES if APPROVE_ANY.search(text) or answer is False else approve.PENDING_MINUTES
        session = session_caller(data, AGENT) if data.get("session_id") else None
        if answer is not None and approve.pending(window, session):
            if answer:
                minutes = minutes_in(text)
                names = approve.answer_pending(True, minutes, window, session)
                span = approve.span(minutes or approve.settings()[2])
                tell_agent(f"keywarden: the person approved {', '.join(names)} for {span}. "
                            "Run the command that asked for it again.")
            else:
                names = approve.answer_pending(False, max_age=window, session=session)
                tell_agent(f"keywarden: the person said no to {', '.join(names)}. Don't ask again.")
    except SystemExit:
        raise
    except BaseException:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
