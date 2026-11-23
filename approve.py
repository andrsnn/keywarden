"""Ask a person before a program gets a key: Windows Hello / Touch ID on the computer, and buttons on the phone.

Both ask at the same time. The first answer wins. A "yes" covers those keys for a few minutes (a grant).

Approvals are on by default. A grant belongs to one thread, including its subagents and subprocesses.
The hook gives commands a thread token in AGENT_KEYS_CALLER; keys.py checks its named keys and expiry.
A different Claude or Codex thread needs its own approval, even though storage is shared.
A command with no token (a person at a terminal) is the caller "terminal".
"""
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("AGENT_KEYS_STATE") or Path.home() / ".agent-keys")
GRANTS = STATE / "grants.json"  # {"callers": {caller: {name: when the approval ends}}}
CALLERS = STATE / "callers.json"  # {token: {"caller": caller, "at": time}}, written by the hook
TERMINAL = "terminal"
CALLER_DAYS = 30  # forget a caller token after this long
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_auth  # noqa: E402

POWERSHELL = (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                           "powershell.exe") if os.name == "nt" else "powershell")


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------- on the computer ----------

def touchid_bin():
    return local_auth.TouchID().argv()


def local_check():
    """'available', or why the computer can't ask."""
    a = local_auth.for_taps()
    return a.check() if a else "unavailable on this system"


def local_label():
    a = local_auth.for_taps()
    return a.label if a else "a check on this computer"


def _local_ask(reason, out, procs, timeout=300):
    a = local_auth.for_taps()
    out.put(("computer", a.ask(reason, timeout, procs) if a else "unavailable"))


# ---------- on the phone (phone_server.py over Tailscale) ----------

PHONE = STATE / "phone.json"
LOCAL_PORT = 8461


def phone_config():
    """True once a phone has a passkey for approvals."""
    return bool(_load(PHONE, {}).get("creds"))


def _local(path, body=None, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{LOCAL_PORT}{path}", method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Local-Secret": _load(PHONE, {}).get("local_secret", ""), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def server_version():
    import hashlib
    return hashlib.sha256((HERE / "phone_server.py").read_bytes()).hexdigest()[:12]


def ensure_server():
    """Start phone_server.py in the background if it isn't running (or is an older version)."""
    try:
        if _local("/health", timeout=2).get("v") == server_version():
            return
        _local("/local/quit", {}, timeout=3)
        time.sleep(1)
    except Exception:
        pass
    STATE.mkdir(exist_ok=True)
    logf = open(STATE / "phone-server.log", "a")
    kw = {"stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, str(HERE / "phone_server.py")], **kw)
    for _ in range(40):
        time.sleep(0.25)
        try:
            if _local("/health", timeout=1).get("v"):
                return
        except Exception:
            pass
    raise RuntimeError("the phone approval server didn't start. See ~/.agent-keys/phone-server.log")


def _phone_ask(reason, out, stop, timeout, rids):
    if not phone_config():
        out.put(("phone", "unavailable"))
        return
    try:
        ensure_server()
        rid = _local("/local/request", {"reason": reason, "durations": DURATIONS, "timeout": timeout})["rid"]
        rids.append(rid)
    except Exception as e:
        print(f"keys: couldn't send the request to your phone ({e})", file=sys.stderr)
        out.put(("phone", "unavailable"))
        return
    end = time.time() + timeout
    while time.time() < end and not stop.is_set():
        try:
            st = _local(f"/local/wait?rid={rid}", timeout=60)["status"]
        except Exception:
            stop.wait(3)
            continue
        if st == "no" or st.startswith("yes:"):
            out.put(("phone", st))
            return
        if st == "gone":
            break
    out.put(("phone", "timeout"))


# ---------- the decision ----------

DURATIONS = [15, 60, 240, 720, 1440]  # minutes the phone offers


def token_for(caller):
    """The token the hook puts in a command for this caller. Made on first use."""
    caller = thread_scope(caller)
    now = time.time()
    table = {t: c for t, c in _load(CALLERS, {}).items()
             if isinstance(c, dict) and now - c.get("at", 0) < CALLER_DAYS * 86400}
    for t, c in table.items():
        if c.get("caller") == caller:
            if now - c.get("at", 0) > 86400:  # keep a busy caller's token from aging out
                c["at"] = now
                _save(CALLERS, table)
            return t
    t = secrets.token_urlsafe(18)
    table[t] = {"caller": caller, "at": now}
    _save(CALLERS, table)
    return t


def caller(token=None):
    """Who is asking: the caller behind AGENT_KEYS_CALLER, or "terminal" when there is no token.
    An unknown token is nobody, so it gets no keys."""
    token = os.environ.get("AGENT_KEYS_CALLER", "") if token is None else token
    if not token:
        return TERMINAL
    c = _load(CALLERS, {}).get(token)
    return thread_scope(c["caller"]) if isinstance(c, dict) and c.get("caller") else "unknown:" + token[:6]


def thread_scope(who):
    """Old tokens can include /agent-id. They now use their parent thread's grants."""
    return who.split("/", 1)[0]


def session_caller(data, codex=False):
    """Both agents supply the parent session_id for subagent hooks; agent_id does not narrow a grant."""
    sid = data.get("session_id") or ("codex" if codex else "claude")
    return f"codex:{sid}" if codex else sid


def _all():
    """Every caller's grants that haven't run out: {caller: {name: expiry}}."""
    now = time.time()
    data = _load(GRANTS, {})
    out = {}
    for c, g in (data.get("callers", {}) if isinstance(data, dict) else {}).items():
        live = {k: v for k, v in (g or {}).items() if isinstance(v, (int, float)) and v > now}
        if live:
            out[c] = live
    return out


def _save(path, data):
    STATE.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))


