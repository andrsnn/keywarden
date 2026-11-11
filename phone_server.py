#!/usr/bin/env python3
"""Phone approval for keywarden: a small page your phone opens over Tailscale.

- The page listens only on this computer's Tailscale addresses, port 8460, with HTTPS from Tailscale's certificate
  for https://<this-computer>.<tailnet>.ts.net. It is never on the public internet.
- For every connection it asks Tailscale which device it comes from. Only phones and tablets (iOS, Android) signed in
  to the same Tailscale account as this computer get in. Connections from this computer itself are refused, so a
  program running here, like an AI agent, can't open the page, sign up a passkey or approve anything.
- `keys run` talks to it on 127.0.0.1:8461 with a secret from phone.json. That port only serves /health and /local/*.
- Approving needs a passkey (Face ID or fingerprint on the phone). The server checks the passkey's signature,
  so a request can't be approved by anything that doesn't hold that passkey.
- Notifications use standard web push from the home-screen app. The push carries no text; the phone fetches the
  request from this server over Tailscale before it shows the notification.

Needs: Python 3.9+, the `cryptography` package, and Tailscale with HTTPS certificates turned on.
"""
import base64
import hashlib
import json
import os
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path.home() / ".agent-keys"
DATA = STATE / "phone.json"
VAPID = STATE / "vapid.pem"
LOG = STATE / "phone-server.log"
HTTPS_PORT, LOCAL_PORT = 8460, 8461
VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
DURATIONS = (15, 60, 240, 720, 1440)  # minutes an approval can last

LOCK = threading.Condition()
PENDING = {}  # rid -> request
HOST = OWNER = None
TAILNET = {"at": 0, "peers": {}, "self_ips": set(), "owner": None}
PHONE_OS = ("iOS", "android")


# ---------- small helpers ----------

def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def log(msg):
    try:
        STATE.mkdir(exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def load():
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(d):
    STATE.mkdir(exist_ok=True)
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, DATA)


def tailscale_bin():
    for c in [shutil.which("tailscale"), r"C:\Program Files\Tailscale\tailscale.exe",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]:
        if c and Path(c).exists():
            return c
    return None


def tailscale_self():
    """(this computer's Tailscale name, the Tailscale login that owns it)"""
    ts = tailscale_bin()
    if not ts:
        raise RuntimeError("Tailscale isn't installed. Get it from https://tailscale.com/download")
    d = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True, text=True, check=True).stdout)
    me = d["Self"]
    login = (d.get("User") or {}).get(str(me.get("UserID")), {}).get("LoginName")
    return me["DNSName"].rstrip("."), login


def tailnet():
    """Who's who on the tailnet: {ip: {"user", "os", "name"}}, this computer's IPs, and the owner's user ID."""
    if time.time() - TAILNET["at"] < 20:
        return TAILNET
    d = json.loads(subprocess.run([tailscale_bin(), "status", "--json"], capture_output=True, text=True,
                                  check=True).stdout)
    me = d["Self"]
    peers = {}
    for p in (d.get("Peer") or {}).values():
        for ip in p.get("TailscaleIPs") or []:
            peers[ip] = {"user": p.get("UserID"), "os": p.get("OS"), "name": p.get("HostName")}
    TAILNET.update(at=time.time(), peers=peers, self_ips=set(me.get("TailscaleIPs") or []), owner=me.get("UserID"))
    return TAILNET


def phone_allowed(ip, net=None, allowed_os=PHONE_OS):
    """True if ip is a phone or tablet on the same Tailscale account as this computer, and not this computer."""
    net = net or tailnet()
    ip = ip.split("%")[0]
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    p = net["peers"].get(ip)
    return bool(p) and ip not in net["self_ips"] and net["owner"] is not None and p["user"] == net["owner"] \
        and p["os"] in allowed_os


def origin():
    return f"https://{HOST}:{HTTPS_PORT}"


# ---------- WebAuthn (passkeys) ----------

