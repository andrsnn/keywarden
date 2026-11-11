"""The two places keys can live. Both have the same five calls, so the rest of keywarden doesn't care which one.

- OnePassword: a vault in 1Password (preferred: it backs keys up and shares them with a team).
- Local: this computer's own key store (Windows Credential Manager or the macOS Keychain). No account needed.

While an approval lasts with 1Password, keys are copied into the local store under a separate name ("cache"),
so each command gets them fast without a 1Password prompt. The copies are deleted when the approval ends.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import local_store

STATE = Path(os.environ.get("AGENT_KEYS_STATE") or Path.home() / ".agent-keys")
CONFIG = STATE / "config.json"
CACHE_NS = local_store.NS + "-cache"
CACHE_INFO = STATE / "cache.json"  # which approval the copies belong to, and their names
FIELD = "credential"
IS_WIN = os.name == "nt"


class StoreError(Exception):
    pass


def strict():
    """Strict mode: keys only come from 1Password, read fresh each time, so 1Password's own Touch ID or
    Windows Hello check guards every read. No copies on this computer and no saved service account token."""
    return bool(config().get("strict"))


def config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


class Local:
    kind = "local"
    label = "Windows Credential Manager" if IS_WIN else "the macOS Keychain"

    def names(self):
        return set(local_store.names())

    def read(self, name):
        return local_store.read(name)

    def save(self, name, value):
        local_store.save(name, value)

    def delete(self, name):
        local_store.delete(name)

    def rename(self, old, new):
        v = local_store.read(old)
        if v is None:
            raise StoreError(f"there's no key named {old}.")
        local_store.save(new, v)
        local_store.delete(old)


def op_command():
    """The 1Password CLI. AGENT_KEYS_OP replaces it (the tests use a fake one)."""
    if os.environ.get("AGENT_KEYS_OP"):
        return [t.strip('"') for t in shlex.split(os.environ["AGENT_KEYS_OP"], posix=not IS_WIN)]
    p = shutil.which("op")
    if p:
        return [p]
    if IS_WIN:  # winget adds op to PATH only for shells opened after the install
        winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
        for c in [winget / "Links" / "op.exe", *winget.glob("Packages/AgileBits.1Password.CLI*/op.exe")]:
            if c.exists():
                return [str(c)]
    return None


class OnePassword:
    kind = "1password"
    label = "1Password"

    def __init__(self, vault=None):
        self.vault = vault or os.environ.get("AGENT_KEYS_VAULT") or config().get("vault") or "Agent Keys"

    def op(self, *args, input=None, check=True):
        cmd = op_command()
        if not cmd:
            raise StoreError("the 1Password CLI (op) is missing. Install it: winget install AgileBits.1Password.CLI "
                             "(Mac: brew install 1password-cli)")
        env = dict(os.environ)
        if strict():
            env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)  # strict: always go through the 1Password app's check
        elif "OP_SERVICE_ACCOUNT_TOKEN" not in env and (IS_WIN or sys.platform == "darwin"):
            token = local_store.read("OP_SERVICE_ACCOUNT_TOKEN")  # lets op run without a 1Password app prompt
            if token:
                env["OP_SERVICE_ACCOUNT_TOKEN"] = token
        r = subprocess.run(cmd + list(args), input=input, capture_output=True, text=True, encoding="utf-8", env=env)
        if check and r.returncode != 0:
            raise StoreError(f"1Password said: {(r.stderr or r.stdout).strip()}")
        return r

    def connected(self):
        try:
            return self.op("vault", "list", "--format", "json", check=False).returncode == 0
        except StoreError:
            return False

    def ensure_vault(self):
        vaults = {v["name"] for v in json.loads(self.op("vault", "list", "--format", "json").stdout or "[]")}
        if self.vault not in vaults:
            self.op("vault", "create", self.vault)
            return True
        return False

    def names(self):
        items = json.loads(self.op("item", "list", "--vault", self.vault, "--format", "json").stdout or "[]")
        return {i["title"] for i in items}

    def read(self, name):
        r = self.op("read", f"op://{self.vault}/{name}/{FIELD}", check=False)
        return r.stdout.rstrip("\n") if r.returncode == 0 else None

    def save(self, name, value):
        if name in self.names():
            self.op("item", "delete", name, "--vault", self.vault, "--archive")
        item = {"title": name, "category": "API_CREDENTIAL", "notesPlain": "Saved by keywarden.",
                "fields": [{"id": FIELD, "label": FIELD, "type": "CONCEALED", "value": value}]}
        self.op("item", "create", "--vault", self.vault, "--format", "json", input=json.dumps(item))

    def delete(self, name):
        self.op("item", "delete", name, "--vault", self.vault, "--archive")

    def rename(self, old, new):
        self.op("item", "edit", old, "--vault", self.vault, "--title", new)


def current():
    kind = os.environ.get("AGENT_KEYS_BACKEND") or config().get("backend") or "local"
    return OnePassword() if kind == "1password" else Local()


def by_kind(kind, vault=None):
    return OnePassword(vault) if kind == "1password" else Local()


# ---------- copies made while an approval lasts (1Password only) ----------

def cache_fill(store, approved):
    if strict():
        cache_clear()
        return
    """Copy the approved keys from 1Password into the local store. approved: {name: expiry}.
    Keys that are no longer approved lose their copies."""
    info = _cache_info()
    have = set(info.get("names", []))
    want = set(approved) & set(store.names()) if set(approved) - have else set(approved) & have
    for n in have - want:
        local_store.delete(n, CACHE_NS)
    for n in sorted(want - have):
        v = store.read(n)
        if v is not None:
            local_store.save(n, v, CACHE_NS)
    STATE.mkdir(exist_ok=True)
    CACHE_INFO.write_text(json.dumps({"names": sorted(want)}))


def cache_read(name):
    return local_store.read(name, CACHE_NS)


def cache_names():
    return _cache_info().get("names", [])


def cache_clear():
    info = _cache_info()
    for n in set(info.get("names", [])) | set(local_store.names(CACHE_NS)):
        local_store.delete(n, CACHE_NS)
    CACHE_INFO.unlink(missing_ok=True)


def _cache_info():
    try:
        return json.loads(CACHE_INFO.read_text())
    except Exception:
        return {}
