"""Run: python test_phone.py   Checks passkey sign-up and approval checks with a software passkey."""
import hashlib
import json
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import phone_server as ps


def cbor_enc(x):
    def head(mt, n):
        if n < 24:
            return bytes([mt << 5 | n])
        for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([mt << 5 | ai]) + n.to_bytes(size, "big")
    if isinstance(x, bool):
        return bytes([0xf5 if x else 0xf4])
    if isinstance(x, int):
        return head(0, x) if x >= 0 else head(1, -1 - x)
    if isinstance(x, bytes):
        return head(2, len(x)) + x
    if isinstance(x, str):
        return head(3, len(x.encode())) + x.encode()
    if isinstance(x, list):
        return head(4, len(x)) + b"".join(cbor_enc(i) for i in x)
    if isinstance(x, dict):
        return head(5, len(x)) + b"".join(cbor_enc(k) + cbor_enc(v) for k, v in x.items())
    raise TypeError(x)


class SoftPasskey:
    """Acts like a phone's passkey: makes WebAuthn responses for a site."""

    def __init__(self, host, origin):
        self.host, self.origin = host, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = b"soft-passkey-0001"

    def rp_hash(self):
        return hashlib.sha256(self.host.encode()).digest()

    def create(self, challenge):
        n = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        ad = self.rp_hash() + bytes([0x45]) + struct.pack(">I", 0) + bytes(16) + \
            struct.pack(">H", len(self.cred_id)) + self.cred_id + cbor_enc(cose)
        cdj = json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": self.origin}).encode()
        return {"attestationObject": ps.b64u(cbor_enc({"fmt": "none", "attStmt": {}, "authData": ad})),
                "clientDataJSON": ps.b64u(cdj)}

    def get(self, challenge, flags=0x05):
        ad = self.rp_hash() + bytes([flags]) + struct.pack(">I", 1)
        cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": self.origin}).encode()
        sig = self.key.sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": ps.b64u(self.cred_id), "authenticatorData": ps.b64u(ad),
                "clientDataJSON": ps.b64u(cdj), "signature": ps.b64u(sig)}


def main():
    ps.HOST = "pc.example.ts.net"
    pk = SoftPasskey(ps.HOST, ps.origin())
    cred = ps.register_passkey(pk.create("chal-1"), "chal-1")
    d = {"creds": [cred]}
    ok, bad = [], []

    def expect(name, fn, should_pass):
        try:
            fn()
            (ok if should_pass else bad).append(name)
        except Exception:
            (bad if should_pass else ok).append(name)

    expect("good approval", lambda: ps.verify_passkey(d, pk.get("c2"), "c2"), True)
    expect("wrong challenge", lambda: ps.verify_passkey(d, pk.get("c2"), "c3"), False)
    expect("no Face ID (UV flag off)", lambda: ps.verify_passkey(d, pk.get("c2", flags=0x01), "c2"), False)
    other = SoftPasskey(ps.HOST, ps.origin())
    expect("unknown passkey", lambda: ps.verify_passkey(d, other.get("c2"), "c2"), False)
    forged = dict(pk.get("c2"), signature=other.get("c2")["signature"])
    expect("forged signature", lambda: ps.verify_passkey(d, forged, "c2"), False)
    wrong_site = SoftPasskey("evil.example.com", ps.origin())
    wrong_site.key, wrong_site.cred_id = pk.key, pk.cred_id
    expect("passkey for another site", lambda: ps.verify_passkey(d, wrong_site.get("c2"), "c2"), False)
    wrong_origin = SoftPasskey(ps.HOST, "https://evil.example.com")
    wrong_origin.key, wrong_origin.cred_id = pk.key, pk.cred_id
    expect("page on another origin", lambda: ps.verify_passkey(d, wrong_origin.get("c2"), "c2"), False)
    expect("sign-up with wrong challenge", lambda: ps.register_passkey(pk.create("x"), "y"), False)

    # The push token must be a valid ES256 JWT signed by the VAPID key.
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    head, claims, sig = ps.vapid_jwt("https://web.push.apple.com").split(".")
    raw = ps.unb64u(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ps.unb64u(ps.vapid_public()))
    expect("push token signature", lambda: pub.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())), True)

    # Who may open the page: only the owner's phones and tablets, never this computer or another computer.
    net = {"owner": 7, "self_ips": {"100.0.0.1", "fd7a::1"}, "peers": {
        "100.0.0.2": {"user": 7, "os": "iOS"}, "100.0.0.3": {"user": 7, "os": "windows"},
        "100.0.0.4": {"user": 9, "os": "iOS"}, "100.0.0.5": {"user": 7, "os": "android"},
        "100.0.0.1": {"user": 7, "os": "iOS"}}}
    expect("owner's iPhone allowed", lambda: ps.phone_allowed("100.0.0.2", net) or 1 / 0, True)
    expect("owner's Android allowed", lambda: ps.phone_allowed("::ffff:100.0.0.5", net) or 1 / 0, True)
    expect("owner's laptop refused", lambda: ps.phone_allowed("100.0.0.3", net) or 1 / 0, False)
    expect("someone else's iPhone refused", lambda: ps.phone_allowed("100.0.0.4", net) or 1 / 0, False)
    expect("this computer refused", lambda: ps.phone_allowed("100.0.0.1", net) or 1 / 0, False)
    expect("unknown address refused", lambda: ps.phone_allowed("127.0.0.1", net) or 1 / 0, False)
    expect("no owner known: refused", lambda: ps.phone_allowed("100.0.0.2", dict(net, owner=None)) or 1 / 0, False)

    for name in bad:
        print("WRONG:", name)
    print(f"{len(ok)}/{len(ok) + len(bad)} phone cases right")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
