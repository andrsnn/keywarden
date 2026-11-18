"""Run: python test_envfind.py   Checks loose approval replies, finding a missing key in a .env file on disk,
keys import --only, and the hint shown when an agent tries to read a .env file. Fake keys are made at run time."""
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.pop("AGENT_KEYS_CALLER", None)  # run as a person at a terminal, not as the agent running the tests

HERE = Path(__file__).resolve().parent
NS = "keywarden-envfind-test"
tmp = Path(tempfile.mkdtemp(prefix="keywarden-envfind-"))
state, disk = tmp / "state", tmp / "disk"
state.mkdir()
env = dict(os.environ, AGENT_KEYS_STATE=str(state), AGENT_KEYS_NS=NS, AGENT_KEYS_ENV_DIRS=str(disk),
           AGENT_KEYS_NO_BIOMETRICS="1")
env.pop("AGENT_KEYS_BACKEND", None)
ok, bad = [], []
rnd = random.Random()
fake = lambda: "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(40))
VALUE_A, VALUE_B, VALUE_OLD, VALUE_NEW = fake(), fake(), fake(), fake()
ALL_VALUES = (VALUE_A, VALUE_B, VALUE_OLD, VALUE_NEW)


def run(script, *args, stdin=None):
    r = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin, capture_output=True, text=True,
                       env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr


def keys(*args):
    return run("keys.py", *args)


def say(text):
    return run("prompt_hook.py", stdin=json.dumps({"prompt": text}))[1]


def blocked(tool, inp):
    return run("hook.py", stdin=json.dumps({"tool_name": tool, "tool_input": inp}))[1]


def expect(name, cond):
    (ok if cond else bad).append(name)


def no_values(out):
    return not any(v in out for v in ALL_VALUES)


def meta(name):
    return json.loads((state / "meta.json").read_text()).get(name, {})