def cbor(b, i=0):
    """Decode one CBOR item from b at i. Enough for WebAuthn attestation objects and COSE keys."""
    ib = b[i]
    mt, ai = ib >> 5, ib & 31
    i += 1
    if ai < 24:
        val = ai
    elif ai in (24, 25, 26, 27):
        n = {24: 1, 25: 2, 26: 4, 27: 8}[ai]
        val = int.from_bytes(b[i:i + n], "big")
        i += n
    else:
        raise ValueError("unsupported CBOR")
    if mt == 0:
        return val, i
    if mt == 1:
        return -1 - val, i
    if mt == 2:
        return bytes(b[i:i + val]), i + val
    if mt == 3:
        return b[i:i + val].decode(), i + val
    if mt == 4:
        out = []
        for _ in range(val):
            x, i = cbor(b, i)
            out.append(x)
        return out, i
    if mt == 5:
        out = {}
        for _ in range(val):
            k, i = cbor(b, i)
            v, i = cbor(b, i)
            out[k] = v
        return out, i
    if mt == 7:
        return {20: False, 21: True, 22: None}.get(ai), i
    raise ValueError("unsupported CBOR")


def check_client_data(cdj, kind, challenge):
    cd = json.loads(cdj)
    if cd.get("type") != kind or cd.get("challenge") != challenge or cd.get("origin") != origin():
        raise ValueError("the passkey response doesn't match this request")


def check_auth_data(ad):
    if ad[:32] != hashlib.sha256(HOST.encode()).digest():
        raise ValueError("the passkey belongs to a different site")
    if ad[32] & 0x05 != 0x05:  # user present + user verified (Face ID, fingerprint or phone passcode)
        raise ValueError("the phone didn't verify you")


def register_passkey(body, challenge):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    check_client_data(unb64u(body["clientDataJSON"]), "webauthn.create", challenge)
    att, _ = cbor(unb64u(body["attestationObject"]))
    ad = att["authData"]
    check_auth_data(ad)
    if not ad[32] & 0x40:
        raise ValueError("the passkey response has no key in it")
    n = struct.unpack(">H", ad[53:55])[0]
    cred_id, (cose, _) = ad[55:55 + n], cbor(ad, 55 + n)
    if cose.get(3) != -7 or cose.get(-1) != 1:
        raise ValueError("unsupported passkey type")
    pub = ec.EllipticCurvePublicNumbers(int.from_bytes(cose[-2], "big"), int.from_bytes(cose[-3], "big"),
                                        ec.SECP256R1()).public_key()
    pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return {"id": b64u(cred_id), "pem": pem, "added": int(time.time())}


