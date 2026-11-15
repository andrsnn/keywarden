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

WIN_HELLO = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$m = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($op, $t) { $task = $m.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
$V = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType=WindowsRuntime]
$a = Await ($V::CheckAvailabilityAsync()) ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
if ("$a" -ne 'Available') { Write-Output "unavailable $a"; exit }
if ($env:AGENT_KEYS_CHECK_ONLY) { Write-Output 'available'; exit }
$r = Await ($V::RequestVerificationAsync($env:AGENT_KEYS_REASON)) ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
Write-Output "$r"
"""


POWERSHELL = (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                           "powershell.exe") if os.name == "nt" else "powershell")


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------- on the computer ----------

def touchid_bin():
    b = STATE / "touchid"
    return [str(b)] if b.exists() else (["swift", str(HERE / "touchid.swift")] if shutil.which("swift") else None)


def local_check():
    """'available', or why the computer can't ask."""
    if os.name == "nt":
        env = dict(os.environ, AGENT_KEYS_CHECK_ONLY="1")
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", WIN_HELLO],
                           capture_output=True, text=True, env=env, timeout=60)
        return (r.stdout.strip() or r.stderr.strip() or "unavailable").splitlines()[-1]
    if sys.platform == "darwin":
        cmd = touchid_bin()
        if not cmd:
            return "unavailable: install Xcode command line tools (xcode-select --install), then rerun install.py"
        r = subprocess.run(cmd + ["--check"], capture_output=True, text=True, timeout=120)
        return r.stdout.strip() or "unavailable"
    return "unavailable on this system"


def _local_ask(reason, out, procs):
    if os.name == "nt":
        p = subprocess.Popen([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", WIN_HELLO],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             env=dict(os.environ, AGENT_KEYS_REASON=reason))
    elif sys.platform == "darwin" and touchid_bin():
        p = subprocess.Popen(touchid_bin() + [reason], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    else:
        out.put(("computer", "unavailable"))
        return
    procs.append(p)
    res = (p.communicate()[0] or "").strip().splitlines()
    res = res[-1] if res else "unavailable"
    if res == "Verified":
        out.put(("computer", "yes"))
    elif res.startswith("unavailable") or p.returncode not in (0, None):
        out.put(("computer", "unavailable"))
    else:  # Canceled, RetriesExhausted, DeviceBusy...
        out.put(("computer", "no"))


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
    "chat" (the default): the agent asks you in the chat and you reply approve.
    "phone": every key needs a tap on your phone (keys approvals phone).
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
            "chat": "chat"}.get(cfg.get("approvals", "chat"), "chat")
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
    With no phone set up (and no Touch ID), a tap can't happen, so they're asked for in the chat instead."""
    mode, protected, _ = settings()
    if not phone_config() and sys.platform != "darwin":
        return []
    return list(names) if mode == "phone" else [n for n in names if n in protected]


PENDING = STATE / "pending.json"
PENDING_MINUTES = 30  # a loose reply (yes, ok, sure) answers requests this recent
APPROVE_WORD_MINUTES = 12 * 60  # the word approve answers requests this recent, so a late reply still works


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
        minutes = minutes or settings()[2]
        for r in reqs:  # each request's keys go only to the caller that asked
            _set(r["names"], time.time() + minutes * 60, r.get("caller", TERMINAL))
            log_use(r["names"], r["what"], f"approved in the chat for {span(minutes)} ({r.get('caller', TERMINAL)})")
    return names


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
    allowed = sorted(set(names) - protected) if mode != "phone" else []
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
    if rest and mode == "chat" and not force:  # keys in 1Password: unlocking 1Password is the approval
        unlocked = _unlock_1password(rest, what, who, minutes)
        rest = [n for n in rest if n not in unlocked]
    if rest:  # ask in the chat: the message hook grants these when the person replies approve
        add_pending(rest, what, who)
        print(f"keys: {', '.join(rest)} needs your OK. Ask the person: \"I need {', '.join(rest)}. Reason: {what}. "
              f"Reply approve, or approve 1h.\" Then end your turn, and run this command again after they "
              "approve.", file=sys.stderr)
        log_use(rest, what, "asked in the chat")
        return False
    if not tap:  # everything was approved above (1Password unlock, or approvals off)
        return True
    need = tap
    reason =f"{', '.join(need)} for: {what}"[:300] + f" (on {socket.gethostname()})"
    out, stop, procs, rids = queue.Queue(), threading.Event(), [], []
    threads = [threading.Thread(target=_local_ask, args=(reason, out, procs), daemon=True),
               threading.Thread(target=_phone_ask, args=(reason, out, stop, timeout, rids), daemon=True)]
    for t in threads:
        t.start()
    where = "Windows Hello" if os.name == "nt" else "Touch ID"
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


def lock(names=None, who="all"):
    """End the approval for these keys (or every key), for one caller or for every caller."""
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
