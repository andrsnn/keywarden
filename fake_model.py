"""Tests only: a stand-in for the `claude` command that keywarden asks what a chat message means.

It answers from a table of message -> what the model would say, so the tests run offline and each one states what
the model decided. The rules the real model follows are in intent._prompt. A message not in the table is UNCLEAR.
An answer can depend on whether a key request is waiting: {"waiting": ..., "idle": ...}.

    os.environ["KEYWARDEN_CLAUDE"] = fake_model.install(folder)   # in a test
    fake_model.force(folder, "NO")                                # every message gets this answer until force(folder, None)
"""
import json
import os
import sys
from pathlib import Path


def yes(minutes=None, *keys):
    return {"answer": "YES", "minutes": minutes, "keys": list(keys)}


ANSWERS = {
    "approve": "YES", "yes": "YES", "Approved": "YES", "go ahead": "YES", "yes go ahead": "YES",
    "approve 1h": yes(60), "approve 30m": yes(30), "approve 2h": yes(120), "approve 15m": yes(15),
    "approve 1h please": yes(60), "It's on disk approve": "YES", "2h": yes(120),
    "Okay, test that it actually fucking works. Like, run it, show me evidence that it runs, and show me the actual "
    "output. I want to get a notification, ideally.": "YES",
    "Didn't we just solve this? Like I could say, if my intent is clear, I don't have to say approve verbatim, "
    "subagent keyword and get this fixed in main, make sure it's deployed.": "YES",
    "no": {"waiting": "NO", "idle": "UNCLEAR"}, "stop": {"waiting": "NO", "idle": "UNCLEAR"},
    "don't approve": "NO", "hold off": "NO", "not that key": "NO", "wait": "UNCLEAR",
    "no, don't use the keys": "NO", "revoke the keys": "NO",
    # Keys approved by name.
    "approve DEMO_API_KEY": yes(None, "DEMO_API_KEY"),
    "approve DEMO_API_KEY for 8h": yes(480, "DEMO_API_KEY"),
    "approve DEMO_API_KEY for 1h": yes(60, "DEMO_API_KEY"),
    "approve DEMO_API_KEY for 15m": yes(15, "DEMO_API_KEY"),
    "approve DEMO_API_KEY and OTHER_TOKEN for 2h": yes(120, "DEMO_API_KEY", "OTHER_TOKEN"),
    "You'll need these keys: DEMO_API_KEY for 8h\nBuild overnight.": yes(480, "DEMO_API_KEY"),
    "approve DEMO_API_KEY for 0h": yes(0, "DEMO_API_KEY"),
    "Use DEMO_API_KEY and UNKNOWN_KEY for 8h": yes(480, "DEMO_API_KEY", "UNKNOWN_KEY"),
    "Do not use DEMO_API_KEY.": "NO",
    "approve WEATHER_KEY for 1h": yes(60, "WEATHER_KEY"),
    "approve TOUCH_TEST_KEY for 1h": yes(60, "TOUCH_TEST_KEY"),
}


def install(folder, answers=None):
    """Make a fake `claude` command in folder and return its path. answers adds to or replaces table entries."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "answers.json").write_text(json.dumps(dict(ANSWERS, **(answers or {}))), encoding="utf-8")
    me = Path(__file__).resolve()
    if os.name == "nt":
        exe = folder / "claude.cmd"
        exe.write_text(f'@"{sys.executable}" "{me}" "{folder}" %*\n')
    else:
        exe = folder / "claude"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{me}" "{folder}" "$@"\n')
        exe.chmod(0o755)
    return str(exe)


def install_temp(answers=None):
    """install() into a new temp folder that is deleted when the test exits. Returns (command, folder)."""
    import atexit
    import shutil
    import tempfile
    folder = tempfile.mkdtemp(prefix="keywarden-fake-model-")
    atexit.register(shutil.rmtree, folder, True)
    return install(folder, answers), folder


def force(folder, answer):
    """Give every message this answer (a word, or a dict), or go back to the table with None."""
    path = Path(folder) / "forced.json"
    if answer is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(answer), encoding="utf-8")


def last_prompt(folder):
    path = Path(folder) / "prompt.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def main():
    folder = Path(sys.argv[1])
    prompt = sys.stdin.buffer.read().decode("utf-8").replace("\r\n", "\n")
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    message = prompt.split("<<<MESSAGE\n", 1)[-1].rsplit("\nMESSAGE>>>", 1)[0].strip()
    forced = folder / "forced.json"
    if forced.exists():
        got = json.loads(forced.read_text(encoding="utf-8"))
    else:
        got = json.loads((folder / "answers.json").read_text(encoding="utf-8")).get(message, "UNCLEAR")
    if isinstance(got, dict) and "waiting" in got:
        got = got["idle"] if "No key request is waiting" in prompt else got["waiting"]
    print(got if isinstance(got, str) else json.dumps(got))


if __name__ == "__main__":
    main()
