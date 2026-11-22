"""intent.py: the model's one-word answer is parsed right, and a missing or broken model raises Unavailable so the
hook falls back to the word lists. Uses a fake `claude` command, so it runs offline."""
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

# The hook falls back to the word lists when the model can't answer.
import prompt_hook  # noqa: E402
answer, by_model = prompt_hook.reply_answer("yes", reqs)
expect("fallback answers yes", answer is True and by_model is False)
os.environ["KEYWARDEN_CLAUDE"] = fake("NO")
answer, by_model = prompt_hook.reply_answer("yes", reqs)
expect("model wins over the word lists", answer is False and by_model is True)

expect("the prompt carries the key names", "EXAMPLE_KEY" in intent._prompt("hi", reqs))
print(f"{ok}/{ok + fail} intent cases right")
sys.exit(1 if fail else 0)