def _set(names, expiry, who, replace=False):
    settings()  # Adopt the default policy before creating a new grant.
    who = thread_scope(who)
    a = _all()
    g = a.setdefault(who, {})
    for n in names:
        g[n] = expiry if replace else max(expiry, g.get(n, 0))
    _save(GRANTS, {"callers": a})


def grants(who=None):
    """Keys this caller may use right now: {name: when the approval ends}."""
    settings()  # Old implicit automatic grants must not survive the approval-on migration.
    return _all().get(caller() if who is None else thread_scope(who), {})


def all_granted():
    """Every key some caller may use now, with the latest end: for the 1Password copies."""
    settings()
    out = {}
    for g in _all().values():
        for n, v in g.items():
            out[n] = max(v, out.get(n, 0))
    return out


def callers_holding():
    """How many other callers hold grants, for keys status."""
    me = caller()
    return sum(1 for c in _all() if c != me)


def active(who=None):
    """True if this caller has any key approved right now."""
    return bool(grants(who))


def granted(names, who=None):
    """The keys in `names` that still need approval for this caller."""
    g = grants(who)
    return [n for n in names if n not in g]


def span(minutes):
    minutes = float(minutes)
    if minutes < 60:
        return f"{minutes:g} minutes"
    return f"{minutes / 60:g} hour{'s' if minutes != 60 else ''}"


def settings():
    """(mode, protected keys, minutes a grant lasts). mode is how a key without a grant gets one:
    "chat" (the default): the agent asks you in the chat and you reply with any clear yes.
    "phone": every key needs a tap on your phone (keys approvals phone).
    "totp": every key needs the 6-digit code from your authenticator app, typed in the chat (keys approvals totp).
    "auto": granted at once with no question (keys approvals off)."""
    cfg = _load(STATE / "config.json", {})
    if "approvals" not in cfg:
        # Previous versions implicitly allowed automatic access. Do not carry those grants into the
        # approval-on default. Record the choice once so subsequent reads preserve new approvals.
        lock(who="all")
        PENDING.unlink(missing_ok=True)
        cfg["approvals"] = "chat"
        _save(STATE / "config.json", cfg)
    mode = {True: "phone", False: "auto", "phone": "phone", "auto": "auto", "off": "auto",
            "chat": "chat", "totp": "totp"}.get(cfg.get("approvals", "chat"), "chat")
    return mode, set(cfg.get("protected", [])), cfg.get("auto_minutes", 60)


def allow_automatic(names, who=None):
    """With approval controls off, make ordinary keys usable without a user prompt.

    Caller records still support audit and explicitly protected keys. They impose no session boundary on
    ordinary keys in this mode: every caller, including subagents, receives access automatically.
    """
    mode, protected, minutes = settings()
    if mode != "auto":
        return []
    who = caller() if who is None else who
    allowed = sorted(set(names) - protected)
    need = granted(allowed, who)
    if need:
        _set(need, time.time() + minutes * 60, who)
        log_use(need, "automatic key access", f"allowed (approvals off) ({who})")
    return allowed


