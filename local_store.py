"""This computer's own key store: Windows Credential Manager or the macOS Keychain.

Every function takes `ns`, a namespace, so the saved keys ("agent-keys") and the short-lived copies made while
an approval lasts ("agent-keys-cache") never mix. AGENT_KEYS_NS changes the default, which the tests use.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

NS = os.environ.get("AGENT_KEYS_NS", "agent-keys")
STATE = Path(os.environ.get("AGENT_KEYS_STATE") or Path.home() / ".agent-keys")


def _index(ns, update=None, remove=None):
    """Key names for the Mac, where the Keychain can't list items cheaply."""
    f = STATE / f"names-{ns}.json"
    names = set(json.loads(f.read_text())) if f.exists() else set()
    if update:
        names.add(update)
    if remove:
        names.discard(remove)
    if update or remove:
        STATE.mkdir(exist_ok=True)
        f.write_text(json.dumps(sorted(names)))
    return names


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    _adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    _adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _adv.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))]
    _adv.CredFree.argtypes = [ctypes.c_void_p]
    CHUNK = 2400  # Credential Manager holds at most 2560 bytes per entry, so long tokens are split

    def _write1(target, data, ns):
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        c = CREDENTIAL(Type=1, TargetName=target, CredentialBlobSize=len(data),
                       CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                       Persist=2, UserName=ns)  # Persist=2: this computer only, never roams
        if not _adv.CredWriteW(ctypes.byref(c), 0):
            raise OSError(ctypes.get_last_error(), "Credential Manager wouldn't save the key")

    def _read1(target):
        p = ctypes.POINTER(CREDENTIAL)()
        if not _adv.CredReadW(target, 1, 0, ctypes.byref(p)):
            return None
        try:
            return ctypes.string_at(p.contents.CredentialBlob, p.contents.CredentialBlobSize)
        finally:
            _adv.CredFree(p)

    def save(name, value, ns=None):
        ns = ns or NS
        delete(name, ns)
        data = value.encode("utf-8")
        parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        for i, part in enumerate(parts):
            _write1(f"{ns}:{name}" + (f"#{i}" if i else ""), part, ns)

    def read(name, ns=None):
        ns = ns or NS
        first = _read1(f"{ns}:{name}")
        if first is None:
            return None
        out, i = [first], 1
        while (more := _read1(f"{ns}:{name}#{i}")) is not None:
            out.append(more)
            i += 1
        return b"".join(out).decode("utf-8")

    def delete(name, ns=None):
        ns = ns or NS
        _adv.CredDeleteW(f"{ns}:{name}", 1, 0)
        i = 1
        while _adv.CredDeleteW(f"{ns}:{name}#{i}", 1, 0):
            i += 1

    def names(ns=None):
        ns = ns or NS
        count, creds = wintypes.DWORD(), ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
        if not _adv.CredEnumerateW(f"{ns}:*", 0, ctypes.byref(count), ctypes.byref(creds)):
            return set()
        try:
            found = {creds[i].contents.TargetName[len(ns) + 1:] for i in range(count.value)}
        finally:
            _adv.CredFree(creds)
        return {n for n in found if "#" not in n}

elif sys.platform == "darwin":
    def _sec(*args, input=None):
        return subprocess.run(["security", *args], input=input, capture_output=True, text=True)

    def save(name, value, ns=None):
        ns = ns or NS
        # `security -i` reads the command from stdin, so the key never shows up in the process list.
        quoted = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        r = _sec("-i", input=f"add-generic-password -U -s {ns} -a {name} -w {quoted}\n")
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            raise OSError(r.stderr.strip() or "the Keychain wouldn't save the key")
        _index(ns, update=name)

    def read(name, ns=None):
        r = _sec("find-generic-password", "-s", ns or NS, "-a", name, "-w")
        return r.stdout.rstrip("\n") if r.returncode == 0 else None

    def delete(name, ns=None):
        ns = ns or NS
        _sec("delete-generic-password", "-s", ns, "-a", name)
        _index(ns, remove=name)

    def names(ns=None):
        return _index(ns or NS)

else:
    def _missing(*a, **k):
        raise SystemExit("keys: this computer's key store works on Windows and macOS. On Linux, use 1Password.")
    save = read = delete = names = _missing
