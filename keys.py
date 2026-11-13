#!/usr/bin/env python3
"""keys: keep API keys locked away and hand them to programs, so Claude only sees key names.

  keys setup                        use 1Password if it's connected, otherwise this computer's key store
  keys setup 1password [VAULT]      use 1Password (preferred)
  keys setup local                  use this computer's key store (Windows Credential Manager or the Mac Keychain)
  keys switch 1password|local       move to the other store, copying every key across
  keys strict on|off                1Password only, read fresh each time behind its own check
  keys approvals chat|phone|off     how a key gets a grant: you reply approve (default), a phone tap, or no question
  keys protect NAME                 this key always needs a tap (keys unprotect NAME to undo)
  keys phone                        approve key use from your phone (then: keys phone test)
  keys add NAME [--clipboard | --from-env VAR] [--note "what it is"]
                                    save a key (from the clipboard, a variable, a pipe, or typed hidden)
  keys import FILE.env              save every NAME=value line of a .env file
  keys list                         list saved keys: what each is, when and where it was saved
  keys note NAME "what it is"       add a note to a key, shown in keys list and to each new Claude session
  keys rename OLD NEW               rename a saved key
  keys remove NAME                  delete a saved key
  keys ref NAME                     print the line for a .env.tpl file
  keys run NAME [VAR=NAME] -- CMD   run CMD with those keys set as environment variables
  keys grant --for WHY              ask for approval; you pick how long on your phone
  keys status                       how long the current approval lasts
  keys lock                         end the current approval now
  keys rescue [--save] [--redact]   find keys pasted into Claude chat logs, save them, scrub the logs
"""
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approve  # noqa: E402
import local_store  # noqa: E402
import stores  # noqa: E402

HOME = Path.home()
STATE = stores.STATE
CONFIG = stores.CONFIG
SAVED = STATE / "saved.json"  # sha256 of each value already saved -> key name
META = STATE / "meta.json"  # key name -> when it was saved, where it came from, a note. Never the value.
IS_WIN = os.name == "nt"
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

SETUP_HELP = """Connect the 1Password app to the command line:
  1. Open the 1Password app and sign in.
  2. Settings > Developer: turn on "Integrate with 1Password CLI".
Then run: keys setup 1password
For computers that run on their own, use a 1Password service account instead:
  keys add OP_SERVICE_ACCOUNT_TOKEN --clipboard   (saved in this computer's key store)"""