def needs_tap(names):
    """Which of these keys need a tap on the phone: all of them in phone mode, otherwise the protected ones.
    With no phone set up (and no Touch ID, or approver you turned on), a tap can't happen, so they're asked for
    in the chat instead."""
    mode, protected, _ = settings()
    if mode == "totp":  # the authenticator code is the second factor, for protected keys too
        return []
    if not phone_config() and sys.platform != "darwin" and not local_auth.chosen():
        return []
    return list(names) if mode == "phone" else [n for n in names if n in protected]


PENDING = STATE / "pending.json"
PENDING_MINUTES = 30  # a loose reply (yes, ok, sure) answers requests this recent
APPROVE_WORD_MINUTES = 12 * 60  # the word approve answers requests this recent, so a late reply still works
# A yes in the chat covers its session this long (owner, Nov 22: "when I say approve one time" it should hold).
CHAT_MINUTES = 12 * 60
STANDING = STATE / "standing.json"  # {session: {"until": time, "at": time, "advance": bool}}


def chat_minutes():
    """How long a yes in the chat lasts, unless the person names a time. "chat_minutes" in config.json changes it."""
    return _load(STATE / "config.json", {}).get("chat_minutes", CHAT_MINUTES)


def _standing_all():
    now = time.time()
    data = _load(STANDING, {})
    return {k: v for k, v in (data if isinstance(data, dict) else {}).items()
            if isinstance(v, dict) and isinstance(v.get("until"), (int, float)) and v["until"] > now}


def standing(who):
    """The session's standing chat approval, or None: {"until", "at", "advance"}."""
    return _standing_all().get(thread_scope(who)) if who else None


def set_standing(who, minutes, advance):
    """A yes in the chat covers every saved key this session asks for until it runs out (protected keys still
    need their tap). advance=True means the yes came before any request was waiting."""
    if not who or who == TERMINAL:
        return None
    who = thread_scope(who)
    table = _standing_all()
    if (table.get(who) or {}).get("advance") is False:
        advance = False  # once it has answered a request, keep calling it a chat approval
    until = time.time() + minutes * 60
    table[who] = {"until": until, "at": time.time(), "advance": advance}
    _save(STANDING, table)
    _renew(sorted(_all().get(who, {})), "a new approve in the chat", who)
    return until


def _drop_standing(who):
    table = {k: v for k, v in _standing_all().items() if who != "all" and k != thread_scope(who)}
    if table:
        _save(STANDING, table)
    else:
        STANDING.unlink(missing_ok=True)


def end_standing(who, why="the person said no in the chat"):
    """A no or stop from the person ends the session's standing approval and its grants at once."""
    if not who:
        return False
    who = thread_scope(who)
    had = who in _standing_all()
    _drop_standing(who)
    held = sorted(_all().get(who, {}))
    if held:
        lock(who=who)
    if had or held:
        log_use(held or ["(standing approval)"], why, f"ended ({who})")
    return had or bool(held)


def asked_from(r, session):
    """Whether this request came from the session the person is typing in: the session itself, one of its
    subagents, or a terminal with no session. session None means any session."""
    who = r.get("caller", TERMINAL)
    return session is None or who in (session, TERMINAL) or who.startswith(session + "/")


def pending(max_minutes=PENDING_MINUTES, session=None):
    """Requests waiting for your OK in the chat: [{names, what, at, caller}] from the last max_minutes,
    only those from `session` (and its subagents) when it is given."""
    now = time.time()
    return [r for r in _load(PENDING, []) if isinstance(r, dict) and now - r.get("at", 0) < max_minutes * 60
            and asked_from(r, session)]


def add_pending(names, what, who=None):
    who = caller() if who is None else who
    reqs = [r for r in pending(APPROVE_WORD_MINUTES)
            if not (set(r["names"]) == set(names) and r.get("caller", TERMINAL) == who)]
    reqs.append({"names": sorted(names), "what": what[:200], "at": time.time(), "caller": who})
    STATE.mkdir(exist_ok=True)
    PENDING.write_text(json.dumps(reqs))


