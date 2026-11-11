"""Run: python test_chat.py   Checks the whole loop with a test key store: save a key by saying "save this",
Claude asks for it, the person replies approve, and commands get it. Real keys are never touched."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="agent-keys-chat-"))
env = dict(os.environ, AGENT_KEYS_STATE=str(tmp), AGENT_KEYS_NS="agent-keys-chattest")
env.pop("AGENT_KEYS_BACKEND", None)
ok, bad = [], []


def run(script, *args, stdin=None):
    r = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin, capture_output=True, text=True,
                       env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr


def say(text):  # the person sends a message
    return run("prompt_hook.py", stdin=json.dumps({"prompt": text}))[1]


def handed_over():
    return "export CLOUDFRONT_KEY=K2JCJMDEHXQW5F" in run("keys.py", "env")[1]


def expect(name, cond):
    (ok if cond else bad).append(name)


try:
    run("keys.py", "setup", "local")
    out = say("save this cloudfront key K2JCJMDEHXQW5F")
    expect("save this saves and stops the message", '"block"' in out and "Saved CLOUDFRONT_KEY" in out
           and "K2JC" not in out)
    expect("the key is in the store", "CLOUDFRONT_KEY" in run("keys.py", "list")[1])
    out = run("session_hook.py")[1]
    expect("new sessions are told the key names", "CLOUDFRONT_KEY" in out and "K2JC" not in out)

    code, out = run("keys.py", "grant", "CLOUDFRONT_KEY", "--for", "testing CloudFront")
    expect("a key with no grant asks the person", code != 0 and "Reply approve" in out)
    expect("no grant before the reply", not handed_over())
    expect("an unrelated message doesn't approve", "additionalContext" not in say("what are you doing?"))
    out = say("approve 1h")
    expect("approve creates the grant", "approved CLOUDFRONT_KEY for 1 hour" in out)
    code, out = run("keys.py", "grant", "CLOUDFRONT_KEY", "--for", "testing CloudFront")
    expect("with a grant, Claude gets the key", code == 0)
    expect("commands get the key", handed_over())

    run("keys.py", "lock")
    run("keys.py", "grant", "CLOUDFRONT_KEY", "--for", "again")
    out = say("no")
    expect("no drops the request", "said no to CLOUDFRONT_KEY" in out)
    expect("approve after no does nothing", "additionalContext" not in say("yes"))
    expect("still no grant", not handed_over())
finally:
    sys.path.insert(0, str(HERE))
    os.environ.update(AGENT_KEYS_NS="agent-keys-chattest", AGENT_KEYS_STATE=str(tmp))
    import local_store
    for n in local_store.names("agent-keys-chattest"):
        local_store.delete(n, "agent-keys-chattest")
    shutil.rmtree(tmp, ignore_errors=True)
for n in bad:
    print("WRONG:", n)
print(f"{len(ok)}/{len(ok) + len(bad)} chat cases right")
raise SystemExit(1 if bad else 0)
