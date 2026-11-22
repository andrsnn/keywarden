"""Run: python test_standing.py   One approve in the chat covers the session: a yes sent before any request, a yes
that answers a request, and how long it lasts. A no or stop ends it, other sessions stay uncovered, protected keys
keep their tap, and the model sees the agent's last message. Uses a test key store and a fake `claude` command."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.pop("AGENT_KEYS_" + "CALLER", None)  # run as a person at a terminal, not as the agent running the tests
HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="agent-keys-standing-"))
base = dict(os.environ, AGENT_KEYS_STATE=str(tmp), AGENT_KEYS_NS="agent-keys-standingtest",
            KEYWARDEN_CLAUDE=str(tmp / "no-model-here.exe"))
base.pop("AGENT_KEYS_BACKEND", None)
ok, bad = [], []

# A fake `claude` command: it answers with the word in answer.txt and keeps the prompt it was given.
FAKE = tmp / "fake"
FAKE.mkdir()
(FAKE / "fake_claude.py").write_text(
    "import sys, pathlib\nd = pathlib.Path(__file__).parent\n"
    "(d / 'prompt.txt').write_text(sys.stdin.read(), encoding='utf-8')\n"
    "print((d / 'answer.txt').read_text().strip())\n")
if os.name == "nt":
    FAKE_EXE = FAKE / "claude.cmd"
    FAKE_EXE.write_text(f'@"{sys.executable}" "{FAKE / "fake_claude.py"}" %*\n')
else:
    FAKE_EXE = FAKE / "claude"
    FAKE_EXE.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE / "fake_claude.py"}" "$@"\n')
    FAKE_EXE.chmod(0o755)


def model(answer):
    """None: no model answers (word-list fallback). Otherwise the fake model says this word."""
    if answer is None:
        base["KEYWARDEN_CLAUDE"] = str(tmp / "no-model-here.exe")
    else:
        (FAKE / "answer.txt").write_text(answer)
        (FAKE / "prompt.txt").unlink(missing_ok=True)
        base["KEYWARDEN_CLAUDE"] = str(FAKE_EXE)


def run(script, *args, stdin=None, token=None):
    env = dict(base)
    if token:
        env["AGENT_KEYS_" + "CALLER"] = token
    r = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin, capture_output=True, text=True,
                       env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr


TOKEN = re.compile(r"AGENT_KEYS_CALLER(?:=| = ')([\w-]+)")


def token(session, agent=None):
    data = {"session_id": session, "tool_name": "Bash", "tool_input": {"command": "keys status"}}
    if agent:
        data["agent_id"] = agent
    out = json.loads(run("hook.py", stdin=json.dumps(data))[1])["hookSpecificOutput"]["updatedInput"]["command"]
    return TOKEN.search(out).group(1)


def say(text, session, transcript=None):
    data = {"prompt": text, "session_id": session}
    if transcript:
        data["transcript_path"] = str(transcript)
    return run("prompt_hook.py", stdin=json.dumps(data))[1]


def grant(tok, *names):
    return run("keys.py", "grant", *names, "--for", "standing test", token=tok)


def holds(tok, name):
    return f"export {name}=" in run("keys.py", "env", token=tok)[1]


def standing():
    try:
        return json.loads((tmp / "standing.json").read_text())
    except Exception:
        return {}


def shift_standing(session, hours):
    """Make the session's approval look `hours` older."""
    table = standing()
    table[session]["until"] -= hours * 3600
    table[session]["at"] -= hours * 3600
    (tmp / "standing.json").write_text(json.dumps(table))


def use_log():
    try:
        return (tmp / "use.log").read_text(encoding="utf-8")
    except OSError:
        return ""


def reset():
    run("keys.py", "lock")
    (tmp / "pending.json").unlink(missing_ok=True)
    (tmp / "standing.json").unlink(missing_ok=True)
    model(None)


def transcript(text):
    """A Claude Code session log whose last agent message is `text`."""
    path = tmp / "session.jsonl"
    lines = [{"type": "user", "message": {"role": "user", "content": "deploy the build"}},
             {"type": "assistant", "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
             {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
             {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "a subagent's own message"}]}}]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


def expect(name, cond):
    (ok if cond else bad).append(name)