def answer_pending(yes, minutes=None, max_age=PENDING_MINUTES, session=None):
    """Called by the message hook when you reply approve or no in a session. It answers only that session's
    requests (and its subagents'); requests from other sessions keep waiting. Returns the key names."""
    reqs = pending(max_age, session)
    rest = [r for r in pending(APPROVE_WORD_MINUTES) if not asked_from(r, session)]
    if rest:
        PENDING.write_text(json.dumps(rest))
    else:
        PENDING.unlink(missing_ok=True)
    names = sorted({n for r in reqs for n in r["names"]})
    if yes and names:
        minutes = minutes or (chat_minutes() if settings()[0] == "chat" else settings()[2])  # totp keeps its hour
        for r in reqs:  # each request's keys go only to the caller that asked
            _set(r["names"], time.time() + minutes * 60, r.get("caller", TERMINAL))
            log_use(r["names"], r["what"], f"approved in the chat for {span(minutes)} ({r.get('caller', TERMINAL)})")
    if yes and names and session and settings()[0] == "chat":
        set_standing(session, minutes, advance=False)  # later asks from this session go straight through
    if not yes and session:
        end_standing(session)
    return names


def approve_in_advance(session, minutes=None):
    """A clear yes in the chat with nothing waiting: the session's next keys grant goes straight through.
    Returns when it ends, or None when it can't apply (no session, or a mode where a chat yes doesn't approve)."""
    if not session or settings()[0] != "chat":
        return None
    minutes = minutes or chat_minutes()
    until = set_standing(session, minutes, advance=True)
    if until:
        log_use(["(any saved key)"], "a yes in the chat with no request waiting",
                f"approved in advance in the chat for {span(minutes)} ({thread_scope(session)})")
    return until


def _covered(names, what, who):
    """Keys a standing chat approval lets through now. Protected keys are never covered: they keep their tap."""
    st = standing(who)
    if not st or settings()[0] != "chat":
        return []
    ok = sorted(set(names) - settings()[1])
    if ok:
        _set(ok, st["until"], who)
        how = "approved in advance in the chat" if st.get("advance") else "covered by an approve in the chat"
        log_use(ok, what, f"{how}, {_left_span(who)} left ({thread_scope(who)})")
    return ok


def _renew(names, what, who):
    """Keys this session already holds run to the end of its standing chat approval, so a grant made hours ago
    doesn't run out while the person's approve still holds. Protected keys keep their own end."""
    st = standing(who)
    if not st or settings()[0] != "chat":
        return []
    g = grants(who)
    late = sorted(n for n in set(names) - settings()[1] if n in g and g[n] < st["until"])
    if late:
        _set(late, st["until"], who)
        log_use(late, what, f"renewed by the approve in the chat, {_left_span(who)} left ({thread_scope(who)})")
    return late


def _left_span(who):
    st = standing(who)
    return span(max(1, round((st["until"] - time.time()) / 60))) if st else "a while"


def log_use(names, what, how):
    try:
        STATE.mkdir(exist_ok=True)
        with open(STATE / "use.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {how}: {', '.join(names)} for: {what[:200]}\n")
    except OSError:
        pass


PASTE_MINUTES = 12 * 60  # a session that was given a key can use it this long


def paste_minutes():
    return _load(STATE / "config.json", {}).get("paste_minutes", PASTE_MINUTES)


def grant_from_prompt(names, what, who=TERMINAL, minutes=None):
    """Giving a thread a key approves that thread and its descendants, not other threads.
    Protected keys and phone mode still ask.

    Called by prompt capture or explicit named consent. `what` contains names, never secret values.
    """
    mode, protected, _ = settings()
    if mode == "auto":
        return allow_automatic(names, who)
    allowed = sorted(set(names) - protected) if mode not in ("phone", "totp") else []
    if not allowed:
        return []
    explicit_duration = minutes is not None
    minutes = paste_minutes() if minutes is None else minutes
    _set(allowed, time.time() + minutes * 60, who, replace=explicit_duration)
    log_use(allowed, what, f"given in the chat, so approved for {span(minutes)} ({who})")
    return allowed


