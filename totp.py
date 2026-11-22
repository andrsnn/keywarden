"""One-time codes from an authenticator app (TOTP, RFC 6238): 6 digits, a new one every 30 seconds, SHA-1.

`keys totp setup` makes a secret and keeps it in this computer's key store under its own namespace
("agent-keys-totp"), apart from the saved keys, so `keys list`, `keys env` and grants never see it.
With `keys approvals totp`, a key request waits until the person types the current code in the chat.

A code is accepted for the current 30 seconds and one step either side (clock drift). Each code works once:
the last counter used is kept in ~/.agent-keys/totp.json, and a code at or before it is refused.
Standard library only.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import struct
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_store  # noqa: E402

STATE = Path(os.environ.get("AGENT_KEYS_STATE") or Path.home() / ".agent-keys")
NS = local_store.NS + "-totp"
SECRET_NAME = "TOTP_SECRET"
USED = STATE / "totp.json"  # {"last": the last counter a code was accepted for}
STEP = 30
DIGITS = 6
DRIFT = 1  # steps either side of now
CODE = re.compile(r"(?<!\d)(\d{3})[ -]?(\d{3})(?!\d)")


def hotp(key, counter, digits=DIGITS):
    """RFC 4226: the code for one counter value."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(n % 10 ** digits).zfill(digits)


def counter_at(t=None):
    return int((time.time() if t is None else t) // STEP)


def code_at(key, t=None, digits=DIGITS):
    return hotp(key, counter_at(t), digits)


def b32decode(secret):
    s = re.sub(r"[\s-]", "", secret).upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def new_secret():
    """A random 160-bit secret, as base32 without padding (what authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def uri(secret, account=None):
    account = account or socket.gethostname()
    label = urllib.parse.quote(f"keywarden:{account}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer=keywarden&algorithm=SHA1"
            f"&digits={DIGITS}&period={STEP}")


def qr_text(text):
    """A QR code drawn in the terminal, if a QR library is already installed. None otherwise."""
    try:
        import segno
        import io
        buf = io.StringIO()
        segno.make(text, error="m").terminal(out=buf, compact=True)
        return buf.getvalue()
    except Exception:
        pass
    try:
        import io
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(text)
        buf = io.StringIO()
        q.print_ascii(out=buf, invert=True)
        return buf.getvalue()
    except Exception:
        return None


# ---------- the saved secret ----------

def saved_secret():
    try:
        return local_store.read(SECRET_NAME, NS)
    except BaseException:
        return None


def save_secret(secret):
    local_store.save(SECRET_NAME, secret, NS)
    _save_used(None)  # a new secret starts with no codes used


def delete_secret():
    local_store.delete(SECRET_NAME, NS)
    USED.unlink(missing_ok=True)


def is_set_up():
    return bool(saved_secret())


def _last_used():
    try:
        return int(json.loads(USED.read_text(encoding="utf-8")).get("last"))
    except Exception:
        return None


def _save_used(counter):
    STATE.mkdir(exist_ok=True)
    if counter is None:
        USED.unlink(missing_ok=True)
    else:
        USED.write_text(json.dumps({"last": counter}), encoding="utf-8")


# ---------- checking a code ----------

def code_in(text):
    """The 6-digit code in a message ("123456" or "123 456"), or None. A message with two codes has none."""
    found = {a + b for a, b in CODE.findall(text or "")}
    return found.pop() if len(found) == 1 else None


def match(key, code, t=None, last=None, drift=DRIFT):
    """(counter, None) for a right code, or (None, why). Refuses a code at or before `last`."""
    code = re.sub(r"[\s-]", "", code or "")
    if not re.fullmatch(r"\d{%d}" % DIGITS, code):
        return None, "that isn't a 6-digit code"
    now = counter_at(t)
    for c in range(now - drift, now + drift + 1):
        if hmac.compare_digest(hotp(key, c), code):
            if last is not None and c <= last:
                return None, "that code was already used. Wait for the next one"
            return c, None
    return None, "that code is wrong or too old"


def verify(code, t=None, secret=None):
    """Check a code against the saved secret and use it up. (True, "") or (False, why)."""
    secret = secret or saved_secret()
    if not secret:
        return False, "no authenticator is set up. Run keys totp setup in your own terminal"
    try:
        key = b32decode(secret)
    except Exception:
        return False, "the saved authenticator secret is damaged. Run keys totp setup again"
    counter, why = match(key, code, t, _last_used())
    if counter is None:
        return False, why
    _save_used(counter)
    return True, ""
