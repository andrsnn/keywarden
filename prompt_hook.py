#!/usr/bin/env python3
"""Save keys supplied in user prompts, with automatic access or optional caller-bound approvals.

Claude's plugin replaces detected values before forwarding. Command hooks let the task continue, attach
saved-name guidance, and attempt local transcript cleanup; the original prompt can still reach the model.
Only messages submitted by the user handle later approval replies.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# What a chat message means for key approvals (yes, no, how long, which keys by name) is read by a language model
# in intent.py, never by word lists (owner, Nov 22: "Delete any hard-coded string matching check in keywarden and
# make it all LLM based"). When no model answers, the message changes nothing.
NOTICES = ("<task-notification", "<system-reminder", "<command-", "<local-command", "<bash-", "<user-memory",
           "Another Claude session", "[SYSTEM NOTIFICATION")
IS_CODEX = "--codex" in sys.argv[1:]
AGENT = "Codex" if IS_CODEX else "Claude"
PRIVACY_NOTE = "The original paste may remain in chat history or with the model provider, even after saving the key."
UNAVAILABLE = ("keywarden: the model that reads key approvals couldn't be reached ({why}), so this message changed "
               "no key approvals. Approvals and grants stay as they were. If the person approved or refused keys in "
               "it, ask them to send that again.")


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


def last_agent_message(path, limit=2_000_000):
    """The agent's last message to the person, from the session log the hook input names. "" when it can't be read.
    Reads Claude Code's log (type assistant) and Codex's (response_item with an assistant message)."""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - limit))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError):
        return ""
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        kind = e.get("type")
        msg = e.get("message") if kind == "assistant" else e.get("payload") if kind == "response_item" else None
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        body = content if isinstance(content, str) else "\n".join(
            c.get("text") or "" for c in (content or []) if isinstance(c, dict) and c.get("type") in ("text", "output_text"))
        if body.strip():
            return body.strip()[-3000:]
    return ""


def held_by(session):
    """Whether the session holds a standing approval or any grant right now."""
    import approve
    return bool(session and (approve.standing(session) or approve.grants(session)))


def approve_named(reading, data, agent):
    """Keys the person approved by name in the message, for this thread. Names that aren't saved are dropped.
    Returns what to tell the agent, or ""."""
    import approve
    import keys
    names = sorted(set(reading["keys"]))
    # A name that isn't saved drops the whole approval, so a stray word can't widen it.
    if not names or not set(names) <= set(keys.existing_names()):
        return ""
    minutes = reading["minutes"] or approve.chat_minutes()
    allowed = approve.grant_from_prompt(names, "approved by name in the chat: " + ", ".join(names),
                                        session_caller(data, agent), minutes)
    contexts = []
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


def named_in(text, data, agent):
    """Keys approved by name in a message that also carried a pasted key."""
    import approve
    import intent
    if approve.settings()[0] == "auto":
        return ""
    session = session_caller(data, agent) if data.get("session_id") else None
    try:
        reading = intent.read(text, [], None, held_by(session))
    except intent.Unavailable as e:
        return UNAVAILABLE.format(why=e)
    return approve_named(reading, data, agent)


def totp_reply(text, data, session):
    """Totp mode: a waiting request is approved only by the current code from the person's authenticator app.
    A wrong or reused code says so and leaves the request waiting. A plain yes doesn't approve. A no still says no."""
    import approve
    import intent
    import totp
    window = approve.APPROVE_WORD_MINUTES  # a code is proof enough to answer a request up to 12 hours old
    waiting = approve.pending(window, session)
    if not waiting:
        sys.exit(0)
    code = totp.code_in(text)
    try:  # the code itself is taken out before the model reads the message
        reading = intent.read(totp.CODE.sub(" ", text), waiting, last_agent_message(data.get("transcript_path")),
                              held_by(session))
    except intent.Unavailable as e:
        reading, why = None, e
    if code:
        good, why_bad = totp.verify(code)
        if not good:
            tell_agent(f"keywarden: the code didn't work ({why_bad}). The key request is still waiting. Tell the "
                       "person, and ask them to reply with the current 6-digit code from their authenticator app.")
        minutes = reading["minutes"] if reading else None
        names = approve.answer_pending(True, minutes, window, session)
        span = approve.span(minutes or approve.settings()[2])
        tell_agent(f"keywarden: the person approved {', '.join(names)} for {span} with their authenticator "
                   "code. Run the command that asked for it again.")
    if reading is None:
        tell_agent(UNAVAILABLE.format(why=why))
    if reading["answer"] is False:
        names = approve.answer_pending(False, max_age=window, session=session)
        tell_agent(f"keywarden: the person said no to {', '.join(names)}. Don't ask again.")
    if reading["answer"] is True:
        tell_agent("keywarden: approvals need the code from the person's authenticator app, so a yes doesn't "
                   "approve. The key request is still waiting. Ask: \"Reply with the 6-digit code from your "
                   "authenticator app.\"")
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