def ask(names, what, minutes=60, timeout=300, force=False):
    """Return True once a person approves the keys in `names` for `what`.

    On the phone the person picks how long the approval lasts. Windows Hello or Touch ID on the computer
    approves for `minutes`. The approval covers only these keys.
    """
    who = caller()
    if not force:  # under a standing chat approval, keys already held are renewed to its end
        _renew(names, what, who)
    need = granted(names, who)
    if not need:
        return True
    mode, _, auto_minutes = settings()
    tap = list(need) if force == "phone" else needs_tap(need)
    rest = [n for n in need if n not in tap]
    if rest and mode == "auto" and not force:  # approvals off: grant these now and note it in the log
        _set(rest, time.time() + auto_minutes * 60, who)
        log_use(rest, what, f"allowed (approvals off) ({who})")
        rest = []
    if rest and mode == "chat" and not force:  # the person already said yes in this session's chat
        covered = _covered(rest, what, who)
        if covered:
            print(f"keys: {', '.join(covered)} approved, because the person already said yes in this chat "
                  f"(good for {_left_span(who)} more). Don't ask them again.", file=sys.stderr)
        rest = [n for n in rest if n not in covered]
    if rest and mode == "chat" and not force:  # keys in 1Password: unlocking 1Password is the approval
        unlocked = _unlock_1password(rest, what, who, minutes)
        rest = [n for n in rest if n not in unlocked]
    if rest and mode == "chat" and not force:  # Touch ID (or your own approver), if you turned it on
        local, denied = _local_approve(rest, what, who, minutes)
        if denied:
            return False
        rest = [n for n in rest if n not in local]
    if rest and mode == "totp":  # the person types the authenticator code: here, or in the chat
        return _totp_approve(rest, what, who, minutes)
    if rest:  # ask in the chat: the message hook grants these when the person replies approve
        add_pending(rest, what, who)
        protected = sorted(set(rest) & settings()[1])
        print(f"keys: {', '.join(rest)} needs your OK. Ask the person once: \"I need {', '.join(rest)}. Reason: "
              f"{what}. Reply approve.\" Then end your turn, and run this command again after they reply. One "
              f"approve covers every saved key this session asks for in the next {span(chat_minutes())}"
              + (f", except {', '.join(protected)}, which is protected" if protected else "")
              + ". Never ask again for a key keys status lists as granted.", file=sys.stderr)
        log_use(rest, what, "asked in the chat")
        return False
    if not tap:  # everything was approved above (1Password unlock, or approvals off)
        return True
    need = tap
    reason =f"{', '.join(need)} for: {what}"[:300] + f" (on {socket.gethostname()})"
    out, stop, procs, rids = queue.Queue(), threading.Event(), [], []
    threads = [threading.Thread(target=_local_ask, args=(reason, out, procs, timeout), daemon=True),
               threading.Thread(target=_phone_ask, args=(reason, out, stop, timeout, rids), daemon=True)]
    for t in threads:
        t.start()
    where = local_label()
    print(f"keys: waiting for approval of {', '.join(need)} ({'your phone, or ' if phone_config() else ''}"
          f"{where} on this computer)...", file=sys.stderr)
    answers, ans, src = {}, None, "computer"
    while len(answers) < 2:
        try:
            src, ans = out.get(timeout=timeout + 30)
        except queue.Empty:
            break
        answers[src] = ans
        if ans == "yes" or ans == "no" or ans.startswith("yes:"):
            break
    stop.set()
    for p in procs:
        if p.poll() is None:
            p.kill()
    for rid in rids:  # take the request off the phone if the computer answered first
        try:
            _local("/local/cancel", {"rid": rid}, timeout=3)
        except Exception:
            pass
    if ans and ans.startswith("yes"):
        chosen = float(ans.split(":", 1)[1]) if ":" in ans else minutes
        _set(need, time.time() + chosen * 60, who)
        print(f"keys: {', '.join(need)} approved on the {src} for {span(chosen)}.", file=sys.stderr)
        log_use(need, what, f"approved on the {src} for {span(chosen)}")
        return True
    if all(a in ("unavailable", "timeout") for a in answers.values()):
        print("keys: nobody approved. Set up your phone with `keys phone`, or turn on "
              f"{where} on this computer.", file=sys.stderr)
    else:
        print(f"keys: denied on the {src}.", file=sys.stderr)
    return False