def verify_passkey(d, body, challenge):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    cred = next((c for c in d.get("creds", []) if c["id"] == body.get("id")), None)
    if not cred:
        raise ValueError("unknown passkey")
    cdj, ad = unb64u(body["clientDataJSON"]), unb64u(body["authenticatorData"])
    check_client_data(cdj, "webauthn.get", challenge)
    check_auth_data(ad)
    from cryptography.exceptions import InvalidSignature
    pub = serialization.load_pem_public_key(cred["pem"].encode())
    try:
        pub.verify(unb64u(body["signature"]), ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise ValueError("the passkey signature doesn't match") from None


# ---------- web push (no payload) ----------

def vapid_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    if VAPID.exists():
        return serialization.load_pem_private_key(VAPID.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    STATE.mkdir(exist_ok=True)
    VAPID.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    return key


def vapid_public():
    from cryptography.hazmat.primitives import serialization
    return b64u(vapid_key().public_key().public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint))


def vapid_jwt(aud):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    def seg(o):
        return b64u(json.dumps(o, separators=(",", ":")).encode())

    msg = seg({"typ": "JWT", "alg": "ES256"}) + "." + seg(
        {"aud": aud, "exp": int(time.time()) + 3600, "sub": f"https://{HOST}"})
    r, s = decode_dss_signature(vapid_key().sign(msg.encode(), ec.ECDSA(hashes.SHA256())))
    return msg + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def push_all():
    with LOCK:
        d = load()
    pub, dead = vapid_public(), []
    for sub in d.get("subs", []):
        ep = sub["endpoint"]
        u = urllib.parse.urlsplit(ep)
        req = urllib.request.Request(ep, data=b"", method="POST", headers={
            "TTL": "600", "Urgency": "high", "Content-Length": "0",
            "Authorization": f"vapid t={vapid_jwt(f'{u.scheme}://{u.netloc}')}, k={pub}"})
        try:
            urllib.request.urlopen(req, timeout=15).close()
        except urllib.error.HTTPError as e:
            log(f"push to {u.netloc} failed: {e.code} {e.read()[:200]!r}")
            if e.code in (404, 410):
                dead.append(ep)
        except Exception as e:
            log(f"push to {u.netloc} failed: {e}")
    if dead:
        with LOCK:
            d = load()
            d["subs"] = [s for s in d.get("subs", []) if s["endpoint"] not in dead]
            save(d)


# ---------- enrollment ----------

def new_enrollment(minutes=30):
    """Make a one-time setup link for a phone. Returns the link."""
    token = secrets.token_urlsafe(24)
    with LOCK:
        d = load()
        d["enroll"] = {"hash": hashlib.sha256(token.encode()).hexdigest(), "exp": time.time() + minutes * 60,
                       "challenge": b64u(secrets.token_bytes(32)), "user": b64u(secrets.token_bytes(16))}
        save(d)
    host, _ = tailscale_self()
    return f"https://{host}:{HTTPS_PORT}/?t={token}"


def enrollment(d, token):
    e = d.get("enroll") or {}
    if not token or e.get("exp", 0) < time.time() or \
            not secrets.compare_digest(e.get("hash", ""), hashlib.sha256(token.encode()).hexdigest()):
        return None
    return e


CERT, CERT_KEY = STATE / "tls.crt", STATE / "tls.key"


def fetch_cert(force=False):
    """Get this computer's HTTPS certificate from Tailscale. It lasts 90 days; refresh it after 30."""
    if not force and CERT.exists() and time.time() - CERT.stat().st_mtime < 30 * 86400:
        return
    ts = tailscale_bin()
    if not ts:
        raise RuntimeError("Tailscale isn't installed. Get it from https://tailscale.com/download")
    host, _ = tailscale_self()
    STATE.mkdir(exist_ok=True)
    r = subprocess.run([ts, "cert", "--cert-file", str(CERT), "--key-file", str(CERT_KEY), host],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Tailscale couldn't make an HTTPS certificate. Turn on HTTPS certificates in the "
                           f"Tailscale admin console (DNS page), then try again.\n{(r.stderr or r.stdout).strip()}")


def publish():
    """Get the certificate. Also removes the older `tailscale serve` setup, which couldn't tell devices apart."""
    unpublish()
    fetch_cert()


def unpublish():
    ts = tailscale_bin()
    if ts:
        subprocess.run([ts, "serve", f"--https={HTTPS_PORT}", "off"], capture_output=True, text=True)


# ---------- the web server ----------

def public_request(r):
    return {"rid": r["rid"], "reason": r["reason"], "durations": r["durations"], "nonce": r["nonce"],
            "created": r["created"]}


def approval_challenge(nonce, minutes):
    """What the passkey signs to approve: the request's nonce plus the chosen time, so the time can't be changed."""
    return b64u(unb64u(nonce) + f"yes:{minutes}".encode())


def expire():
    now = time.time()
    for rid in [k for k, r in PENDING.items() if r["expires"] < now]:
        PENDING.pop(rid, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-keys"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (
            json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def from_tailnet(self):
        # Only the public (Tailscale) listener serves the page, and only to the owner's phones and tablets.
        if not getattr(self.server, "public", False):
            return False
        try:
            return phone_allowed(self.client_address[0], allowed_os=tuple(load().get("devices", PHONE_OS)))
        except Exception as e:
            log(f"couldn't check who {self.client_address[0]} is: {e}")
            return False

    def from_keys(self, d):
        # Calls from `keys run` on this computer: the local listener only, and the local secret.
        return (not getattr(self.server, "public", False) and self.client_address[0] == "127.0.0.1"
                and secrets.compare_digest(self.headers.get("X-Local-Secret", ""), d.get("local_secret", "")))

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/health":
            return self.send(200, {"ok": True, "v": VERSION})
        d = load()
        if u.path == "/local/wait":
            if not self.from_keys(d):
                return self.send(403, {"error": "forbidden"})
            rid = q.get("rid", "")
            with LOCK:
                LOCK.wait_for(lambda: rid not in PENDING or PENDING[rid]["status"] != "pending", timeout=50)
                r = PENDING.get(rid)
                return self.send(200, {"status": r["status"] if r else "gone"})
        if not self.from_tailnet():
            return self.send(403, {"error": "Open this page on your phone, signed in to the same Tailscale "
                                            "account as this computer."})
        if u.path == "/":
            return self.send(200, APP_HTML.replace("__HOST__", HOST), "text/html; charset=utf-8",
                             {"Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline' 'self'; "
                                                         "style-src 'unsafe-inline'; img-src 'self' data:"})
        if u.path == "/sw.js":
            return self.send(200, SW_JS, "text/javascript; charset=utf-8")
        if u.path == "/manifest.json":
            start = "/?t=" + urllib.parse.quote(q["t"]) if q.get("t") else "/"
            return self.send(200, {"name": "Keys", "short_name": "Keys", "start_url": start, "scope": "/",
                                   "display": "standalone", "background_color": "#0d1117",
                                   "theme_color": "#0d1117",
                                   "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]},
                             "application/manifest+json")
        if u.path == "/icon.png":
            return self.send(200, ICON_PNG, "image/png")
        if u.path == "/api/pending":
            with LOCK:
                expire()
                items = [public_request(r) for r in PENDING.values() if r["status"] == "pending"]
            return self.send(200, {"pending": sorted(items, key=lambda r: r["created"]), "rpId": HOST,
                                   "credIds": [c["id"] for c in d.get("creds", [])], "host": HOST})
        if u.path == "/api/enroll":
            e = enrollment(d, q.get("t"))
            if not e:
                return self.send(410, {"error": "This setup link has expired. Run keys phone for a new one."})
            return self.send(200, {"challenge": e["challenge"], "userId": e["user"], "rpId": HOST,
                                   "userName": f"keywarden on {HOST.split('.')[0]}", "vapid": vapid_public(),
                                   "passkeys": len(d.get("creds", [])), "push": len(d.get("subs", []))})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        d = load()
        try:
            b = self.body()
        except Exception:
            return self.send(400, {"error": "bad request"})
        if u.path.startswith("/local/"):
            if not self.from_keys(d):
                return self.send(403, {"error": "forbidden"})
            if u.path == "/local/request":
                rid = secrets.token_hex(8)
                durations = [int(m) for m in b.get("durations", DURATIONS) if int(m) in DURATIONS] or [60]
                with LOCK:
                    expire()
                    PENDING[rid] = {"rid": rid, "reason": str(b.get("reason", ""))[:400], "durations": durations,
                                    "nonce": b64u(secrets.token_bytes(32)),
                                    "status": "pending", "created": time.time(),
                                    "expires": time.time() + float(b.get("timeout", 300)) + 60}
                    LOCK.notify_all()
                threading.Thread(target=push_all, daemon=True).start()
                return self.send(200, {"rid": rid})
            if u.path == "/local/cancel":
                with LOCK:
                    PENDING.pop(b.get("rid", ""), None)
                    LOCK.notify_all()
                return self.send(200, {"ok": True})
            if u.path == "/local/quit":
                self.send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.send(404, {"error": "not found"})
        if not self.from_tailnet():
            return self.send(403, {"error": "forbidden"})
        try:
            if u.path == "/api/decide":
                rid, ans = b.get("rid", ""), b.get("answer")
                with LOCK:
                    r = PENDING.get(rid)
                    if not r or r["status"] != "pending" or ans not in ("yes", "no"):
                        return self.send(410, {"error": "This request is already closed."})
                    if ans == "yes":
                        minutes = int(b.get("minutes", 0))
                        if minutes not in r["durations"]:
                            raise ValueError("pick one of the offered times")
                        verify_passkey(d, b, approval_challenge(r["nonce"], minutes))
                        ans = f"yes:{minutes}"
                    r["status"] = ans
                    LOCK.notify_all()
                log(f"request {rid}: {ans}")
                return self.send(200, {"ok": True})
            if u.path in ("/api/enroll/push", "/api/enroll/passkey"):
                with LOCK:
                    d = load()
                    e = enrollment(d, b.get("t"))
                    if not e:
                        return self.send(410, {"error": "This setup link has expired. Run keys phone for a new one."})
                    if u.path.endswith("push"):
                        sub = b.get("sub") or {}
                        if not str(sub.get("endpoint", "")).startswith("https://"):
                            raise ValueError("bad push subscription")
                        d["subs"] = [s for s in d.get("subs", []) if s["endpoint"] != sub["endpoint"]] + [sub]
                    else:
                        d.setdefault("creds", []).append(register_passkey(b, e["challenge"]))
                        e["challenge"] = b64u(secrets.token_bytes(32))  # one passkey per challenge
                    save(d)
                log(f"enrolled {u.path.rsplit('/', 1)[-1]}")
                return self.send(200, {"ok": True})
        except Exception as ex:
            log(f"{u.path} rejected: {ex}")
            return self.send(400, {"error": str(ex)})
        return self.send(404, {"error": "not found"})


class TLSServer(ThreadingHTTPServer):
    public = True
    daemon_threads = True

    def __init__(self, addr, ctx, family):
        self.address_family = family
        self.ctx = ctx
        super().__init__(addr, Handler)

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(15)  # a slow or broken TLS handshake can't hold up other visitors
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def process_request_thread(self, request, client_address):
        try:
            request.do_handshake()
        except Exception:
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)


def main():
    import socket
    import ssl
    global HOST, OWNER
    HOST, OWNER = tailscale_self()
    with LOCK:
        d = load()
        d.setdefault("local_secret", secrets.token_urlsafe(32))
        save(d)
    vapid_key()
    fetch_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, CERT_KEY)

    def renew():
        while True:
            time.sleep(86400)
            try:
                fetch_cert()
                ctx.load_cert_chain(CERT, CERT_KEY)
            except Exception as e:
                log(f"certificate refresh failed: {e}")

    threading.Thread(target=renew, daemon=True).start()
    servers = []
    for ip in sorted(tailnet()["self_ips"]):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        try:
            servers.append(TLSServer((ip, HTTPS_PORT), ctx, fam))
        except OSError as e:
            log(f"couldn't listen on {ip}:{HTTPS_PORT}: {e}")
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    local = ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), Handler)
    local.daemon_threads = True
    log(f"listening on {', '.join(str(s.server_address[0]) for s in servers)}:{HTTPS_PORT} for {origin()}, "
        f"local 127.0.0.1:{LOCAL_PORT}, version {VERSION}")
    local.serve_forever()
    for srv in servers:
        srv.shutdown()


