"""intent.py: the model's answer is parsed right, the prompt carries the rules and the session's state, and a
missing or broken model raises Unavailable so the hook changes nothing. Uses a fake `claude` command, so it runs
offline."""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intent  # noqa: E402

ok = 0
fail = 0


def expect(name, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("FAIL", name)


def fake(answer, code=0):
    d = Path(tempfile.mkdtemp())
    script = d / "fake_claude.py"
    script.write_text(f"import sys\nsys.stdin.read()\nprint({answer!r})\nsys.exit({code})\n")
    if os.name == "nt":
        exe = d / "claude.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        exe = d / "claude"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        exe.chmod(0o755)
    return str(exe)


reqs = [{"names": ["EXAMPLE_KEY"], "what": "testing"}]
for answer, want in [("YES", True), ("NO", False), ("UNCLEAR", None), ("Answer: YES.", True), ("**NO**", False)]:
    os.environ["KEYWARDEN_CLAUDE"] = fake(answer)
    expect(f"model says {answer!r}", intent.judge("anything", reqs) is want)

for answer, code, name in [("maybe?", 0, "unreadable answer"), ("YES", 1, "model exit 1")]:
    os.environ["KEYWARDEN_CLAUDE"] = fake(answer, code)
    try:
        intent.judge("anything", reqs)
        expect(name + " raises Unavailable", False)
    except intent.Unavailable:
        expect(name + " raises Unavailable", True)

os.environ["KEYWARDEN_CLAUDE"] = str(HERE / "no-such-claude.exe")
try:
    intent.judge("anything", reqs)
    expect("missing claude raises Unavailable", False)
except intent.Unavailable:
    expect("missing claude raises Unavailable", True)

os.environ["KEYWARDEN_CLAUDE"] = fake('{"answer": "YES", "minutes": 120, "keys": ["EXAMPLE_KEY"]}')
expect("a JSON answer gives the minutes and the named keys",
       intent.read("anything", reqs) == {"answer": True, "minutes": 120.0, "keys": ["EXAMPLE_KEY"]})
os.environ["KEYWARDEN_CLAUDE"] = fake('{"answer": "YES", "minutes": 0, "keys": ["EXAMPLE_KEY"]}')
expect("approve for 0 minutes approves nothing", intent.read("anything", reqs)["answer"] is None)

import prompt_hook  # noqa: E402
expect("the hook has no word lists left", not any(hasattr(prompt_hook, n) for n in (
    "answer_in", "approve_word_yes", "reply_answer", "STOP", "YES_START", "REFUSE", "NAMED_APPROVAL", "minutes_in")))

idle = intent._prompt("Stop agent that one too quickly.", [], held=True)
expect("the prompt says when nothing is waiting", "No key request is waiting" in idle)
expect("the prompt says when the session holds an approval", "already approved key use for this session" in idle)
expect("the prompt says when it holds none", "holds no key approval" in intent._prompt("hi", reqs))
expect("the prompt says a stop about other work is UNCLEAR", "stop the build, wrong branch" in idle
       and "explicitly withdraws permission" in idle)
expect("the prompt carries the key names", "EXAMPLE_KEY" in intent._prompt("hi", reqs))
print(f"{ok}/{ok + fail} intent cases right")
sys.exit(1 if fail else 0)