try:
    # F1: the word approve anywhere in a short reply answers the waiting request.
    sys.path.insert(0, str(HERE))
    from prompt_hook import answer_in, minutes_in
    for text, want in [("It's on disk approve", True), ("approve 1h please", True), ("yes approve", True),
                       ("Approved", True), ("ok it's there now, approve 8h", True), ("approve", True), ("yes", True),
                       ("don't approve", False), ("do not approve that", False), ("no", False), ("deny", False),
                       ("I don't approve of this", False), ("approve it, no wait", None),
                       ("what are you doing?", None), ("fix the bug in a.ts", None)]:
        expect(f"answer to {text!r} is {want}", answer_in(text) is want)
    expect("approve 1h please is 60 minutes", minutes_in("approve 1h please") == 60)

    keys("setup", "local")
    keys("approvals", "on")
    proc = subprocess.run([sys.executable, str(HERE / "keys.py"), "add", "SAVED_TEST_KEY"], input=fake(),
                          capture_output=True, text=True, env=env, timeout=60)
    expect("a key to approve is saved", proc.returncode == 0)

    code, out = keys("grant", "SAVED_TEST_KEY", "--for", "the test")
    expect("grant asks the person", code != 0 and "Reply approve" in out)
    out = say("It's on disk approve")
    expect("'It's on disk approve' approves", "approved SAVED_TEST_KEY" in out)
    keys("lock")
    keys("grant", "SAVED_TEST_KEY", "--for", "the test")
    out = say("approve 1h please")
    expect("'approve 1h please' approves for 1 hour", "approved SAVED_TEST_KEY for 1 hour" in out)
    keys("lock")
    keys("grant", "SAVED_TEST_KEY", "--for", "the test")
    out = say("don't approve")
    expect("'don't approve' turns it down", "said no to SAVED_TEST_KEY" in out and "approved" not in out)
    (state / "pending.json").unlink(missing_ok=True)

    # F2: a name that isn't saved is found in a .env file on disk and saved without printing it.
    proj = disk / "some-app"
    proj.mkdir(parents=True)
    (proj / ".env.local").write_text(f"# app settings\nDISK_ONLY_TOKEN={VALUE_A}\nOTHER_TOKEN={VALUE_B}\n")
    (proj / ".env.example").write_text("EXAMPLE_ONLY_TOKEN=put-your-token-here\n")
    (proj / ".env.local.bak-old").write_text("EXAMPLE_ONLY_TOKEN=" + fake() + "\n")
    code, out = keys("grant", "DISK_ONLY_TOKEN", "--for", "the test")
    expect("grant finds the key in a .env file", "found DISK_ONLY_TOKEN in" in out and ".env.local" in out
           and "saved it" in out)
    expect("then asks for approval as usual", code != 0 and "Reply approve" in out)
    expect("the found value is never printed", no_values(out))
    listing = keys("list")[1]
    expect("the found key is saved", "DISK_ONLY_TOKEN" in listing)
    expect("only the asked-for key is saved", "OTHER_TOKEN" not in listing)
    expect("the key's details say which file it came from", ".env.local" in meta("DISK_ONLY_TOKEN").get("from", ""))
    (state / "pending.json").unlink(missing_ok=True)
    code, out = keys("grant", "EXAMPLE_ONLY_TOKEN", "--for", "the test")
    expect("example and backup files are not used", code != 0 and "isn't in any .env file" in out
           and "EXAMPLE_ONLY_TOKEN" not in keys("list")[1])
    code, out = keys("grant", "NOWHERE_TOKEN", "--for", "the test")
    expect("a key that isn't anywhere says where it looked", code != 0 and "isn't in any .env file" in out
           and "keys import FILE --only NOWHERE_TOKEN" in out)

    # keys import --only saves just the named keys and never replaces a different value without --replace.
    other = disk / "other-app" / ".env"
    other.parent.mkdir()
    other.write_text(f"FIRST_ONLY={VALUE_OLD}\nNOT_THIS_ONE={VALUE_B}\n")
    code, out = keys("import", str(other), "--only", "FIRST_ONLY")
    listing = keys("list")[1]
    expect("--only imports the named key", code == 0 and "FIRST_ONLY: saved" in out and "FIRST_ONLY" in listing)
    expect("--only skips the other keys", "NOT_THIS_ONE" not in listing and no_values(out))
    newer = disk / "newer-app" / ".env.local"
    newer.parent.mkdir()
    newer.write_text(f"FIRST_ONLY={VALUE_NEW}\n")
    code, out = keys("import", str(newer), "--only", "FIRST_ONLY")
    expect("--only doesn't replace a different value", "a different value is already saved" in out
           and str(other.resolve()) in meta("FIRST_ONLY").get("from", "") and no_values(out))
    code, out = keys("import", str(newer), "--only", "MISSING_HERE")
    expect("--only says when a name isn't in the file", "MISSING_HERE: not in" in out)

    # F3: a blocked .env read says how to import the key instead.
    out = blocked("Bash", {"command": "grep SENTRY_AUTH_TOKEN /proj/app/.env.local"})
    expect("a blocked grep names the import command",
           'keys import "/proj/app/.env.local" --only SENTRY_AUTH_TOKEN' in out and "keys grant SENTRY_AUTH_TOKEN" in out)
    out = blocked("Read", {"file_path": "C:/Users/someone/app/.env"})
    expect("a blocked Read names the import command", 'keys import "C:/Users/someone/app/.env" --only NAME' in out)
    out = blocked("PowerShell", {"command": "Get-Content 'C:/Users/some one/app/.env.local' | Select-String DISK_ONLY_TOKEN"})
    expect("a saved key gets keys grant instead", "DISK_ONLY_TOKEN is already saved" in out
           and "keys grant DISK_ONLY_TOKEN" in out and "keys import" not in out)
    out = blocked("Bash", {"command": "cat ~/.npmrc"})
    expect("other blocked files keep the usual hint", "keys run KEY_NAME" in out and "keys import" not in out)
finally:
    sys.path.insert(0, str(HERE))
    os.environ.update(AGENT_KEYS_NS=NS, AGENT_KEYS_STATE=str(state))
    import local_store
    for n in local_store.names(NS):
        local_store.delete(n, NS)
    shutil.rmtree(tmp, ignore_errors=True)
for n in bad:
    print("WRONG:", n)
print(f"{len(ok)}/{len(ok) + len(bad)} env-find cases right")
raise SystemExit(1 if bad else 0)