# ---------- the phone page ----------

def _png(size=192, rgb=(13, 17, 23), fg=(63, 185, 80)):
    """A plain square icon: dark background with a green key-shaped mark."""
    import zlib
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            cx, cy = x - size * 0.38, y - size * 0.5
            ring = (size * 0.10) ** 2 < cx * cx + cy * cy < (size * 0.20) ** 2
            shaft = size * 0.55 <= x <= size * 0.80 and abs(y - size * 0.5) < size * 0.045
            teeth = (size * 0.66 <= x <= size * 0.71 or size * 0.75 <= x <= size * 0.80) and \
                size * 0.5 <= y <= size * 0.62
            row += bytes(fg if (ring or shaft or teeth) else rgb)
        rows.append(bytes(row))

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


ICON_PNG = _png()

SW_JS = r"""
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("push", e => e.waitUntil((async () => {
  let title = "A program wants a key", body = "Open Keys to approve or deny it.";
  try {
    const r = await fetch("/api/pending", {cache: "no-store"});
    const j = await r.json();
    if (j.pending.length) { title = "Approve key use?"; body = j.pending[j.pending.length - 1].reason; }
  } catch (_) {}
  await self.registration.showNotification(title, {body, tag: "keys", renotify: true, icon: "/icon.png"});
})()));
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(ws =>
    ws.length ? ws[0].focus() : self.clients.openWindow("/")));
});
"""

