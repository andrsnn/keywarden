"""Run: python test_touchid.py   Checks approving keys with Touch ID (or a plugged-in approver) on this computer.

A fake approver program stands in for Touch ID, so the test runs on any computer. On a Mac with the compiled
helper, it also checks the real touchid binary answers --check. Real keys are never touched."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.pop("AGENT_KEYS_CALLER", None)  # run as a person at a terminal, not as the agent running the tests

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="agent-keys-touchid-"))
env = dict(os.environ, AGENT_KEYS_STATE=str(tmp), AGENT_KEYS_NS="agent-keys-touchidtest")
env.pop("AGENT_KEYS_BACKEND", None)
ANSWER, CALLS = tmp / "answer.txt", tmp / "calls.txt"
FAKE = tmp / "fake_touchid.py"
FAKE.write_text(f'''import os, sys, time
from pathlib import Path
if sys.argv[1:] == ["--check"]:
    print("available"); sys.exit(0)
with open({str(CALLS)!r}, "a", encoding="utf-8") as f:
    f.write(sys.argv[-1] + " | timeout " + os.environ.get("AGENT_KEYS_TIMEOUT", "") + "\\n")
ans = Path({str(ANSWER)!r}).read_text().strip()
if ans == "hang":
    time.sleep(60)
print(ans)
''', encoding="utf-8")
ok, bad = [], []


def run(script, *args, stdin=None, timeout=60):
    r = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin, capture_output=True, text=True,
                       env=env, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def keys(*args, **kw):
    return run("keys.py", *args, **kw)


def say(text):  # the person sends a message
    return run("prompt_hook.py", stdin=json.dumps({"prompt": text}))[1]


def handed_over():
    return "export TOUCH_TEST_KEY=" in keys("env")[1]


def calls():
    return CALLS.read_text(encoding="utf-8").splitlines() if CALLS.exists() else []


def answer(text):
    ANSWER.write_text(text)
    CALLS.unlink(missing_ok=True)


def config():
    return json.loads((tmp / "config.json").read_text(encoding="utf-8"))


def expect(name, cond):
    (ok if cond else bad).append(name)


def unit_tests():
    os.environ["AGENT_KEYS_STATE"] = str(tmp)
    sys.path.insert(0, str(HERE))
    import local_auth
    expect("Verified approves", local_auth.result("Verified\n", 0) == "yes")
    expect("Verified with an error code doesn't approve", local_auth.result("Verified", 1) == "unavailable")
    expect("Canceled is a no", local_auth.result("Canceled", 0) == "no")
    expect("Timeout is a timeout", local_auth.result("Timeout", 0) == "timeout")
    expect("unavailable stays unavailable", local_auth.result("unavailable no biometrics", 0) == "unavailable")
    expect("no output is unavailable", local_auth.result("", 0) == "unavailable")
    expect("the last line decides", local_auth.result("Verified\nCanceled", 0) == "no")
    real_platform = sys.platform
    try:
        sys.platform = "linux"
        expect("Touch ID says it needs a Mac elsewhere", local_auth.TouchID().argv() is None
               and "Mac" in local_auth.TouchID().check())
        sys.platform = "darwin"
        (tmp / "touchid").write_text("")
        expect("on a Mac, the compiled helper is used", local_auth.TouchID().argv() == [str(tmp / "touchid")])
        (tmp / "touchid").unlink()
    finally:
        sys.platform = real_platform

    class Always:
        label = "Test approver"

        def check(self):
            return "available"

        def ask(self, reason, timeout=60, procs=None):
            return "yes"

    local_auth.register("always", Always())
    (tmp / "config.json").write_text(json.dumps({"local_approval": "always"}))
    expect("a registered approver can be chosen", local_auth.enabled().label == "Test approver")
    (tmp / "config.json").write_text(json.dumps({"local_approval": "nonsense"}))
    expect("an unknown approver name means off", local_auth.enabled() is None)
    (tmp / "config.json").unlink()
    expect("off by default", local_auth.enabled() is None and local_auth.chosen() is None)


def mac_helper_test():
    """On a Mac with the helper built (install.py, or CI), the real binary answers --check."""
    b = tmp / "touchid"
    if sys.platform != "darwin" or not os.environ.get("KEYWARDEN_TOUCHID_BIN"):
        return
    b.write_bytes(Path(os.environ["KEYWARDEN_TOUCHID_BIN"]).read_bytes())
    b.chmod(0o755)
    _, out = keys("touchid")
    expect("the real Touch ID helper answers --check", "Touch ID: " in out)
    print("Touch ID on this Mac: " + out.strip().splitlines()[-1])
    b.unlink()


try:
    unit_tests()
    mac_helper_test()
    keys("setup", "local")
    keys("approvals", "on")
    keys("add", "TOUCH_TEST_KEY", stdin="tk-" + "4f9a2c" * 4 + "\n")
    answer("Verified")

    code, out = keys("grant", "TOUCH_TEST_KEY", "--for", "testing")
    expect("off by default: a request is asked in the chat", code != 0 and "Reply approve" in out and not calls())
    expect("status says it is off", "is off" in keys("touchid")[1])

    code, out = keys("touchid", "use", "command", "--", sys.executable, str(FAKE))
    expect("plugging in a program needs an OK in the chat", code != 0 and "Reply approve" in out
           and "local_approval" not in config())
    say("approve")
    code, out = keys("touchid", "use", "command", "--", sys.executable, str(FAKE))
    expect("after approve, the program is plugged in", code == 0 and config().get("local_approval") == "command")
    expect("status says it is on", "approvals are on" in keys("touchid")[1])

    keys("lock")
    answer("Verified")
    code, out = keys("grant", "TOUCH_TEST_KEY", "--for", "deploying the site")
    expect("a tap approves the key", code == 0 and "approved with your approval program" in out)
    expect("the approver was asked once, with the key name and reason", len(calls()) == 1
           and "TOUCH_TEST_KEY for: deploying the site" in calls()[0])
    expect("the approver never sees the key value", "4f9a2c" not in "".join(calls()))
    expect("the approver is told the timeout", "timeout 60" in calls()[0])
    expect("commands get the key", handed_over())
    expect("the use log names the approver", "approved with your approval program"
           in (tmp / "use.log").read_text(encoding="utf-8"))

    keys("lock")
    answer("Canceled")
    code, out = keys("grant", "TOUCH_TEST_KEY", "--for", "testing")
    expect("cancel denies", code != 0 and "denied with your approval program" in out)
    expect("a denial doesn't leave a chat request waiting", "TOUCH_TEST_KEY" not in
           ((tmp / "pending.json").read_text() if (tmp / "pending.json").exists() else ""))
    expect("no key after a denial", not handed_over())
    out = say("approve TOUCH_TEST_KEY for 1h")
    expect("after a denial, the person can still approve by name in the chat", "TOUCH_TEST_KEY" in out
           and "additionalContext" in out)

    keys("lock")
    answer("unavailable no fingerprint enrolled")
    code, out = keys("grant", "TOUCH_TEST_KEY", "--for", "testing")
    expect("an approver that can't ask falls back to the chat", code != 0 and "Reply approve" in out
           and "isn't available" in out)
    say("approve")
    expect("and the chat approval works", handed_over())

    keys("lock")
    keys("touchid", "timeout", "5")
    answer("hang")
    start = time.time()
    code, out = keys("grant", "TOUCH_TEST_KEY", "--for", "testing")
    took = time.time() - start
    expect("nobody tapping falls back to the chat", code != 0 and "nobody answered" in out and "Reply approve" in out)
    expect("the wait is capped by the timeout", took < 20)
    keys("touchid", "timeout", "60")
    say("no")

    answer("Verified")
    code, out = keys("approvals", "off")
    expect("changing approval settings still asks in the chat, not the approver", code != 0
           and "Reply approve" in out and not calls())
    say("no")

    keys("add", "TOUCH_PROTECTED", stdin="tp-" + "9b1e7d" * 4 + "\n")
    keys("protect", "TOUCH_PROTECTED")
    answer("Verified")
    code, out = keys("grant", "TOUCH_PROTECTED", "--for", "testing")
    expect("a protected key is tapped on the plugged-in approver", code == 0 and "approved on the computer" in out)

    keys("lock")
    code, out = keys("touchid", "off")
    expect("touchid off turns it off", code == 0 and "local_approval" not in config())
    answer("Verified")
    code, out = keys("grant", "TOUCH_TEST_KEY", "--for", "testing")
    expect("after off, requests go to the chat again", code != 0 and "Reply approve" in out and not calls())
    say("no")
    code, out = keys("touchid", "use", "nope")
    expect("an unknown approver name is refused", code != 0 and "no approver named nope" in out)
finally:
    os.environ.update(AGENT_KEYS_NS="agent-keys-touchidtest", AGENT_KEYS_STATE=str(tmp))
    import local_store
    for n in local_store.names("agent-keys-touchidtest"):
        local_store.delete(n, "agent-keys-touchidtest")
    shutil.rmtree(tmp, ignore_errors=True)

for n in ok:
    print("PASS", n)
for n in bad:
    print("FAIL", n)
print(f"{len(ok)}/{len(ok) + len(bad)} passed")
sys.exit(1 if bad else 0)
