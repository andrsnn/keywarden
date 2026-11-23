"""Run: python test_totp.py

Checks authenticator codes (TOTP) and 1Password references with a test key store and a fake `op`, so real
keys and the real authenticator secret are never touched:
- the RFC 6238 test vectors, the drift window, a used code refused, a wrong code refused;
- in totp mode a key request waits for the code, and a plain yes doesn't approve;
- keys totp setup and keys totp test refuse to run from an agent's command;
- keys add NAME --op saves only the reference and reads the value from 1Password each time.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.pop("AGENT_KEYS_CALLER", None)  # run as a person at a terminal, not as the agent running the tests

HERE = Path(__file__).resolve().parent
NS = "agent-keys-totptest"
tmp = Path(tempfile.mkdtemp(prefix="agent-keys-totp-"))
os.environ.update(AGENT_KEYS_STATE=str(tmp), AGENT_KEYS_NS=NS)
os.environ.pop("AGENT_KEYS_BACKEND", None)
sys.path.insert(0, str(HERE))
import local_store  # noqa: E402
import totp  # noqa: E402

FAKE_OP = r'''
import json, os, sys
a = sys.argv[1:]
if os.environ.get("FAKE_OP_STATE") == "signed-out":
    sys.stderr.write("[ERROR] You are not currently signed in. Please run `op signin --help` for instructions")
    sys.exit(1)
if a and a[0] == "read":
    vals = json.load(open(os.environ["FAKE_OP_VALUES"]))
    if a[1] in vals:
        print(vals[a[1]]); sys.exit(0)
    sys.stderr.write("[ERROR] could not read secret: item not found"); sys.exit(1)
sys.stderr.write("fake op: unknown " + " ".join(a)); sys.exit(1)
'''
(tmp / "fakeop.py").write_text(FAKE_OP)
import fake_model  # noqa: E402  a stand-in for the model that reads chat replies; test_intent.py checks the real call
os.environ["KEYWARDEN_CLAUDE"], FAKE_MODEL = fake_model.install_temp()
values = tmp / "op-values.json"
REF = "op://Dev Vault/Weather API/credential"
env = dict(os.environ, AGENT_KEYS_OP=f'"{sys.executable}" "{tmp / "fakeop.py"}"', FAKE_OP_VALUES=str(values))
ok, bad = [], []


def run(script, *args, stdin=None, extra=None):
    e = dict(env, **(extra or {}))
    r = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin, capture_output=True, text=True,
                       env=e, timeout=60)
    return r.returncode, r.stdout + r.stderr


def keys(*args, **kw):
    return run("keys.py", *args, **kw)


def say(text):  # the person sends a message
    return run("prompt_hook.py", stdin=json.dumps({"prompt": text}))[1]


def expect(name, cond):
    (ok if cond else bad).append(name)


def env_out():
    return keys("env")[1]


def next_code():
    """The code a person would type a little later: mark the codes before now as used, return the current one."""
    totp._save_used(totp.counter_at() - 2)
    return totp.code_at(totp.b32decode(totp.saved_secret()))


try:
    # ---------- the code itself ----------
    rfc = b"12345678901234567890"
    vectors = {59: "94287082", 1111111109: "07081804", 1111111111: "14050471", 1234567890: "89005924",
               2000000000: "69279037", 20000000000: "65353130"}
    expect("RFC 6238 SHA-1 test vectors (8 digits)", all(totp.code_at(rfc, t, 8) == c for t, c in vectors.items()))
    expect("RFC 6238 test vectors cut to 6 digits", all(totp.code_at(rfc, t) == c[-6:] for t, c in vectors.items()))
    expect("base32 secret round trip", totp.b32decode(totp.new_secret()) and len(totp.b32decode(totp.new_secret())) == 20)
    t = 1_700_000_000
    now = totp.counter_at(t)
    expect("the current code works", totp.match(rfc, totp.hotp(rfc, now), t)[0] == now)
    expect("one step behind works (clock drift)", totp.match(rfc, totp.hotp(rfc, now - 1), t)[0] == now - 1)
    expect("one step ahead works (clock drift)", totp.match(rfc, totp.hotp(rfc, now + 1), t)[0] == now + 1)
    expect("two steps behind is refused", totp.match(rfc, totp.hotp(rfc, now - 2), t)[0] is None)
    near = {totp.hotp(rfc, c) for c in range(now - 1, now + 2)}
    wrong = next(f"{n:06d}" for n in range(1000000) if f"{n:06d}" not in near)
    expect("a wrong code is refused", totp.match(rfc, wrong, t) == (None, "that code is wrong or too old"))
    expect("a code at or before the last used one is refused",
           "already used" in (totp.match(rfc, totp.hotp(rfc, now), t, last=now)[1] or "")
           and totp.match(rfc, totp.hotp(rfc, now - 1), t, last=now)[0] is None
           and totp.match(rfc, totp.hotp(rfc, now + 1), t, last=now)[0] == now + 1)
    expect("codes are found in a reply", totp.code_in("123456 2h") == "123456" and totp.code_in("here: 123 456")
           == "123456" and totp.code_in("1234567") is None and totp.code_in("approve") is None)
    expect("otpauth link has the standard fields", totp.uri("ABC", "box").startswith("otpauth://totp/keywarden%3Abox?")
           and "secret=ABC" in totp.uri("ABC") and "digits=6" in totp.uri("ABC") and "period=30" in totp.uri("ABC"))

    # The saved secret: in the key store, never in a plain file, and each code works once.
    secret = totp.new_secret()
    totp.save_secret(secret)
    key = totp.b32decode(secret)
    expect("the secret is in the key store under its own namespace", local_store.read("TOTP_SECRET", NS + "-totp")
           == secret and "TOTP_SECRET" not in local_store.names(NS))
    expect("the secret isn't written to any file", not any(secret in f.read_text(errors="replace")
                                                            for f in tmp.rglob("*") if f.is_file()))
    code = totp.code_at(key)
    expect("verify takes the right code", totp.verify(code) == (True, ""))
    expect("verify refuses the same code again", not totp.verify(code)[0] and "already used" in totp.verify(code)[1])
    expect("verify refuses a wrong code", not totp.verify(wrong if wrong != code else "000000")[0])

    # ---------- person-only commands ----------
    keys("setup", "local")
    code_, out = keys("totp", "setup", extra={"AGENT_KEYS_CALLER": "some-agent-token"})
    expect("setup is refused from an agent's command", code_ != 0 and "own terminal" in out
           and totp.saved_secret() == secret)
    code_, out = keys("totp", "setup", stdin="")
    expect("setup is refused when nobody is typing (no terminal)", code_ != 0 and "own terminal" in out
           and totp.saved_secret() == secret)
    code_, out = keys("totp", "test", stdin=totp.code_at(key) + "\n", extra={"AGENT_KEYS_CALLER": "some-agent-token"})
    expect("test is refused from an agent's command", code_ != 0 and "own terminal" in out)
    out = keys("totp")[1]
    expect("keys totp says it is set up", "set up" in out and secret not in out)

    # ---------- totp approvals ----------
    totp.delete_secret()
    code_, out = keys("approvals", "totp")
    expect("totp mode needs a secret first", code_ != 0 and "keys totp setup" in out)
    totp.save_secret(secret)
    code_, out = keys("approvals", "totp")
    expect("keys approvals totp turns it on", code_ == 0 and "6-digit code" in out)
    expect("keys approvals reports totp", "6-digit code" in keys("approvals")[1])
    keys("add", "WEATHER_KEY", stdin="wk_" + "Tq8Rz" * 6)
    code_, out = keys("grant", "WEATHER_KEY", "--for", "testing the weather")
    expect("a request asks for the code", code_ != 0 and "6-digit code from your authenticator app" in out)
    expect("no key before the code", "WEATHER_KEY=" not in env_out())
    out = say("approve")
    expect("a yes without a code doesn't approve", "6-digit code" in out and "WEATHER_KEY=" not in env_out())
    out = say("yes go ahead")
    expect("any yes without a code doesn't approve", "WEATHER_KEY=" not in env_out())
    now = totp.counter_at()
    near = {totp.hotp(key, c) for c in range(now - 1, now + 2)}
    wrong = next(f"{n:06d}" for n in range(1000000) if f"{n:06d}" not in near)
    out = say(wrong)
    expect("a wrong code says so and keeps the request waiting", "didn't work" in out
           and "WEATHER_KEY=" not in env_out() and json.loads((tmp / "pending.json").read_text()))
    good = totp.code_at(key)
    out = say(f"{good} 2h")
    expect("the right code approves, for the time given", "approved WEATHER_KEY for 2 hours" in out
           and "WEATHER_KEY=wk_" in env_out().replace("'", ""))
    keys("lock")
    keys("grant", "WEATHER_KEY", "--for", "again")
    out = say(good)
    expect("the same code can't be used twice", "already used" in out and "WEATHER_KEY=" not in env_out())
    out = say(totp.hotp(key, totp._last_used() + 1))
    expect("the next code works", "approved WEATHER_KEY" in out and "WEATHER_KEY=" in env_out())
    keys("lock")
    keys("grant", "WEATHER_KEY", "--for", "a no")
    out = say("no")
    expect("a no still turns a request down", "said no to WEATHER_KEY" in out)
    out = say("approve WEATHER_KEY for 1h")
    expect("a named approval line doesn't approve in totp mode", "approved WEATHER_KEY" not in out
           and "WEATHER_KEY=" not in env_out())
    (tmp / "pending.json").unlink(missing_ok=True)
    out = say("save this: other api key ok_" + "Zw4Kp" * 6)
    expect("a pasted key is saved but not approved in totp mode", "Saved" in out and "Approval needed" in out)

    keys("protect", "WEATHER_KEY")
    code_, out = keys("grant", "WEATHER_KEY", "--for", "protected")
    expect("a protected key asks for the code in totp mode", code_ != 0 and "6-digit code" in out)
    out = say(next_code())
    expect("a protected key takes the code", "approved WEATHER_KEY" in out and "WEATHER_KEY=" in env_out())
    keys("lock")

    code_, out = keys("approvals", "on")
    expect("leaving totp mode asks for a code", code_ != 0 and "6-digit code" in out)
    say(next_code())
    code_, out = keys("approvals", "on")
    expect("with the code, approvals go back to chat", code_ == 0 and "Approvals are on" in out)
    keys("lock")

    # ---------- 1Password references ----------
    values.write_text(json.dumps({REF: "wapi_first_value_1234567890"}))
    code_, out = keys("add", "WEATHER_API", "--op", REF)
    expect("keys add --op saves a reference", code_ == 0 and "1Password" in out and "wapi_first" not in out)
    stored = local_store.read("WEATHER_API", NS) or ""
    expect("only the reference is stored", REF in stored and "wapi_first" not in stored)
    expect("no file holds the value", not any("wapi_first" in f.read_text(errors="replace")
                                              for f in tmp.rglob("*") if f.is_file() and f.name != "op-values.json"))
    out = keys("list")[1]
    expect("keys list shows it as a 1Password reference", f"1Password reference: {REF}" in out and "wapi_" not in out)
    code_, out = keys("add", "BAD_REF", "--op", "Vault/Item")
    expect("a path that isn't op://vault/item/field is refused", code_ != 0 and "op://" in out)
    keys("add", "PLAIN_KEY", stdin="pk_" + "Hn3Vx" * 6)
    keys("grant", "WEATHER_API", "PLAIN_KEY", "--for", "the weather app")
    say("approve 1h")
    out = env_out()
    expect("hooked commands get the value read from 1Password", "WEATHER_API=wapi_first_value_1234567890" in
           out.replace("'", "") and "PLAIN_KEY=" in out)
    values.write_text(json.dumps({REF: "wapi_second_value_0987654321"}))
    expect("the value is read again each time, not kept", "wapi_second_value" in env_out())
    code_, out = keys("run", "WEATHER_API", "--", sys.executable, "-c",
                      "import os; print('len', len(os.environ['WEATHER_API']))")
    expect("keys run gets the value from 1Password", code_ == 0 and "len 28" in out)
    code_, out = run("keys.py", "env", extra={"FAKE_OP_STATE": "signed-out"})
    expect("signed out of 1Password: that key fails and says so, other keys still load",
           "WEATHER_API=" not in out and "PLAIN_KEY=" in out and "isn't signed in" in out)
    code_, out = run("keys.py", "run", "WEATHER_API", "--", sys.executable, "-c", "print('ran')",
                     extra={"FAKE_OP_STATE": "signed-out"})
    expect("keys run says 1Password isn't signed in", code_ != 0 and "isn't signed in" in out and "ran" not in out)
    code_, out = run("keys.py", "env", extra={"AGENT_KEYS_OP": str(tmp / "no-such-op.exe")})
    expect("no op CLI: that key fails and says so", "WEATHER_API=" not in out and "PLAIN_KEY=" in out
           and "1Password CLI" in out)
finally:
    for ns in (NS, NS + "-totp"):
        for n in local_store.names(ns):
            local_store.delete(n, ns)
    shutil.rmtree(tmp, ignore_errors=True)
for n in bad:
    print("WRONG:", n)
print(f"{len(ok)}/{len(ok) + len(bad)} totp and 1Password reference cases right")
raise SystemExit(1 if bad else 0)