APP_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Keys">
<meta name="theme-color" content="#0d1117">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="manifest" id="manifest" href="/manifest.json">
<title>Keys</title>
<style>
:root { --bg:#f6f8fa; --card:#fff; --text:#1f2328; --muted:#59636e; --line:#d0d7de; --go:#1a7f37; --no:#d1242f; --blue:#0969da; }
@media (prefers-color-scheme: dark) { :root { --bg:#0d1117; --card:#161b22; --text:#e6edf3; --muted:#9198a1; --line:#3d444d; --go:#238636; --no:#da3633; --blue:#4493f8; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:16px/1.45 -apple-system, system-ui, sans-serif;
  padding: calc(env(safe-area-inset-top) + 16px) 16px calc(env(safe-area-inset-bottom) + 24px); }
h1 { font-size:22px; margin:0 0 2px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:18px; word-break:break-all; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; margin-bottom:14px; }
.reason { font-size:15px; margin:0 0 6px; word-break:break-word; }
.when { color:var(--muted); font-size:13px; margin-bottom:14px; }
button { display:block; width:100%; border:0; border-radius:10px; padding:14px; font-size:16px; font-weight:600;
  margin-top:8px; color:#fff; background:var(--go); }
button.alt { background:transparent; color:var(--blue); border:1px solid var(--line); }
button.no { background:transparent; color:var(--no); border:1px solid var(--line); }
button:disabled { opacity:.5; }
.empty { color:var(--muted); text-align:center; padding:40px 0; }
.err { color:var(--no); font-size:14px; margin-top:10px; }
ol { padding-left:20px; } li { margin-bottom:8px; }
.ok { color:var(--go); font-weight:600; }
.label { color:var(--muted); font-size:13px; margin:4px 0 6px; }
.times { display:flex; gap:6px; flex-wrap:wrap; }
.times button { flex:1 1 0; min-width:56px; margin:0; padding:10px 4px; font-size:14px; font-weight:600;
  background:transparent; color:var(--text); border:1px solid var(--line); }
.times button.on { background:var(--blue); color:#fff; border-color:var(--blue); }
</style></head>
<body>
<h1>Keys</h1>
<div class="sub">__HOST__</div>
<div id="main"></div>
<script>
const $ = s => document.querySelector(s);
const main = $("#main");
let T = new URLSearchParams(location.search).get("t");
if (T) $("#manifest").href = "/manifest.json?t=" + encodeURIComponent(T);
const enc = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
const dec = s => Uint8Array.from(atob(s.replace(/-/g,"+").replace(/_/g,"/") + "===".slice((s.length + 3) % 4)), c => c.charCodeAt(0));
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(path, body) {
  const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {cache:"no-store"});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("Error " + r.status));
  return j;
}
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);

async function setup() {
  let o;
  try { o = await api("/api/enroll?t=" + encodeURIComponent(T)); } catch (e) { T = null; return pending(); }
  if (isIOS && !standalone) {
    main.innerHTML = `<div class="card"><p class="reason">Add Keys to your home screen first:</p><ol>
      <li>Tap the Share button at the bottom of Safari.</li><li>Tap Add to Home Screen, then Add.</li>
      <li>Open Keys from your home screen to finish.</li></ol></div>`;
    return;
  }
  main.innerHTML = `<div class="card">
    <p class="reason">Set up this phone to approve key use.</p>
    <button id="push">${o.push ? "Notifications are on (tap to redo)" : "1. Turn on notifications"}</button>
    <button id="pk">${o.passkeys ? "Add another passkey" : "2. Create a passkey with Face ID"}</button>
    <div id="msg"></div></div>`;
  const msg = (t, ok) => $("#msg").innerHTML = `<p class="${ok ? "ok" : "err"}">${esc(t)}</p>`;
  $("#push").onclick = async () => {
    try {
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      if (await Notification.requestPermission() !== "granted") throw new Error("Notifications are off for Keys. Turn them on in Settings.");
      const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: dec(o.vapid)});
      await api("/api/enroll/push", {t: T, sub: sub.toJSON()});
      $("#push").textContent = "Notifications are on"; msg("Notifications are on.", true);
    } catch (e) { msg(e.message); }
  };
  $("#pk").onclick = async () => {
    try {
      const fresh = await api("/api/enroll?t=" + encodeURIComponent(T));
      const c = await navigator.credentials.create({publicKey: {
        rp: {id: fresh.rpId, name: "keywarden"},
        user: {id: dec(fresh.userId), name: fresh.userName, displayName: fresh.userName},
        challenge: dec(fresh.challenge), pubKeyCredParams: [{type: "public-key", alg: -7}],
        authenticatorSelection: {residentKey: "preferred", userVerification: "required"},
        attestation: "none", timeout: 120000}});
      await api("/api/enroll/passkey", {t: T, attestationObject: enc(c.response.attestationObject), clientDataJSON: enc(c.response.clientDataJSON)});
      msg("Done. This phone can approve key use now. You can close Keys.", true);
      $("#pk").textContent = "Passkey saved";
    } catch (e) { msg(e.message); }
  };
}

