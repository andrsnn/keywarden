"""Run: python test_send.py   Checks sending a key in the chat: a key sent on its own is saved and taken out of the
message, IDs aren't saved, a key the agent asked for by name replaces the old one, and keys forget and keys add
--clipboard behave. Uses a test key store and made-up values only."""
import hashlib
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

import fake_model  # noqa: E402  a stand-in for the model; test_intent.py checks the real call

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="agent-keys-send-"))
os.environ.pop("AGENT_KEYS_CALLER", None)  # run as a person at a terminal
os.environ.update(AGENT_KEYS_STATE=str(tmp), AGENT_KEYS_NS="agent-keys-sendtest")
os.environ.pop("AGENT_KEYS_BACKEND", None)
rnd = random.Random(11)
b64 = "".join(rnd.choice(string.ascii_letters + string.digits + "+/") for _ in range(43))
while not ("+" in b64 and "/" in b64):
    b64 = "".join(rnd.choice(string.ascii_letters + string.digits + "+/") for _ in range(43))
b64_new = "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(43))
product_id = "".join(rnd.choice("0123456789abcdef") for _ in range(32))
plain = "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(40))
# What the model says about a value: a message the stand-in doesn't know gets no answer, so IDs are dropped.
SECRET_HEX = f"here's the epic client secret, save it: [VALUE1]"
fake_exe, FAKE = fake_model.install_temp({SECRET_HEX: {"VALUE1": {"secret": True, "name": "EPIC_CLIENT_SECRET"}}})
os.environ["KEYWARDEN_CLAUDE"] = fake_exe
env = dict(os.environ)
ok, bad = [], []


def run(script, *args, stdin=None, extra=None):
    r = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin, capture_output=True, text=True,
                       env=dict(env, **(extra or {})), timeout=90)
    return r.returncode, r.stdout + r.stderr


def transcript(agent_says):
    path = tmp / f"session-{rnd.randrange(10**9)}.jsonl"
    line = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": agent_says}]}}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return str(path)


def send(text, agent_says=None, session="send-test-session"):
    """The person sends a message through the plugin path. Returns its JSON answer."""
    data = {"prompt": text, "session_id": session}
    if agent_says:
        data["transcript_path"] = transcript(agent_says)
    code, out = run("prompt_hook.py", "--rewrite", stdin=json.dumps(data))
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": out}


def saved_as(value):
    saved = json.loads((tmp / "saved.json").read_text(encoding="utf-8")) if (tmp / "saved.json").exists() else {}
    return saved.get(hashlib.sha256(value.encode()).hexdigest())


def clear_store():
    sys.path.insert(0, str(HERE))
    import local_store  # noqa: E402  only this test's namespace
    for n in local_store.names("agent-keys-sendtest"):
        local_store.delete(n, "agent-keys-sendtest")


def expect(name, cond):
    (ok if cond else bad).append(name)


try:
    clear_store()  # a run that was stopped part way can leave keys behind
    run("keys.py", "setup", "local")
    run("keys.py", "approvals", "on")

    out = send(b64)
    expect("a bare 43-character base64 key is saved", saved_as(b64) == "SENT_KEY")
    expect("the bare key is taken out of the message", out.get("text") == "[SENT_KEY]" and b64 not in json.dumps(out))
    expect("the agent is told it can rename the placeholder", "keys rename" in out.get("context", ""))

    out = send("key DEMO_SEND_KEY " + plain)
    expect("key NAME value still works", saved_as(plain) == "DEMO_SEND_KEY" and out.get("text") == "key DEMO_SEND_KEY [DEMO_SEND_KEY]")

    for msg in (f"the Epic product ID is {product_id}, put it in the config",
                f"Hard Burn's product ID portal key: {product_id}",
                f"store it in the config: the sandbox id is {product_id}"):
        out = send(msg)
        expect(f"a 32-hex product ID is not saved: {msg[:30]}", saved_as(product_id) is None and not out.get("saved")
               and out.get("text") == msg)
    hex_secret = "".join(rnd.choice("0123456789abcdef") for _ in range(40))
    out = send(f"here's the epic client secret, save it: {hex_secret}")
    expect("hex the model reads as a secret is saved under its name", saved_as(hex_secret) == "EPIC_CLIENT_SECRET")

    run("keys.py", "add", "HARD_BURN_EOS_CLIENT_SECRET", stdin=plain + "x")
    out = send(b64_new, agent_says="That one failed. Send the new client secret and I'll save it as "
                                   "HARD_BURN_EOS_CLIENT_SECRET.")
    expect("a key the agent asked for by name replaces the old value without approval",
           saved_as(b64_new) == "HARD_BURN_EOS_CLIENT_SECRET" and "replaced" in out.get("notice", ""))

    sys.path.insert(0, str(HERE))
    import approve  # noqa: E402  uses this test's key store folder
    token = approve.token_for("send-test-session")
    code, got = run("keys.py", "forget", "SENT_KEY", extra={"AGENT_KEYS_CALLER": token})
    expect("keys forget drops the agent's own save from this session", code == 0 and saved_as(b64) is None
           and "SENT_KEY" not in run("keys.py", "list")[1])
    other = approve.token_for("another-session")
    code, got = run("keys.py", "forget", "DEMO_SEND_KEY", extra={"AGENT_KEYS_CALLER": other})
    expect("keys forget asks before deleting another session's save", code != 0 and saved_as(plain) == "DEMO_SEND_KEY")

    import keys  # noqa: E402
    log = "\n".join(f"2026-11-26T10:{i:02d}:00Z INFO build step {i} finished in {i * 7} ms" for i in range(14))
    log = (log + "\n" + "x" * 926)[:926]
    keys.read_clipboard = lambda: log
    try:
        keys.cmd_add(["LOG_PASTE", "--clipboard"])
        refused = False
    except SystemExit as e:
        refused = e.code != 0
    expect("keys add --clipboard refuses a 926-character log", refused and "LOG_PASTE" not in keys.existing_names())
finally:
    clear_store()
    shutil.rmtree(tmp, ignore_errors=True)

for b in bad:
    print("WRONG:", b)
print(f"{len(ok)}/{len(ok) + len(bad)} send cases right")
raise SystemExit(1 if bad else 0)