try:
    run("keys.py", "setup", "local")
    run("keys.py", "approvals", "on")
    run("keys.py", "add", "WEATHER_API_KEY", stdin="wx_" + "Qm7Lp" * 6)
    run("keys.py", "add", "MAPS_API_KEY", stdin="mp_" + "Zq81x" * 6)
    run("keys.py", "add", "VAULT_TOKEN", stdin="vt_" + "Hh3kP" * 6)
    s1, s1_sub, s2 = token("S1"), token("S1", "A1"), token("S2")

    # 1. approve sent before the request (no model: the word approve counts)
    out = say("approve", "S1")
    expect("approve with nothing waiting is taken as approval in advance", "approved key use in advance" in out)
    expect("the advance approval lasts 12 hours", "12 hours" in out)
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("the next keys grant goes straight through", code == 0 and holds(s1, "WEATHER_API_KEY"))
    expect("use.log says it was approved in advance in the chat",
           "approved in advance in the chat" in use_log() and "WEATHER_API_KEY" in use_log())
    code, out = grant(s1_sub, "MAPS_API_KEY")
    expect("the session's subagent is covered too, for any saved key", code == 0 and holds(s1_sub, "MAPS_API_KEY"))
    code, out = grant(s2, "WEATHER_API_KEY")
    expect("another session is not covered", code != 0 and "Reply approve" in out and not holds(s2, "WEATHER_API_KEY"))

    # 2. approve, then a request 6 hours later; and nothing after 12 hours
    reset()
    say("approve", "S1")
    shift_standing("S1", 6)
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("a request 6 hours after approve goes through", code == 0 and holds(s1, "WEATHER_API_KEY"))
    reset()
    say("approve", "S1")
    shift_standing("S1", 12.1)
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("a request over 12 hours after approve asks again", code != 0 and "Reply approve" in out)
    reset()
    say("approve 1h", "S1")
    shift_standing("S1", 1.1)
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("approve 1h lasts only an hour", code != 0 and "Reply approve" in out)

    # 3. a yes that answers a request covers the session's later requests, for 12 hours
    reset()
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("the ask tells the agent one approve covers the session", "covers every saved key" in out)
    out = say("approve, approve, go, go", "S1")
    expect("'approve, approve, go, go' approves (word lists)", "approved WEATHER_API_KEY for 12 hours" in out)
    code, out = grant(s1, "MAPS_API_KEY")
    expect("a different key later in the session goes straight through", code == 0 and holds(s1, "MAPS_API_KEY"))
    expect("use.log says the approve covered it", "covered by an approve in the chat" in use_log())
    g = json.loads((tmp / "grants.json").read_text())["callers"]["S1"]
    expect("the chat grant lasts about 12 hours", 11.9 * 3600 < g["WEATHER_API_KEY"] - time.time() <= 12 * 3600)
    reset()
    grant(s1, "WEATHER_API_KEY")
    model("YES")
    out = say("approve, approve, go, go", "S1")
    expect("'approve, approve, go, go' approves (model)", "approved WEATHER_API_KEY" in out)

    # 4. a yes inside a complaint, right after a request, even when the model isn't sure
    reset()
    grant(s1, "WEATHER_API_KEY")
    model("UNCLEAR")
    out = say("I keep saying approve, it's not working, please fix", "S1")
    expect("approve inside a complaint approves when the model says unclear", "approved WEATHER_API_KEY" in out)
    reset()
    grant(s1, "WEATHER_API_KEY")
    out = say("I keep saying approve, it's not working, please fix", "S1")
    expect("approve inside a complaint approves (word lists)", "approved WEATHER_API_KEY" in out)
    reset()
    grant(s1, "WEATHER_API_KEY")
    model("NO")
    out = say("don't approve that", "S1")
    expect("a real no to a request still says no", "said no to WEATHER_API_KEY" in out)

    # 5. no and stop end the standing approval
    reset()
    say("approve", "S1")
    grant(s1, "WEATHER_API_KEY")
    out = say("no", "S1")
    expect("no ends the session's approval", "approvals ended" in out and not holds(s1, "WEATHER_API_KEY")
           and "S1" not in standing())
    code, out = grant(s1, "MAPS_API_KEY")
    expect("after no, the next grant asks again", code != 0 and "Reply approve" in out)
    reset()
    say("approve", "S1")
    say("approve", "S2")
    grant(s1, "WEATHER_API_KEY")
    out = say("stop", "S1")
    expect("stop ends the session's approval", "approvals ended" in out and not holds(s1, "WEATHER_API_KEY"))
    expect("stop in one session leaves the other session's approval", "S2" in standing())
    reset()
    say("approve", "S1")
    grant(s1, "WEATHER_API_KEY")
    model("YES")
    out = say("stop using the laptop, and I approve using a key", "S1")
    expect("a message that starts with stop but approves keeps the approval", holds(s1, "WEATHER_API_KEY")
           and "S1" in standing())
    reset()
    grant(s1, "WEATHER_API_KEY")
    say("no", "S1")
    expect("no to a request leaves no standing approval", "S1" not in standing())
    reset()
    say("approve", "S1")
    run("keys.py", "lock", token=s1)
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("keys lock in the session ends the standing approval", code != 0 and "Reply approve" in out)

    # 6. the model sees the agent's last message
    reset()
    model("YES")
    log = transcript("Status: the deploy is ready. I need R2_SECRET and WEATHER_API_KEY. Reply approve.")
    out = say("go, go", "S1", log)
    prompt = (FAKE / "prompt.txt").read_text(encoding="utf-8") if (FAKE / "prompt.txt").exists() else ""
    expect("the model is given the agent's last message", "I need R2_SECRET and WEATHER_API_KEY" in prompt
           and "a subagent's own message" not in prompt)
    expect("'go, go' after the agent asked for keys approves in advance", "approved key use in advance" in out)
    reset()
    model("YES")
    out = say("yes", "S1")
    expect("a plain yes with no key ask in view does nothing", "additionalContext" not in out)
    reset()
    model("UNCLEAR")
    out = say("fix the approve flow in the settings page", "S1", transcript("I changed the settings page."))
    expect("a message that only mentions approve doesn't approve", "additionalContext" not in out and not standing())
    reset()
    model("UNCLEAR")
    out = say("I keep saying approve, it's not working, please fix", "S1",
              transcript("I need WEATHER_API_KEY to upload the build. Reply approve."))
    expect("a complaint with approve after the agent asked for keys approves in advance",
           "approved key use in advance" in out)
    reset()
    import prompt_hook
    codex_log = tmp / "codex.jsonl"
    codex_log.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "Codex needs MAPS_API_KEY. Reply approve."}]}}) + "\n")
    expect("reads the agent's last message from a Codex log", "MAPS_API_KEY" in prompt_hook.last_agent_message(str(codex_log)))
    expect("a missing log gives no context", prompt_hook.last_agent_message(str(tmp / "none.jsonl")) == "")

    # 7. protected keys keep their tap; approvals off is unchanged; totp ignores a chat yes
    reset()
    cfg = json.loads((tmp / "config.json").read_text())
    (tmp / "config.json").write_text(json.dumps(dict(cfg, protected=["VAULT_TOKEN"])))
    say("approve", "S1")
    code, out = grant(s1, "VAULT_TOKEN")
    expect("a protected key is not covered by the standing approval", code != 0 and not holds(s1, "VAULT_TOKEN"))
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("an ordinary key still is", code == 0)
    (tmp / "config.json").write_text(json.dumps(dict(cfg, approvals="auto")))
    reset()
    out = say("approve", "S1")
    expect("approvals off: approve makes no standing approval", "additionalContext" not in out and not standing())
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("approvals off: keys work without asking, as before", code == 0 and "allowed (approvals off)" in use_log())
    (tmp / "config.json").write_text(json.dumps(dict(cfg, approvals="totp")))
    reset()
    out = say("approve", "S1")
    expect("totp mode: a chat yes approves nothing in advance", not standing())
    (tmp / "config.json").write_text(json.dumps(cfg))
    expect("no key value is written to the log", "Qm7Lp" not in use_log() and "Zq81x" not in use_log())
finally:
    sys.path.insert(0, str(HERE))
    os.environ.update(AGENT_KEYS_NS="agent-keys-standingtest", AGENT_KEYS_STATE=str(tmp))
    try:
        import local_store
        for n in local_store.names():
            local_store.delete(n)
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

for name in bad:
    print("WRONG:", name)
print(f"{len(ok)}/{len(ok) + len(bad)} standing approval cases right")
sys.exit(1 if bad else 0)