let P = null, busy = false, chosen = 60;
async function pending() {
  if (busy) return;
  try { P = await api("/api/pending"); } catch (e) { main.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  if (!P.pending.length) { main.innerHTML = `<div class="empty">Nothing to approve right now.</div>`; return; }
  const label = m => m < 60 ? m + " min" : (m / 60) + (m === 60 ? " hour" : " hours");
  main.innerHTML = P.pending.map(r => `<div class="card" data-rid="${r.rid}">
      <p class="reason">${esc(r.reason)}</p>
      <div class="when">Asked at ${new Date(r.created * 1000).toLocaleTimeString()}</div>
      <div class="label">Allow these keys for</div>
      <div class="times">${r.durations.map(m => `<button data-m="${m}" class="${m === chosen ? "on" : ""}">${label(m)}</button>`).join("")}</div>
      <button data-a="yes">Allow</button>
      <button class="no" data-a="no">Deny</button>
      <div class="msg"></div></div>`).join("");
  main.querySelectorAll(".times button").forEach(b => b.onclick = () => {
    chosen = +b.dataset.m;
    b.parentNode.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  });
  main.querySelectorAll("button[data-a]").forEach(b => b.onclick = () => decide(b.closest(".card"), b.dataset.a));
}
const label2 = m => m < 60 ? m + " minutes" : (m / 60) + (m === 60 ? " hour" : " hours");
async function decide(card, a) {
  const r = P.pending.find(x => x.rid === card.dataset.rid);
  busy = true; card.querySelectorAll("button").forEach(b => b.disabled = true);
  try {
    const body = {rid: r.rid, answer: a, minutes: chosen};
    if (a === "yes") {
      const tail = new TextEncoder().encode("yes:" + chosen), n = dec(r.nonce);
      const challenge = new Uint8Array(n.length + tail.length); challenge.set(n); challenge.set(tail, n.length);
      const s = await navigator.credentials.get({publicKey: {challenge, rpId: P.rpId,
        allowCredentials: P.credIds.map(id => ({type: "public-key", id: dec(id)})), userVerification: "required", timeout: 60000}});
      Object.assign(body, {id: s.id, authenticatorData: enc(s.response.authenticatorData),
        clientDataJSON: enc(s.response.clientDataJSON), signature: enc(s.response.signature)});
    }
    await api("/api/decide", body);
    card.innerHTML = `<p class="ok">${a === "no" ? "Denied." : "Approved for " + label2(chosen) + "."}</p>`;
  } catch (e) {
    card.querySelector(".msg").innerHTML = `<p class="err">${esc(e.message)}</p>`;
    card.querySelectorAll("button").forEach(b => b.disabled = false);
  }
  busy = false; setTimeout(pending, 1500);
}
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
T ? setup() : pending();
setInterval(() => { if (!T && document.visibilityState === "visible") pending(); }, 3000);
document.addEventListener("visibilitychange", () => { if (!T && document.visibilityState === "visible") pending(); });
</script>
</body></html>"""

if __name__ == "__main__":
    main()