# A name the agent gave the key it asked for: "save it as NAME", "keys add NAME", "send me NAME".
AGENT_NAME = re.compile(r"(?:\bas|keys add|\bsend(?: me)?|\bpaste|\bneed)\s+(?:the\s+|your\s+)?[`*\"']*"
                        r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")


def sort_out(text, found, agent_msg):
    """Keep the secrets and drop public IDs. A value that may be an ID (hex digits, a GUID, or one the message calls
    an ID) is kept only when the model reads it as a secret the person wants saved; with no model, it's dropped.
    A key sent with no name takes the name the model, or the agent's last message, gave it, and replaces a saved
    key of that name. Returns (kept, dropped values)."""
    import intent
    import keys
    doubt = [i for i, (name, v, _) in enumerate(found) if keys.doubtful(text, v, name)]
    unnamed = [i for i, (name, _, named) in enumerate(found) if not named and name == keys.PLACEHOLDER]
    verdict = {}
    if doubt:
        masked = text
        for i, (_, v, _) in enumerate(found):
            masked = masked.replace(v, f"[VALUE{i + 1}]")
        items = [{"label": f"VALUE{i + 1}", "shape": keys.shape(found[i][1]),
                  "name": None if found[i][0] == keys.PLACEHOLDER else found[i][0]} for i in doubt]
        try:
            verdict = intent.classify(masked, items, agent_msg)
        except intent.Unavailable:
            verdict = {}
    named_by_agent = AGENT_NAME.findall(agent_msg or "")
    kept, dropped = [], []
    for i, (name, v, named) in enumerate(found):
        said = verdict.get(f"VALUE{i + 1}") or {}
        if i in doubt and said.get("secret") is not True:
            dropped.append(v)
            continue
        if i in unnamed:
            pick = said.get("name") or (named_by_agent[-1] if len(unnamed) == 1 and named_by_agent else None)
            if pick:
                name, named = pick, True  # the agent asked for exactly this key, so it replaces an older value
        kept.append((name, v, named))
    return kept, dropped


def capture(data, agent):
    """Save detected values, then derive consent from the user's sanitized request. Never return values."""
    import approve
    import keys
    text = data.get("prompt") or ""
    notice_text = text.lstrip().startswith(NOTICES)
    found = [] if notice_text else keys.find_keys(text)
    attached = [] if notice_text else attached_keys(text, data.get("cwd") or "")
    dropped = []
    if found:
        found, dropped = sort_out(text, found, last_agent_message(data.get("transcript_path")))
    if not found and not attached:
        return {"text": text, "saved": []}
    who = session_caller(data, agent)
    results = keys.save_found(found, text, data.get("cwd") or "", agent=agent, session=who)
    spans, files = [], []
    for (start, end), path, file_found in attached:
        saved = keys.save_found(file_found, f"attached file {path.name}", data.get("cwd") or "", agent=agent)
        results += saved
        found += file_found
        spans.append((start, end, saved[0][0]))
        files.append(path.name)
        drop_upload(path)
    text, edits = replacement_plan(text, found, results, spans)
    if [f for f in keys.find_keys(text) if f[1] not in dropped] or attached_keys(text, data.get("cwd") or ""):
        raise ValueError("a detected value remained after replacement")
    names = list(dict.fromkeys(name for name, _ in results))
    replaced = [name for name, how in results if how == "replaced"]
    # Giving a thread a key approves its descendants too. Protected keys and phone mode still ask.
    approved = approve.grant_from_prompt(names, text, who)
    pending = [name for name in names if name not in approved]
    notice = f"Saved {', '.join(names)} to keywarden."
    if replaced:
        notice += f" It replaced the old value of {', '.join(replaced)}."
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
    if keys.PLACEHOLDER in " ".join(names):
        context += (f"The key came with no name, so it's saved as {', '.join(n for n in names if keys.PLACEHOLDER in n)}"
                    ". Rename it with keys rename OLD NEW (no approval needed for your own save from this session). ")
    if dropped:
        context += ("keywarden didn't save the other values in the message: they look like public IDs, not secrets. "
                    "You may use them as written. ")
    context += ("If keywarden saved something that isn't a secret, run keys forget NAME (no approval needed for a "
                "save from this session). ")
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


def no_request_reply(reading, session):
    """Nothing is waiting in this session. A yes approves in advance: the session's next keys grant goes straight
    through. A no that the model read as withdrawing key permission ends what the session holds. A "stop" about
    other work reads as UNCLEAR and ends nothing."""
    import approve
    if approve.settings()[0] != "chat":
        return
    if reading["answer"] is True:
        minutes = reading["minutes"] or approve.chat_minutes()
        if approve.approve_in_advance(session, minutes):
            tell_agent(f"keywarden: the person approved key use in advance for this session for {approve.span(minutes)}. "
                       "Run keys grant NAME --for \"why\" for the keys you need now: it goes straight through. "
                       "Don't ask the person again. Protected keys still need their tap.")
    elif reading["answer"] is False and held_by(session):
        approve.end_standing(session, "the person withdrew key permission in the chat")
        tell_agent("keywarden: the person withdrew permission to use keys, so this session's key approvals ended. "
                   "Don't use keys until they approve again.")


def main():
    if os.environ.get("KEYWARDEN_INTENT_CALL"):
        sys.exit(0)  # this is keywarden's own model call reading a reply; nothing to do
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
        found = out["saved"]  # empty when every value turned out to be a public ID
    if found:
        disclosure = (f" {AGENT}'s prompt hook cannot replace your original message; the pasted value can reach "
                      "the model and local history. Local redaction is best-effort.")
        context = (out["context"] + " " + named_in(out["text"], data, AGENT) + disclosure +
                   " Any privacy note is handled separately by the app. "
                   "Do not repeat that note or the hook explanation in your reply.")
        tell_agent(context, privacy_notice(data, AGENT))
    try:
        import approve
        import intent
        mode = approve.settings()[0]
        if mode == "auto":
            sys.exit(0)  # approvals are off: nothing a message says changes key access
        session = session_caller(data, AGENT) if data.get("session_id") else None
        if mode == "totp":
            totp_reply(text, data, session)
        # A request from up to 12 hours ago can still be answered.
        waiting = approve.pending(approve.APPROVE_WORD_MINUTES, session)
        if not waiting and mode != "chat":
            sys.exit(0)
        try:
            reading = intent.read(text, waiting, last_agent_message(data.get("transcript_path")), held_by(session))
        except intent.Unavailable as e:
            tell_agent(UNAVAILABLE.format(why=e))
        named = approve_named(reading, data, AGENT)
        if named:
            tell_agent(named)
        if not waiting:
            if session:
                no_request_reply(reading, session)
            sys.exit(0)
        if reading["answer"] is True:
            minutes = reading["minutes"]
            names = approve.answer_pending(True, minutes, approve.APPROVE_WORD_MINUTES, session)
            span = approve.span(minutes or approve.chat_minutes())
            more = (" That approve also covers any other saved key this session asks for in that time (not "
                    "protected keys): keys grant lets it straight through, so don't ask again."
                    if session and mode == "chat" else "")
            tell_agent(f"keywarden: the person approved {', '.join(names)} for {span}.{more} "
                       "Run the command that asked for it again.")
        elif reading["answer"] is False:
            names = approve.answer_pending(False, max_age=approve.APPROVE_WORD_MINUTES, session=session)
            tell_agent(f"keywarden: the person said no to {', '.join(names)}. Don't ask again.")
    except SystemExit:
        raise
    except BaseException:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
