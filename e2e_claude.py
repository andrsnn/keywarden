"""Run: python e2e_claude.py   End-to-end check of per-session approvals with real Claude Code sessions (it uses
your Claude account, so it isn't part of test_all.py). Needs keywarden installed. Uses a throwaway key store and a
fake key made at run time. Prints only pass/fail lines and the model's short answers."""
import json
import os
import random
import string
import subprocess
import sys
import time
from pathlib import Path

import tempfile
HERE = Path(__file__).resolve().parent
ROOT = Path(tempfile.mkdtemp(prefix="kw-e2e-"))
(ROOT / "work").mkdir()
STATE = ROOT / "state"
env = dict(os.environ, AGENT_KEYS_STATE=str(STATE), AGENT_KEYS_NS="kw-e2e")
env.pop("AGENT_KEYS_" + "CALLER", None)
env.pop("AGENT_KEYS_BACKEND", None)
rnd = random.SystemRandom()
VALUE = "wx_" + "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(30))
results = []

# Start from an empty throwaway store (namespace kw-e2e, never the real one).
import shutil
os.environ.update(AGENT_KEYS_STATE=str(STATE), AGENT_KEYS_NS="kw-e2e")
sys.path.insert(0, str(HERE))
import local_store


def clear_store():
    for n in local_store.names():
        local_store.delete(n)
    shutil.rmtree(STATE, ignore_errors=True)


clear_store()
subprocess.run([sys.executable, str(HERE / "keys.py"), "setup", "local"], env=env, capture_output=True)


def check(name, ok):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name, flush=True)


def claude(prompt, resume=None):
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "bypassPermissions",
           "--model", "sonnet"]
    if resume:
        cmd += ["--resume", resume]
    r = subprocess.run(cmd, cwd=ROOT / "work", env=env, capture_output=True, text=True, encoding="utf-8",
                       timeout=600, shell=os.name == "nt")
    try:
        out = json.loads(r.stdout)
    except Exception:
        print("raw stdout:", r.stdout[-800:], "stderr:", r.stderr[-800:])
        raise
    return out


def grants():
    try:
        return json.loads((STATE / "grants.json").read_text())["callers"]
    except Exception:
        return {}


# Session A: the person gives a key with a task.
a = claude(
    "Here is my weather api key, save this: " + VALUE + " and use it to do this task: run this exact Bash command "
    "and tell me its output: python -c \"import os; print(len(os.environ.get('WEATHER_API_KEY', '')))\" "
    "Then use the Agent tool to start a general-purpose subagent with this exact instruction: 'Run this exact Bash "
    "command and report its full output word for word: keys grant WEATHER_API_KEY --for \"subagent check\"'. "
    "Do not run keys grant yourself. Finish with two lines: LENGTH=<the number> and SUBAGENT=<what the subagent "
    "reported, on one line>.")
sid_a, text_a = a.get("session_id"), a.get("result", "")
print("session A answer:", text_a[-400:].replace(VALUE, "<VALUE>"))
g = grants()
check("the key was saved", "WEATHER_API_KEY" in subprocess.run(
    [sys.executable, str(HERE / "keys.py"), "list"], env=env, capture_output=True, text=True).stdout)
check("only session A holds a grant", list(g) == [sid_a] and "WEATHER_API_KEY" in g.get(sid_a, {}))
check("session A's command got the key (LENGTH=33)", "LENGTH=33" in text_a.replace(" ", ""))
check("the subagent had to ask", "needs your OK" in text_a or "Reply approve" in text_a)
check("the value never reached the model's answer", VALUE not in json.dumps(a))

# Session B: a new session asks for the same key.
b = claude("Run this exact Bash command and report its full output word for word: "
           "keys grant WEATHER_API_KEY --for \"session B check\"")
sid_b, text_b = b.get("session_id"), b.get("result", "")
print("session B answer:", text_b[-300:])
check("session B had to ask", "needs your OK" in text_b or "Reply approve" in text_b)
check("session B has no grant yet", sid_b not in grants())

# The person replies approve in session B, then B uses the key.
claude("approve", resume=sid_b)
b2 = claude("Now run this exact Bash command and tell me its output: "
            "python -c \"import os; print(len(os.environ.get('WEATHER_API_KEY', '')))\" "
            "Finish with LENGTH=<the number>.", resume=sid_b)
text_b2 = b2.get("result", "")
print("session B after approve:", text_b2[-300:])
g = grants()
check("approve gave session B the key", "WEATHER_API_KEY" in g.get(sid_b, {}))
check("session B's command got the key (LENGTH=33)", "LENGTH=33" in text_b2.replace(" ", ""))
check("the subagent still has no grant", all(c in (sid_a, sid_b) for c in g))

clear_store()
print(f"\n{sum(ok for _, ok in results)}/{len(results)} end-to-end checks passed")
sys.exit(0 if all(ok for _, ok in results) else 1)
