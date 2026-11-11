"""Ask a person before a program gets a key: Windows Hello / Touch ID on the computer, and buttons on the phone.

Both ask at the same time. The first answer wins. A "yes" covers those keys for a few minutes (a grant).
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
GRANTS = STATE / "grants.json"
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


def grants():
    """Keys approved right now: {name: when the approval ends}."""
    now = time.time()
    return {k: v for k, v in _load(GRANTS, {}).items() if isinstance(v, (int, float)) and v > now}


def active():
    """True if any key is approved right now."""
    return bool(grants())


def granted(names):
    """The keys in `names` that still need approval."""
    g = grants()
    return [n for n in names if n not in g]


def span(minutes):
    minutes = float(minutes)
    if minutes < 60:
        return f"{minutes:g} minutes"
    return f"{minutes / 60:g} hour{'s' if minutes != 60 else ''}"


def settings():
    """(mode, protected keys, minutes a grant lasts). mode is how a key without a grant gets one:
    "chat" (the default): Claude asks you in the chat and you reply approve.
    "phone": every key needs a tap on your phone (keys approvals phone).
    "auto": granted at once with no question (keys approvals off)."""
    cfg = _load(STATE / "config.json", {})
    mode = {True: "phone", "phone": "phone", "auto": "auto"}.get(cfg.get("approvals"), "chat")
    return mode, set(cfg.get("protected", [])), cfg.get("auto_minutes", 720)


def needs_tap(names):
    """Which of these keys need a tap on the phone: all of them in phone mode, otherwise the protected ones.
    With no phone set up (and no Touch ID), a tap can't happen, so they're asked for in the chat instead."""
    mode, protected, _ = settings()
    if not phone_config() and sys.platform != "darwin":
        return []
    return list(names) if mode == "phone" else [n for n in names if n in protected]


PENDING = STATE / "pending.json"
PENDING_MINUTES = 30


def pending():
    """Requests waiting for your OK in the chat: [{names, what, at}] from the last 30 minutes."""
    now = time.time()
    return [r for r in _load(PENDING, []) if isinstance(r, dict) and now - r.get("at", 0) < PENDING_MINUTES * 60]


def add_pending(names, what):
    reqs = [r for r in pending() if set(r["names"]) != set(names)]
    reqs.append({"names": sorted(names), "what": what[:200], "at": time.time()})
    STATE.mkdir(exist_ok=True)
    PENDING.write_text(json.dumps(reqs))


def answer_pending(yes, minutes=None):
    """Called by the message hook when you reply approve or no. Returns the key names it applied to."""
    reqs = pending()
    PENDING.unlink(missing_ok=True)
    names = sorted({n for r in reqs for n in r["names"]})
    if yes and names:
        minutes = minutes or settings()[2]
        g = grants()
        for n in names:
            g[n] = time.time() + minutes * 60
        GRANTS.write_text(json.dumps(g))
        for r in reqs:
            log_use(r["names"], r["what"], f"approved in the chat for {span(minutes)}")
    return names


def log_use(names, what, how):
    try:
        STATE.mkdir(exist_ok=True)
        with open(STATE / "use.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {how}: {', '.join(names)} for: {what[:200]}\n")
    except OSError:
        pass


def ask(names, what, minutes=60, timeout=300, force=False):
    """Return True once a person approves the keys in `names` for `what`.

    On the phone the person picks how long the approval lasts. Windows Hello or Touch ID on the computer
    approves for `minutes`. The approval covers only these keys.
    """
    need = granted(names)
    if not need:
        return True
    mode, _, auto_minutes = settings()
    tap = list(need) if force == "phone" else needs_tap(need)
    rest = [n for n in need if n not in tap]
    if rest and mode == "auto" and not force:  # approvals off: grant these now and note it in the log
        g = grants()
        for n in rest:
            g[n] = time.time() + auto_minutes * 60
        STATE.mkdir(exist_ok=True)
        GRANTS.write_text(json.dumps(g))
        log_use(rest, what, "allowed (approvals off)")
        rest = []
    if rest:  # ask in the chat: the message hook grants these when the person replies approve
        add_pending(rest, what)
        print(f"keys: {', '.join(rest)} needs your OK. Ask the person: \"I need {', '.join(rest)}. Reason: {what}. "
              f"Reply approve, or approve 1h.\" Then end your turn, and run this command again after they "
              "approve.", file=sys.stderr)
        log_use(rest, what, "asked in the chat")
        return False
    need = tap
    reason = f"{', '.join(need)} for: {what}"[:300] + f" (on {socket.gethostname()})"
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
        g = grants()
        for n in need:
            g[n] = time.time() + chosen * 60
        STATE.mkdir(exist_ok=True)
        GRANTS.write_text(json.dumps(g))
        print(f"keys: {', '.join(need)} approved on the {src} for {span(chosen)}.", file=sys.stderr)
        log_use(need, what, f"approved on the {src} for {span(chosen)}")
        return True
    if all(a in ("unavailable", "timeout") for a in answers.values()):
        print("keys: nobody approved. Set up your phone with `keys phone`, or turn on "
              f"{where} on this computer.", file=sys.stderr)
    else:
        print(f"keys: denied on the {src}.", file=sys.stderr)
    return False


def lock(names=None):
    """End the approval for these keys, or for every key."""
    if not names:
        GRANTS.unlink(missing_ok=True)
        return
    g = {k: v for k, v in grants().items() if k not in names}
    GRANTS.write_text(json.dumps(g))