def own_terminal():
    """True when a person is typing at their own terminal, not an agent's command. Agents' commands carry a
    thread token (AGENT_KEYS_CALLER, which the hook stops them from clearing), Claude Code marks its commands
    with CLAUDECODE, and neither has a keyboard on stdin."""
    if any(os.environ.get(v) for v in ("AGENT_KEYS_CALLER", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_THREAD_ID")):
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _totp_approve(names, what, who, minutes):
    """Totp mode. At the person's own terminal, ask for the code right there. From an agent, leave the request
    waiting: the message hook approves it when the person replies in the chat with the current code."""
    import totp
    if not totp.is_set_up():
        print("keys: approvals need an authenticator code, but no authenticator is set up. In your own terminal, "
              "run keys totp setup (or keys approvals on to approve in the chat).", file=sys.stderr)
        return False
    if who == TERMINAL and own_terminal():
        import getpass
        try:
            code = getpass.getpass(f"Code from your authenticator app to approve {', '.join(names)}: ")
        except (EOFError, KeyboardInterrupt):
            return False
        good, why = totp.verify(code)
        if not good:
            print(f"keys: {why}. Nothing was approved.", file=sys.stderr)
            log_use(names, what, f"wrong authenticator code ({who})")
            return False
        _set(names, time.time() + minutes * 60, who)
        print(f"keys: {', '.join(names)} approved with your authenticator code for {span(minutes)}.", file=sys.stderr)
        log_use(names, what, f"approved with an authenticator code for {span(minutes)} ({who})")
        return True
    add_pending(names, what, who)
    print(f"keys: {', '.join(names)} needs the code from the person's authenticator app. Ask the person: \"I need "
          f"{', '.join(names)}. Reason: {what}. Reply with the 6-digit code from your authenticator app (add a time "
          "like 2h to choose how long it lasts).\" A yes without the code doesn't approve. Then end your turn, and "
          "run this command again after they reply.", file=sys.stderr)
    log_use(names, what, "asked for an authenticator code in the chat")
    return False


def _local_approve(names, what, who, minutes):
    """With a local approver turned on (keys touchid on), ask it first. Returns (approved names, denied).
    A tap approves. Cancel is a no. If it can't ask, or nobody answers in time, the chat asks instead."""
    a = local_auth.enabled()
    if not a:
        return [], False
    wait = local_auth.timeout()
    print(f"keys: approve {', '.join(names)} with {a.label} on this computer (the chat asks instead after "
          f"{wait:g} seconds)...", file=sys.stderr)
    reason = f"use {', '.join(names)} for: {what}"[:300]
    ans = a.ask(reason, wait)
    if ans == "yes":
        _set(names, time.time() + minutes * 60, who)
        print(f"keys: {', '.join(names)} approved with {a.label} for {span(minutes)}.", file=sys.stderr)
        log_use(names, what, f"approved with {a.label} for {span(minutes)} ({who})")
        return list(names), False
    if ans == "no":
        print(f"keys: {', '.join(names)} denied with {a.label}. Tell the person it was denied. They can still "
              f"approve in the chat with: approve {' '.join(names)}", file=sys.stderr)
        log_use(names, what, f"denied with {a.label} ({who})")
        return [], True
    why = "nobody answered" if ans == "timeout" else "it isn't available"
    print(f"keys: {a.label} didn't approve ({why}). Asking in the chat instead.", file=sys.stderr)
    log_use(names, what, f"{a.label}: {why}, asked in the chat ({who})")
    return [], False


def _unlock_1password(names, what, who, minutes):
    """With keys in 1Password, ask the person to unlock 1Password for these keys. Returns the keys it approved.
    Turn it off with "unlock_1password": false in config.json; then 1Password users approve in the chat."""
    sys.path.insert(0, str(HERE))
    import stores
    store = stores.current()
    if store.kind != "1password" or not _load(STATE / "config.json", {}).get("unlock_1password", True):
        return []
    print(f"keys: unlock 1Password to let this session use {', '.join(names)}...", file=sys.stderr)
    ok, why = store.unlock(names)
    if not ok:
        print(f"keys: 1Password didn't approve ({why}). Asking in the chat instead.", file=sys.stderr)
        log_use(names, what, f"1Password unlock didn't approve: {why[:80]} ({who})")
        return []
    _set(names, time.time() + minutes * 60, who)
    print(f"keys: {', '.join(names)} approved by unlocking 1Password for {span(minutes)}.", file=sys.stderr)
    log_use(names, what, f"approved by unlocking 1Password for {span(minutes)} ({who})")
    return list(names)


def lock(names=None, who="all", keep_standing=False):
    """End the approval for these keys (or every key), for one caller or for every caller. keep_standing leaves the
    person's standing chat approval in place: an agent's keys lock clears its grants, but the approve is the
    person's to withdraw."""
    a = _all()
    for c in list(a):
        if who != "all" and thread_scope(c) != thread_scope(who):
            continue
        a[c] = {k: v for k, v in a[c].items() if names and k not in names}
        if not a[c]:
            del a[c]
    if a:
        _save(GRANTS, {"callers": a})
    else:
        GRANTS.unlink(missing_ok=True)
    if not names and not keep_standing:  # the person locking every key also ends the standing chat approval
        _drop_standing(who)