def die(msg, code=1):
    print(f"keys: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    STATE.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def mask(v):
    return f"{v[:4]}... {len(v)} chars" if len(v) > 16 else f"{len(v)} chars"


def fingerprint(v):
    return hashlib.sha256(v.encode()).hexdigest()


def norm_name(n):
    n = re.sub(r"[^A-Za-z0-9_]", "_", n.strip()).upper()
    if not NAME_RE.match(n):
        die(f"'{n}' isn't a usable name. Use letters, numbers and _, like OPENAI_API_KEY.")
    return n


def store_now():
    return stores.current()


def call(fn, *args):
    """Run a store call and turn its errors into a plain message."""
    try:
        return fn(*args)
    except stores.StoreError as e:
        msg = str(e)
        if any(w in msg.lower() for w in ("integration", "sign in", "not signed", "no accounts")):
            die(f"1Password isn't connected yet.\n{SETUP_HELP}\n\n{msg}")
        die(msg)


def existing_names():
    return call(store_now().names)


def remember(name, value):
    saved = load_json(SAVED, {})
    saved[fingerprint(value)] = name
    for line in value.splitlines() if "\n" in value else []:
        line = line.strip()
        if len(line) >= 32 and not line.startswith("-----"):
            saved[fingerprint(line)] = name
    save_json(SAVED, saved)


def store(name, value, replace=False, names=None, source=None, note=None):
    names = existing_names() if names is None else names
    if name in names and not replace:
        die(f"{name} is already saved. Add --replace to overwrite it.")
    call(store_now().save, name, value)
    remember(name, value)
    names.add(name)
    meta = load_json(META, {})
    old = meta.get(name, {})
    meta[name] = {k: v for k, v in {
        "saved": time.strftime("%Y-%m-%d %H:%M"), "hint": mask(value), "kind": known_key_in(value),
        "from": source, "note": note or old.get("note")}.items() if v}
    save_json(META, meta)


def set_meta(name, **fields):
    meta = load_json(META, {})
    meta.setdefault(name, {}).update({k: v for k, v in fields.items() if v})
    save_json(META, meta)


def describe(name, meta=None):
    """One line about a saved key, for keys list and the note at the start of each session."""
    m = (load_json(META, {}) if meta is None else meta).get(name, {})
    bits = [m.get("note"), m.get("kind"), m.get("hint"), m.get("saved") and "saved " + m["saved"][:10],
            m.get("from")]
    return "; ".join(b for b in bits if b)


def read_value(name):
    """A key's value, for handing to a program after approval. 1Password keys come from the approval's copies."""
    s = store_now()
    if s.kind == "local":
        return s.read(name)
    approved = approve.grants()
    if name in approved and not stores.strict():
        call(stores.cache_fill, s, approved)
        v = stores.cache_read(name)
        if v is not None:
            return v
    return call(s.read, name)


# ---------- setup ----------

def cmd_setup(args):
    cfg = load_json(CONFIG, {})
    if "--grant-minutes" in args:
        i = args.index("--grant-minutes")
        cfg["grant_minutes"] = float(args[i + 1])
        args = args[:i] + args[i + 2:]
        save_json(CONFIG, cfg)
    choice = args[0] if args else None
    if choice == "1password" or (choice is None and stores.OnePassword().connected()):
        return setup_1password(args[1:] if choice else [])
    if choice is None:
        print("1Password isn't connected, so keys go in this computer's key store. "
              "1Password is preferred: run  keys setup 1password  once it's connected.")
    cfg["backend"] = "local"
    save_json(CONFIG, cfg)
    print(f"Keys are stored in {stores.Local.label} on this computer.")
    setup_report(cfg)


def setup_report(cfg):
    check = approve.local_check()
    hello = "Windows Hello" if IS_WIN else "Touch ID"
    print(f"{hello}: {'ready' if check == 'available' else check}")
    print(f"Phone approval: {'ready' if approve.phone_config() else 'off (run: keys phone)'}")
    print(f"On your phone you pick how long each approval lasts. An approval with {hello} lasts "
          f"{cfg.get('grant_minutes', 60):g} minutes (change it with: keys setup --grant-minutes N).")


def setup_1password(args):
    s = stores.OnePassword(args[0] if args else None)
    if not s.connected():
        die(SETUP_HELP)
    if call(s.ensure_vault):
        print(f"Created the 1Password vault '{s.vault}'.")
    cfg = load_json(CONFIG, {})
    old = cfg.get("backend", "local")
    cfg.update(backend="1password", vault=s.vault)
    save_json(CONFIG, cfg)
    print(f"Keys are stored in the 1Password vault '{s.vault}'.")
    if old == "local" and local_store.names():
        print(f"This computer's key store still has {len(local_store.names())} keys. "
              "To copy them into 1Password, run: keys switch 1password")
    setup_report(cfg)


def cmd_switch(args):
    """Copy every key into the other store and use that store from now on. The old copies stay until removed."""
    if not args or args[0] not in ("1password", "local"):
        die("usage: keys switch 1password [VAULT]  or  keys switch local")
    if args[0] == "local" and stores.strict():
        die("strict mode is on, and it needs 1Password. Turn it off first: keys strict off")
    src, dst = store_now(), stores.by_kind(args[0], args[1] if len(args) > 1 else None)
    if dst.kind == "1password":
        if not dst.connected():
            die(SETUP_HELP)
        call(dst.ensure_vault)
    names = sorted(call(src.names))
    need_approval(names, f"copy keys from {src.label} to {dst.label}")
    have = call(dst.names)
    for n in names:
        v = call(src.read, n)
        if v is None:
            print(f"  {n}: couldn't read it, skipped")
            continue
        call(dst.save, n, v)
        remember(n, v)
        print(f"  {n}: {'replaced' if n in have else 'copied'}")
    cfg = load_json(CONFIG, {})
    cfg["backend"] = dst.kind
    if dst.kind == "1password":
        cfg["vault"] = dst.vault
    save_json(CONFIG, cfg)
    print(f"Done. Keys now come from {dst.label}. The copies in {src.label} are still there; "
          f"remove them by hand once you've checked {dst.label}.")


def cmd_phone(args):
    try:
        import phone_server as ps
    except ImportError:
        die("phone approval needs the cryptography package. Run: python -m pip install --user cryptography")
    if args[:1] == ["off"]:
        ps.unpublish()
        try:
            approve._local("/local/quit", {}, timeout=3)
        except Exception:
            pass
        (STATE / "phone.json").unlink(missing_ok=True)
        print("Phone approval is off, and its passkeys are forgotten.")
        return
    if args[:1] == ["test"]:
        if not approve.phone_config():
            die("set up your phone first: keys phone")
        print("Sent a test request to your phone. Open Keys and tap Allow or Deny...")
        ok = approve.ask(["TEST"], "a test from keys phone test (this one doesn't unlock anything)", force="phone")
        approve.lock(["TEST"])
        print("It works." if ok else "No approval came back.")
        return
    try:
        ps.publish()
        approve.ensure_server()
        link = ps.new_enrollment()
    except Exception as e:
        die(str(e))
    print(f"""Set up your phone to approve key use. Your phone needs Tailscale turned on.

  1. On your phone, open this link in Safari (Chrome on Android). It works for 30 minutes:
       {link}
  2. On an iPhone, tap Share, then Add to Home Screen, then open Keys from your home screen.
  3. Tap "Turn on notifications", then "Create a passkey with Face ID".
  4. Run: keys phone test

Only devices signed in to your Tailscale account can open the page, and approving needs your passkey.""")

# ---------- add / list / ref ----------

def read_clipboard():
    if IS_WIN:
        cmd = [approve.POWERSHELL, "-NoProfile", "-Command", "Get-Clipboard -Raw"]
    elif sys.platform == "darwin":
        cmd = ["pbpaste"]
    elif shutil.which("wl-paste"):
        cmd = ["wl-paste", "-n"]
    else:
        cmd = ["xclip", "-o", "-selection", "clipboard"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout or ""


def clear_clipboard():
    if IS_WIN:
        subprocess.run([approve.POWERSHELL, "-NoProfile", "-Command", "Set-Clipboard -Value ' '"])
    elif sys.platform == "darwin":
        subprocess.run(["pbcopy"], input="", text=True)


def need_approval(names, action):
    """Changing or deleting saved keys needs the person's OK for those keys (or an approval still running)."""
    if not approve.ask(sorted(names), action, minutes=grant_minutes()):
        die("not approved, so nothing changed.")


def cmd_add(args):
    """Save a key from the clipboard, an environment variable, a pipe, or typed in hidden."""
    from_env = args[args.index("--from-env") + 1] if "--from-env" in args else None
    rest = [a for i, a in enumerate(args)
            if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--from-env", "--note"))]
    if len(rest) != 1:
        die('usage: keys add NAME [--clipboard | --from-env VAR] [--note "what it is"] [--replace]'
            "   (or pipe the key in)")
    name = norm_name(rest[0])
    note = args[args.index("--note") + 1] if "--note" in args else None
    if "--clipboard" in args:
        value, source = read_clipboard(), "keys add, from the clipboard"
    elif from_env:
        value, source = os.environ.get(from_env, ""), f"keys add, from ${from_env}"
        if not value:
            die(f"the environment variable {from_env} is empty.")
    elif sys.stdin.isatty():
        value, source = getpass.getpass(f"Paste {name} (it won't show): "), "keys add, typed in"
    else:
        value, source = sys.stdin.read(), "keys add, piped in"
    source += f" in {os.getcwd()}"
    value = value.strip()
    if len(value) < 8:
        die("that doesn't look like a key.")
    names = existing_names()
    if name in names:
        if "--replace" not in args:
            die(f"{name} is already saved. Add --replace to overwrite it (that needs approval).")
        need_approval([name], "replace the saved key")
    store(name, value, replace=True, names=names, source=source, note=note)
    if "--clipboard" in args:
        clear_clipboard()
    print(f"Saved {name} ({mask(value)}).")


def cmd_import(args):
    """Save every NAME=value line of a .env file. Values are never printed."""
    files = [a for a in args if not a.startswith("--")]
    if not files:
        die("usage: keys import FILE.env [FILE2.env ...] [--replace]")
    names, saved, asked = existing_names(), load_json(SAVED, {}), False
    for path in files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            var, _, value = line.removeprefix("export ").partition("=")
            value = value.strip().strip('"').strip("'")
            if not value or value.startswith(("op://", "keys:")):
                continue
            name = norm_name(var)
            if saved.get(fingerprint(value)) == name and name in names:
                print(f"  {name}: already saved")
            elif name in names and "--replace" not in args:
                print(f"  {name}: a different value is already saved. Add --replace to overwrite it.")
            else:
                if name in names:
                    need_approval([name], f"replace it with the one in {Path(path).name}")
                store(name, value, replace=True, names=names, source=f"imported from {Path(path).resolve()}")
                saved[fingerprint(value)] = name
                print(f"  {name}: saved ({mask(value)})")
    print(f"To use them: keys run NAME -- <command>, or put the lines from keys ref NAME in a .env.tpl file.")


def cmd_list(args):
    names = sorted(existing_names())
    if not names:
        print("No keys yet. Add one with: keys add NAME --clipboard")
    meta = load_json(META, {})
    for n in names:
        print(f"{n}\n    {describe(n, meta) or 'no details (saved before keywarden kept them)'}")


def cmd_note(args):
    """keys note NAME "what it is": a note that keys list and each new Claude session show next to the name."""
    if len(args) < 2:
        die('usage: keys note NAME "what it is, e.g. GitHub token for the deploy bot, expires March"')
    name = norm_name(args[0])
    if name not in existing_names():
        die(f"there's no saved key called {name}. See keys list.")
    set_meta(name, note=" ".join(args[1:]).strip())
    print(f"{name}: {describe(name)}")


def cmd_remove(args):
    names = [norm_name(n) for n in args]
    if not names:
        die("usage: keys remove NAME [NAME ...]")
    need_approval(names, "delete them" if len(names) > 1 else "delete it")
    save_json(SAVED, {k: v for k, v in load_json(SAVED, {}).items() if v not in names})
    save_json(META, {k: v for k, v in load_json(META, {}).items() if k not in names})
    for n in names:
        call(store_now().delete, n)
        print(f"Removed {n}.")


def grant_minutes():
    """How long an approval from Windows Hello or Touch ID lasts. On the phone, the person picks."""
    return load_json(CONFIG, {}).get("grant_minutes", 60)


def cmd_grant(args):
    """Ask for approval of these keys. While it lasts, every command Claude runs has them as environment variables."""
    reason = args[args.index("--for") + 1] if "--for" in args else "agent work"
    names = [norm_name(a) for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--for")]
    if not names:
        die('name the keys you need, like: keys grant CLOUDFLARE_API_TOKEN R2_BUCKET --for "uploading the build"')
    missing = [n for n in names if n not in existing_names()]
    if missing:
        die(f"no saved key named {', '.join(missing)}. Run: keys list")
    if not approve.ask(names, reason, minutes=grant_minutes()):
        sys.exit(1)
    cmd_status([])


def _left(expiry):
    h, m = divmod(int((expiry - time.time()) // 60), 60)
    return f"{f'{h}h ' if h else ''}{m}m"


def cmd_approvals(args):
    """keys approvals chat|phone|off: how a key without a grant gets one. chat is the default."""
    cfg = load_json(CONFIG, {})
    words = {"chat": "chat: Claude asks you in the chat and you reply approve",
             "phone": "phone: every key needs a tap on your phone",
             "auto": "off: keys are granted with no question"}
    mode = approve.settings()[0]
    if not args:
        print(f"Approvals are {words[mode]}.")
        if cfg.get("protected") and mode != "phone":
            print("These keys need a tap on your phone: " + ", ".join(sorted(cfg["protected"])))
        return
    want = {"chat": "chat", "phone": "phone", "on": "phone", "off": "auto"}.get(args[0])
    if not want:
        die("usage: keys approvals chat|phone|off")
    order = {"auto": 0, "chat": 1, "phone": 2}
    if order[want] < order[mode]:  # asking less needs the person's OK
        if not approve.ask(["APPROVALS_CHANGE"], f"change approvals to {args[0]}", minutes=1, force=True):
            die("not approved, so approvals stay as they are.")
        approve.lock(["APPROVALS_CHANGE"])
    cfg["approvals"] = want
    save_json(CONFIG, cfg)
    print(f"Approvals are {words[want]}.")


def cmd_protect(args):
    """keys protect NAME: this key always needs a tap. keys unprotect NAME takes that off (needs a tap)."""
    names = [norm_name(a) for a in args]
    if not names:
        die("usage: keys protect NAME [NAME ...]")
    cfg = load_json(CONFIG, {})
    cfg["protected"] = sorted(set(cfg.get("protected", [])) | set(names))
    save_json(CONFIG, cfg)
    approve.lock(names)
    print(f"{', '.join(names)} now need{'s' if len(names) == 1 else ''} a tap on your phone before each use.")


def cmd_unprotect(args):
    names = [norm_name(a) for a in args]
    if not names:
        die("usage: keys unprotect NAME [NAME ...]")
    ok_names = [f"UNPROTECT_{n}" for n in names]
    if not approve.ask(ok_names, "stop asking before each use of " + ", ".join(names), minutes=1, force=True):
        die("not approved, so nothing changed.")
    approve.lock(ok_names)
    cfg = load_json(CONFIG, {})
    cfg["protected"] = sorted(set(cfg.get("protected", [])) - set(names))
    save_json(CONFIG, cfg)
    print(f"{', '.join(names)} no longer need a tap.")


def cmd_strict(args):
    """keys strict on|off: keys only from 1Password, read fresh each time behind 1Password's own check."""
    cfg = load_json(CONFIG, {})
    if not args:
        print(f"Strict mode is {'on' if cfg.get('strict') else 'off'}.")
        return
    if args[0] == "on":
        if store_now().kind != "1password":
            die("strict mode needs 1Password. Run  keys setup 1password  (or keys switch 1password) first.")
        cfg["strict"] = True
        save_json(CONFIG, cfg)
        stores.cache_clear()
        print("Strict mode is on. Every key a command gets is read fresh from 1Password, which asks with its own "
              "Touch ID or Windows Hello check. Nothing is copied onto this computer.")
        if local_store.read("OP_SERVICE_ACCOUNT_TOKEN"):
            print("A 1Password service account token is saved on this computer. Strict mode won't use it, but a "
                  "program running as you could read it. Delete it with "
                  + ("cmdkey /delete:agent-keys:OP_SERVICE_ACCOUNT_TOKEN" if IS_WIN else
                     "security delete-generic-password -s agent-keys -a OP_SERVICE_ACCOUNT_TOKEN")
                  + ", then revoke it in 1Password.")
    elif args[0] == "off":
        if not cfg.get("strict"):
            print("Strict mode is already off.")
            return
        if not approve.ask(["STRICT_MODE_OFF"], "turn strict mode off", minutes=1, force=True):
            die("not approved, so strict mode stays on.")
        approve.lock(["STRICT_MODE_OFF"])
        cfg["strict"] = False
        save_json(CONFIG, cfg)
        print("Strict mode is off.")
    else:
        die("usage: keys strict on|off")


def cmd_status(args):
    approved, names = approve.grants(), sorted(existing_names())
    if stores.strict():
        print("Strict mode is on: keys are read fresh from 1Password, which asks before each new program reads.")
    if approved:
        print("Approved now. Every command has these as environment variables:")
        for n in sorted(approved):
            print(f"  {n}  ({_left(approved[n])} left)")
    else:
        print("No keys are approved right now.")
    rest = [n for n in names if n not in approved]
    if rest:
        print("Not approved: " + ", ".join(rest))
        print('To use one, run: keys grant NAME --for "what you\'re doing"')
    on, protected, _ = approve.settings()
    if not on and protected:
        print("These need a tap on the phone: " + ", ".join(sorted(protected)))


def cmd_env(args):
    """Print shell lines that set every key, only while an approval lasts. The Claude hook runs this before
    each command and evals the output, so values reach the command and never the chat."""
    approved = approve.grants()
    if not approved:
        if stores.cache_names():
            stores.cache_clear()  # every approval ran out: delete the 1Password copies
        return
    ps = "--ps" in args
    s = store_now()
    fresh = s.kind == "local" or stores.strict()  # read straight from the store, not from copies
    try:
        if fresh:
            names = sorted(approved) if stores.strict() else sorted(set(approved) & s.names())
        else:
            stores.cache_fill(s, approved)
            names = stores.cache_names()
    except stores.StoreError as e:
        print(f"keys: couldn't load keys ({e})", file=sys.stderr)
        return
    import base64
    import shlex
    loaded = []
    for name in names:
        try:
            v = s.read(name) if fresh else stores.cache_read(name)
        except stores.StoreError as e:
            print(f"keys: couldn't read {name} ({e})", file=sys.stderr)
            continue
        if v is None:
            continue
        loaded.append(name)
        if ps:  # base64, so no character in a key can end the PowerShell string early
            b = base64.b64encode(v.encode()).decode()
            print(f"$env:{name} = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b}'))")
        else:
            print(f"export {name}={shlex.quote(v)}")
    # mask.py reads this to know which variables to hide in command output.
    print(f"$env:AGENT_KEYS_NAMES = '{','.join(loaded)}'" if ps else f"export AGENT_KEYS_NAMES={','.join(loaded)}")


def cmd_lock(args):
    names = [norm_name(a) for a in args]
    approve.lock(names or None)
    if names and store_now().kind == "1password":
        stores.cache_fill(store_now(), approve.grants())
    elif not names:
        stores.cache_clear()
    print(f"Done. {', '.join(names) if names else 'Every key'} will ask for approval again.")


def cmd_rename(args):
    if len(args) != 2:
        die("usage: keys rename OLD_NAME NEW_NAME")
    old, new = norm_name(args[0]), norm_name(args[1])
    need_approval([old], f"rename it to {new}")
    call(store_now().rename, old, new)
    saved = load_json(SAVED, {})
    save_json(SAVED, {k: (new if v == old else v) for k, v in saved.items()})
    meta = load_json(META, {})
    if old in meta:
        meta[new] = meta.pop(old)
        save_json(META, meta)
    print(f"Renamed {old} to {new}.")


def cmd_ref(args):
    for n in args:
        n = norm_name(n)
        print(f"{n}=keys:{n}")


# ---------- run ----------

def tpl_keys(path=".env.tpl"):
    """VAR -> key name from a .env.tpl. Lines look like VAR=keys:NAME or VAR=op://Vault/NAME/field."""
    out = {}
    if not Path(path).exists():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        var, _, ref = line.strip().removeprefix("export ").partition("=")
        ref = ref.strip().strip('"').strip("'")
        if ref.startswith("keys:"):
            out[norm_name(var)] = norm_name(ref[5:])
        elif ref.startswith("op://") and len(ref.split("/")) >= 5:
            out[norm_name(var)] = norm_name(ref.split("/")[3])
    return out


def run_masked(cmd, env, values):
    """Run cmd and replace any key value in its output, so a stray print can't reach the chat."""
    hide = sorted({v.encode() for v in values if len(v) >= 6}, key=len, reverse=True)
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def pump(src, dst):
        for line in iter(src.readline, b""):
            for h in hide:
                line = line.replace(h, b"<concealed by keywarden>")
            dst.buffer.write(line)
            dst.flush()

    ts = [threading.Thread(target=pump, args=(p.stdout, sys.stdout)),
          threading.Thread(target=pump, args=(p.stderr, sys.stderr))]
    for t in ts:
        t.start()
    code = p.wait()
    for t in ts:
        t.join()
    return code


def cmd_run(args):
    if "--" not in args:
        die("usage: keys run NAME [VAR=NAME ...] -- command")
    i = args.index("--")
    pairs, cmd = args[:i], args[i + 1:]
    if not cmd:
        die("nothing to run after --")
    wanted = tpl_keys()
    for p in pairs:
        var, _, name = p.partition("=")
        wanted[norm_name(var)] = norm_name(name or var)
    if not wanted:
        die("name the keys to pass in, like: keys run OPENAI_API_KEY -- npm start")
    if IS_WIN:
        found = shutil.which(cmd[0])
        if found:
            cmd = [found, *cmd[1:]]
    what = " ".join(args[i + 1:])
    minutes = grant_minutes()

    # Same path for both stores: approval first, then the values go into this one program's environment.
    if not approve.ask(sorted(set(wanted.values())), what, minutes):
        sys.exit(1)
    env, values, missing = dict(os.environ), [], []
    for var, name in wanted.items():
        v = read_value(name)
        if v is None:
            missing.append(name)
        else:
            env[var] = v
            values.append(v)
    if missing:
        die(f"no saved key named {', '.join(missing)}. Run: keys list")
    sys.exit(run_masked(cmd, env, values))


# ---------- rescue ----------

B, E = r"(?<![A-Za-z0-9_\-])", r"(?![A-Za-z0-9_\-])"
PATTERNS = [
    ("ANTHROPIC_API_KEY", r"sk-ant-(?:api|admin)\d\d-[A-Za-z0-9_\-]{80,}"),
    ("OPENROUTER_API_KEY", r"sk-or-v1-[a-f0-9]{64}"),
    ("OPENAI_API_KEY", r"sk-(?:proj-|svcacct-|admin-)?(?!ant-|or-|lora-)[A-Za-z0-9_\-]{40,}"),
    ("CLOUDFLARE_API_TOKEN", r"cf[a-z]t_[A-Za-z0-9_\-]{40,}"),
    ("DOCKER_TOKEN", r"dckr_(?:pat|oat)_[A-Za-z0-9_\-]{20,}"),
    ("FLY_API_TOKEN", r"FlyV1 fm[12]_[A-Za-z0-9_+/=,]{40,}"),
    ("GITHUB_TOKEN", r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    ("GOOGLE_API_KEY", r"AIza[0-9A-Za-z_\-]{35}"),
    ("HF_TOKEN", r"hf_[A-Za-z0-9]{34,}"),
    ("RUNPOD_API_KEY", r"rpa_[A-Za-z0-9]{40,}"),
    ("REPLICATE_API_TOKEN", r"r8_[A-Za-z0-9]{37,}"),
    ("XAI_API_KEY", r"xai-[A-Za-z0-9]{60,}"),
    ("GROQ_API_KEY", r"gsk_[A-Za-z0-9]{50,}"),
    ("STRIPE_SECRET_KEY", r"(?:sk|rk)_live_[A-Za-z0-9]{24,}"),
    ("SLACK_TOKEN", r"xox[abprs]-[A-Za-z0-9\-]{20,}"),
    ("ELEVENLABS_API_KEY", r"sk_[a-f0-9]{48}"),
    ("FAL_KEY", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{32}"),
    ("TAVILY_API_KEY", r"tvly-[A-Za-z0-9\-]{30,}"),
    ("PERPLEXITY_API_KEY", r"pplx-[A-Za-z0-9]{40,}"),
    ("NPM_TOKEN", r"npm_[A-Za-z0-9]{36}"),
    ("AWS_ACCESS_KEY_ID", r"AKIA[0-9A-Z]{16}"),
]
# Your own key formats go in ~/.agent-keys/patterns.json: [{"name": "MY_KEY", "regex": "myk_[A-Za-z0-9]{30,}"}]
def _own_patterns():
    """Your own key formats from patterns.json. A broken entry is skipped, so it can't switch the checks off."""
    out = []
    for p in load_json(STATE / "patterns.json", []):
        try:
            re.compile(p["regex"])
            out.append((norm_name(p["name"]), p["regex"]))
        except (re.error, KeyError, TypeError, SystemExit):
            print(f"keys: skipped a broken entry in patterns.json: {p!r}", file=sys.stderr)
    return out


PATTERNS = _own_patterns() + [("JWT_TOKEN", r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")] + PATTERNS
KNOWN = re.compile("|".join(f"(?P<g{i}>{B}{p}{E})" for i, (_, p) in enumerate(PATTERNS)))
PREFILTER = re.compile("|".join(p for _, p in PATTERNS))
# Keys with no known prefix: only looked for in text you typed. Long runs with upper, lower and digits,
# at most one _ or -, and not part of a path or file name.
GENERIC = re.compile(r"(?<![A-Za-z0-9_\-\\/.:])[A-Za-z0-9_\-]{32,120}(?![A-Za-z0-9_\-\\/.:])")


def generic_ok(v):
    return (sum(c in "_-" for c in v) <= 1 and sum(c.isdigit() for c in v) >= 4
            and sum(c.isupper() for c in v) >= 3 and sum(c.islower() for c in v) >= 3
            and not v.lower().startswith("session"))


def plausible(v):
    low = v.lower()
    return len(set(v)) >= 10 and any(c.isdigit() for c in v) and not any(
        s in low for s in ("example", "xxxx", "your", "0000000", "abcdef", "redacted", "placeholder"))


# ---------- keys inside a message, a file or a command ----------

# Keys in a message get saved before Claude sees them, in three ways:
#   1. `key NAME value`, or `key NAME` with a private key block under it. The name is yours.
#   2. "save this" (or "store this", "save my ... key") with the key in the message. The name comes from
#      NAME=value, "as NAME", the words before the key ("save this cloudfront key K2J...") or the key's format.
#   3. A key in a well-known format (sk-..., ghp_..., AKIA..., a private key block) or NAME_KEY=value, with no
#      "save". It's saved under the format's name.
#   4. A long value described nearby as a key, token, secret, password or credential.
KEY_LINE = re.compile(r"(?m)^[ \t]*key[ \t]+([A-Za-z][A-Za-z0-9_]{1,})[ \t]+(\S+)[ \t]*$")
KEY_NAME_LINE = re.compile(r"(?m)^[ \t]*key[ \t]+([A-Za-z][A-Za-z0-9_]{1,})[ \t]*$")
PEM = re.compile(r"-----BEGIN ([A-Z ]*)PRIVATE KEY-----[\s\S]+?-----END \1PRIVATE KEY-----")
TOKEN = re.compile(r"[A-Za-z0-9_\-+/=.:]{12,}")
SAVE_WORDS = re.compile(r"(?i)\b(?:save|store)\s+(?:this|it|these|that|them|my|as)\b"
                        r"|\b(?:save|store)\b[^\n]{0,40}?\b(?:key|token|secret|password|credential|pat)s?\b")
KEY_WORDS = re.compile(r"(?i)\b(?:key|token|secret|password|credential)s?\b")
PAIR = re.compile(r"(?m)(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,})[ \t]*[=:][ \t]*[\"']?([^\s\"']{8,})[\"']?")
SECRET_NAME = re.compile(r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PAT|CREDENTIALS?)$")
AS_NAME = re.compile(r"\b[Aa]s\s+([A-Z][A-Z0-9_]+|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b")
RAW = re.compile(r"(?<![A-Za-z0-9_\-+/=.\\])[A-Za-z0-9_\-+/=]{12,}(?![A-Za-z0-9_\-+/=.\\])")
IDENT = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
STOP = set("save store keep add put this it these that them my the a an is are here as for please plz pls and "
           "to of in on new it's its me can you i here's use".split())


def _raw_ok(v):
    """A string that could be a key: letters and digits, not a name like R2_BUCKET, a path or a word."""
    return (any(c.isdigit() for c in v) and any(c.isalpha() for c in v) and not IDENT.match(v)
            and not (("/" in v) and len(v) < 20) and not v.lower().startswith(("http", "www", "/"))
            and not re.search(r"\.[A-Za-z]{2,4}(?:/|$)", v))


def _name_before(text, at):
    """A name from the words just before a key: "save this cloudfront key K2J..." gives CLOUDFRONT_KEY."""
    line = re.split(r"[\n.!?;]", text[:at])[-1]
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:'[A-Za-z]+)?", line)
    if words and IDENT.match(words[-1]):
        return words[-1]
    # "key for the weather api" and "password for the db" put the service after the kind of key.
    for i, w in enumerate(words):
        if KEY_WORDS.fullmatch(w) and i + 1 < len(words) and words[i + 1].lower() == "for":
            words = words[i + 2:] + [w]
            break
    words = [w for w in words if w.lower() not in STOP][-5:]
    if not words:
        return None
    n = "_".join(words).upper()
    return n if SECRET_NAME.search(n) or n.endswith("_ID") else n + "_KEY"


def _format_name(v):
    m = KNOWN.fullmatch(v) if v else None
    return PATTERNS[int(m.lastgroup[1:])][0] if m else None


def find_keys(text):
    """Keys in a message: [(NAME, value, named)]. named is True when the person chose the name, so a saved key
    with that name gets replaced. Otherwise a name that's taken gets _2, _3..."""
    found, taken = [], []  # taken: spans already used, so a value isn't picked up twice

    def add(name, value, named, span):
        if any(a < span[1] and span[0] < b for a, b in taken) or any(v == value for _, v, _ in found):
            return
        taken.append(span)
        found.append((norm_name(name), value, named))

    for m in KEY_LINE.finditer(text):
        if re.fullmatch(r"\[[A-Z][A-Z0-9_]*\]", m.group(2)):
            continue  # A name inserted by the prompt-replacement plugin, not another value to save.
        add(m.group(1), m.group(2), True, m.span(2))
    for m in KEY_NAME_LINE.finditer(text):
        rest = text[m.end():]
        block = PEM.match(rest.lstrip("\r\n \t"))
        if block:
            off = m.end() + len(rest) - len(rest.lstrip("\r\n \t"))
            add(m.group(1), block.group(0).strip(), True, (off, off + block.end()))
    orig, text = text, re.sub(r"\S*://\S*", lambda m: " " * len(m.group(0)), text)  # links aren't keys
    asked = bool(SAVE_WORDS.search(text))
    as_name = AS_NAME.search(text) if asked else None
    for m in PEM.finditer(text):
        add(as_name.group(1) if as_name else "PRIVATE_KEY", m.group(0).strip(), bool(as_name), m.span())
    for m in PAIR.finditer(text):
        name, v = m.group(1), m.group(2)
        if name.lower() in STOP:  # "save this: KEY" names nothing, so the words before it pick the name
            continue
        if (asked or SECRET_NAME.search(name)) and _raw_ok(v) and not v.isdigit() and len(v) >= 12:
            if KEY_WORDS.fullmatch(name):
                add(_name_before(text, m.start(2)) or name, v, False, m.span(2))
            else:
                add(name, v, True, m.span(2))
    for m in KNOWN.finditer(orig):
        if plausible(m.group(0)):
            fmt = PATTERNS[int(m.lastgroup[1:])][0]
            if asked and as_name:
                add(as_name.group(1), m.group(0), True, m.span())
            else:
                add(fmt, m.group(0), False, m.span())
    for m in RAW.finditer(text):
        v = m.group(0)
        before = re.split(r"[\n.!?;]", text[max(0, m.start() - 80):m.start()])[-1]
        described = (KEY_WORDS.search(before) and len(v) >= 32
                     and not re.fullmatch(r"[A-Fa-f0-9]+|[A-Fa-f0-9-]{36}", v))
        if (asked or described) and _raw_ok(v):
            if as_name:
                add(as_name.group(1), v, True, m.span())
                as_name = None
            else:
                name = _name_before(text, m.start())
                add(name or "SAVED_KEY", v, False, m.span())
    return found


def known_key_in(text):
    """The kind of key, if text has one in a well-known format (sk-..., ghp_..., AKIA..., a private key block)."""
    if PEM.search(text):
        return "private key"
    for m in KNOWN.finditer(text):
        if plausible(m.group(0)):
            return PATTERNS[int(m.lastgroup[1:])][0]
    return None


# Saved values that aren't secret, so they may appear in commands and files: bucket names, account IDs, regions...
NOT_SECRET = re.compile(r"(?:ACCOUNT_ID|BUCKET|REGION|ENDPOINT|URL|HOST|USERNAME|USER|EMAIL)(?:_\d+)?$")


def key_in(text):
    """Name of a saved key, or of a key-shaped string, that appears in text. None if there isn't one."""
    saved = load_json(SAVED, {})
    for t in TOKEN.findall(text):
        for v in {t, t.strip(".:")}:
            name = saved.get(fingerprint(v))
            if name and not NOT_SECRET.search(name):
                return name
    for m in KNOWN.finditer(text):
        if plausible(m.group(0)):
            return PATTERNS[int(m.lastgroup[1:])][0]
    return None


def save_found(found, text="", where="", agent="Claude"):
    """Save keys found in a message. A name the person chose replaces a saved key with that name. A name
    keywarden picked gets _2, _3... if it's taken. Returns [(name, 'saved' | 'replaced' | 'already saved')].
    The message, with every key in it swapped for its name, is kept as where the key came from."""
    names, saved, out = existing_names(), load_json(SAVED, {}), []
    said = text
    for name, v, _ in found:
        said = said.replace(v, f"[{norm_name(name)}]")
    said = " ".join(said.split())
    said = said[:150] + ("..." if len(said) > 150 else "")
    source = f'pasted in a {agent} chat{" in " + where if where else ""}: "{said}"' if said else f"pasted in a {agent} chat"
    for name, v, named in found:
        name = norm_name(name)
        if saved.get(fingerprint(v)) in names:
            out.append((saved[fingerprint(v)], "already saved"))
            continue
        if not named:
            base, i = name, 2
            while name in names:
                name, i = f"{base}_{i}", i + 1
        out.append((name, "replaced" if name in names else "saved"))
        store(name, v, replace=True, names=names, source=source)
        saved[fingerprint(v)] = name
    return out


def strings_in(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for x in obj.values():
            yield from strings_in(x)
    elif isinstance(obj, list):
        for x in obj:
            yield from strings_in(x)


def typed_text(obj):
    """Text the person typed in this log line, or None."""
    t = None
    if obj.get("display") is not None:  # history.jsonl prompt
        t = obj["display"]
    elif obj.get("type") == "user":
        c = (obj.get("message") or {}).get("content")
        if isinstance(c, str):
            t = c
        elif isinstance(c, list):
            t = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    # Skip notices the app or other sessions put in the user's turn.
    if not t or t.lstrip().startswith(("<", "Another Claude session", "[Request interrupted")):
        return None
    return t


def log_files(hours):
    root = HOME / ".claude"
    files = list((root / "projects").rglob("*.jsonl")) + [root / "history.jsonl"]
    for extra in ("paste-cache", "file-history"):  # pasted text and earlier versions of edited files
        files += [f for f in (root / extra).rglob("*") if f.is_file()]
    cutoff = time.time() - hours * 3600 if hours else 0
    return [f for f in files if f.exists() and f.stat().st_mtime >= cutoff]


def scan(hours, generic):
    found = {}  # value -> info
    for f in log_files(hours):
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for raw in fh:
                quick = PREFILTER.search(raw)
                maybe_typed = '"display"' in raw or ('"type":"user"' in raw and '"tool_result"' not in raw)
                if not quick and not (generic and maybe_typed):
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    if f.suffix == ".jsonl" or not quick:
                        continue
                    obj = {"display": raw}  # plain text, like a saved paste
                typed = typed_text(obj) if isinstance(obj, dict) else None
                texts = list(strings_in(obj)) if quick else []
                hits = []
                for t in texts:
                    for m in KNOWN.finditer(t):
                        label = PATTERNS[int(m.lastgroup[1:])][0]
                        hits.append((label, m.group(0)))
                if generic and typed:
                    spans = [m.span() for m in KNOWN.finditer(typed)]
                    for m in GENERIC.finditer(typed):
                        inside = any(a <= m.start() < b for a, b in spans)
                        if not inside and generic_ok(m.group(0)):
                            hits.append(("UNKNOWN_KEY", m.group(0)))
                for label, v in hits:
                    if not plausible(v):
                        continue
                    info = found.setdefault(v, {"label": label, "files": set(), "typed": False,
                                                "when": str(obj.get("timestamp", "")) if isinstance(obj, dict) else ""})
                    info["files"].add(f)
                    if typed and v in typed:
                        info["typed"] = True
    return found


def redact_file(f, repl):
    for _ in range(5):
        before = f.stat().st_size
        text = f.read_text(encoding="utf-8", errors="surrogateescape")
        new = text
        for v, r in repl.items():
            new = new.replace(v, r)
        if new == text:
            return "clean"
        if f.stat().st_size != before:  # a live session wrote while we read; try again
            time.sleep(0.2)
            continue
        tmp = f.with_suffix(f.suffix + ".agent-keys-tmp")
        tmp.write_text(new, encoding="utf-8", errors="surrogateescape")
        try:
            os.replace(tmp, f)
            return "scrubbed"
        except OSError as e:
            tmp.unlink(missing_ok=True)
            return f"skipped ({e.strerror})"
    return "skipped (busy)"


def cmd_rescue(args):
    hours = 72
    if "--hours" in args:
        hours = float(args[args.index("--hours") + 1])
    include_unknown = "--all" in args
    found = scan(hours, generic=True)
    if not found:
        print(f"No keys found in Claude chat logs from the last {hours:g} hours." if hours else
              "No keys found in Claude chat logs.")
        return
    saved = load_json(SAVED, {})
    print(f"Found {len(found)} key(s) in Claude chat logs:")
    for v, info in sorted(found.items(), key=lambda kv: kv[1]["when"]):
        where = "you pasted it" if info["typed"] else "in tool output"
        done = f" -> saved as {saved[fingerprint(v)]}" if fingerprint(v) in saved else ""
        print(f"  {info['label']:<20} {mask(v):<16} {where}, {len(info['files'])} log file(s){done}")

    if "--save" in args:
        names = existing_names()
        for v, info in found.items():
            fp = fingerprint(v)
            if fp in saved or (info["label"] == "UNKNOWN_KEY" and not include_unknown):
                continue
            base = info["label"]
            name, n = base, 2
            while name in names:
                name, n = f"{base}_{n}", n + 1
            when = info["when"]
            if when.isdigit():  # Claude's prompt history keeps milliseconds since 1970
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(when) / 1000))
            files = ", ".join(sorted(str(f) for f in info["files"]))
            store(name, v, names=names, source=f"keys rescue found it in {files}"
                                              f" ({'you pasted it' if info['typed'] else 'tool output'}, {when[:16]})")
            saved[fp] = name
            print(f"  saved {mask(v)} as {name}")
        skipped = sum(1 for v, i in found.items() if i["label"] == "UNKNOWN_KEY" and fingerprint(v) not in saved)
        if skipped:
            print(f"  {skipped} unrecognised key(s) not saved. Add --all to save them as UNKNOWN_KEY.")

    if "--redact" in args:
        repl = {v: f"[key moved to keywarden: {saved[fingerprint(v)]}]" for v in found if fingerprint(v) in saved}
        files = sorted({f for v in repl for f in found[v]["files"]})
        results = {}
        for f in files:
            r = redact_file(f, repl)
            results[r] = results.get(r, 0) + 1
            if r.startswith("skipped"):
                print(f"  {r}: {f}")
        print("  scrubbed " + ", ".join(f"{n} file(s) {k}" for k, n in results.items()))
        print("Rotate any key that was pasted into chat: it was also sent to the model provider.")


def scrub_in_place(f, values, start=0):
    """Overwrite each value in file f with asterisks of the same length, without moving any other bytes, so a
    session that is still appending to the file is not disturbed. Only looks at bytes from `start` on."""
    hits = 0
    try:
        with open(f, "r+b") as fh:
            fh.seek(start)
            buf = fh.read()
            for v in values:
                i = buf.find(v)
                while i != -1:
                    fh.seek(start + i)
                    fh.write(b"*" * len(v))
                    hits += 1
                    i = buf.find(v, i + len(v))
    except OSError:
        pass
    return hits


def cmd_scrub(args):
    """keys _scrub FILE OFFSET, with the values on stdin. The prompt hook starts this in the background after it
    saves a pasted key: Claude Code writes the blocked message, key included, into the session log after the
    hook returns, so this waits for that line and blanks the key out for the next 30 seconds."""
    path, start = Path(args[0]), max(0, int(args[1]))
    values = set()
    for v in sys.stdin.read().splitlines():
        if len(v) >= 8:
            values.add(v.encode())
            values.add(json.dumps(v)[1:-1].encode())  # the form it takes inside a JSON line
    end = time.time() + 30
    while values and time.time() < end:
        time.sleep(1)
        scrub_in_place(path, values, start)


COMMANDS = {"_scrub": cmd_scrub, "note": cmd_note, "setup": cmd_setup, "switch": cmd_switch, "strict": cmd_strict, "approvals": cmd_approvals,
            "protect": cmd_protect, "unprotect": cmd_unprotect, "phone": cmd_phone, "remove": cmd_remove, "lock": cmd_lock, "grant": cmd_grant, "status": cmd_status, "env": cmd_env, "add": cmd_add, "import": cmd_import, "list": cmd_list, "rename": cmd_rename, "ref": cmd_ref,
            "run": cmd_run, "rescue": cmd_rescue}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) < 2 else 2)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
