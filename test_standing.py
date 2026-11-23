"""Run: python test_standing.py   One approve in the chat covers the session: a yes sent before any request, a yes
that answers a request, and how long it lasts. A no or stop ends it, other sessions stay uncovered, protected keys
keep their tap, and the model sees the agent's last message. A "stop" about other work leaves the approval alone,
and when no model answers nothing changes. Uses a test key store and a fake `claude` command (fake_model.py)."""
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

# A fake `claude` command (fake_model.py): it answers from its table, or with the answer forced by model().
import fake_model  # noqa: E402
FAKE = tmp / "fake"
FAKE_EXE = fake_model.install(FAKE, {"approve, approve, go, go": "YES", "go, go": "YES"})
base["KEYWARDEN_CLAUDE"] = FAKE_EXE


def model(answer):
    """None: the fake model answers from its table. Otherwise it gives every message this answer."""
    fake_model.force(FAKE, answer)
    (FAKE / "prompt.txt").unlink(missing_ok=True)
    base["KEYWARDEN_CLAUDE"] = FAKE_EXE


def no_model():
    """No model can be reached."""
    base["KEYWARDEN_CLAUDE"] = str(tmp / "no-model-here.exe")


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


def shift_grants(session, hours):
    """Make the session's grants run out `hours` sooner."""
    a = json.loads((tmp / "grants.json").read_text())
    for n in a["callers"][session]:
        a["callers"][session][n] -= hours * 3600
    (tmp / "grants.json").write_text(json.dumps(a))


def left(session, name):
    """Seconds left on the session's grant of `name`, or 0."""
    try:
        return json.loads((tmp / "grants.json").read_text())["callers"][session].get(name, time.time()) - time.time()
    except Exception:
        return 0


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

    # 1. approve sent before the request
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
    expect("'approve, approve, go, go' approves", "approved WEATHER_API_KEY for 12 hours" in out)
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

    # 4. a yes inside a complaint, right after a request: the model's reading decides
    reset()
    grant(s1, "WEATHER_API_KEY")
    model("YES")
    out = say("I keep saying approve, it's not working, please fix", "S1")
    expect("approve inside a complaint approves when the model reads it as yes", "approved WEATHER_API_KEY" in out)
    reset()
    grant(s1, "WEATHER_API_KEY")
    model("UNCLEAR")
    out = say("fix the approve flow", "S1")
    expect("the word approve alone doesn't approve when the model says unclear", "approved" not in out
           and not holds(s1, "WEATHER_API_KEY"))
    reset()
    grant(s1, "WEATHER_API_KEY")
    model("NO")
    out = say("don't approve that", "S1")
    expect("a real no to a request still says no", "said no to WEATHER_API_KEY" in out)

    # 5. only a clear withdrawal of key permission ends the standing approval; a stop about other work doesn't
    for text in ("Stop agent that one too quickly.", "stop the build, wrong branch"):
        reset()
        say("approve", "S1")
        grant(s1, "WEATHER_API_KEY")
        model("UNCLEAR")
        out = say(text, "S1")
        expect(f"{text!r} keeps the standing approval", "approvals ended" not in out
               and holds(s1, "WEATHER_API_KEY") and "S1" in standing())
        prompt = fake_model.last_prompt(FAKE)
        expect(f"{text!r}: the model is told nothing is waiting and an approval is in place",
               "No key request is waiting" in prompt and "already approved key use for this session" in prompt)
        code, out = grant(s1, "MAPS_API_KEY")
        expect(f"after {text!r}, the next grant still goes straight through", code == 0)
    reset()
    say("approve", "S1")
    grant(s1, "WEATHER_API_KEY")
    model("NO")
    out = say("no, don't use the keys", "S1")
    expect("'no, don't use the keys' ends the session's approval", "approvals ended" in out
           and not holds(s1, "WEATHER_API_KEY") and "S1" not in standing())
    code, out = grant(s1, "MAPS_API_KEY")
    expect("after it, the next grant asks again", code != 0 and "Reply approve" in out)
    reset()
    say("approve", "S1")
    say("approve", "S2")
    grant(s1, "WEATHER_API_KEY")
    model("NO")
    out = say("revoke the keys", "S1")
    expect("'revoke the keys' ends the session's approval", "approvals ended" in out
           and not holds(s1, "WEATHER_API_KEY") and "S1" not in standing())
    expect("a revoke in one session leaves the other session's approval", "S2" in standing())
    reset()
    say("approve", "S1")
    grant(s1, "WEATHER_API_KEY")
    model("YES")
    out = say("stop using the laptop, and I approve using a key", "S1")
    expect("a message that starts with stop but approves keeps the approval", holds(s1, "WEATHER_API_KEY")
           and "S1" in standing())
    reset()
    grant(s1, "WEATHER_API_KEY")
    model("NO")
    out = say("no", "S1")
    expect("a bare no with a request waiting denies that request", "said no to WEATHER_API_KEY" in out
           and not holds(s1, "WEATHER_API_KEY") and "S1" not in standing())
    expect("the model is told the request is waiting", "WEATHER_API_KEY: standing test" in fake_model.last_prompt(FAKE))
    reset()
    say("approve", "S1")
    grant(s1, "WEATHER_API_KEY")
    code, out = run("keys.py", "lock", token=s1)
    expect("keys lock in the session clears its grants", not holds(s1, "WEATHER_API_KEY"))
    expect("keys lock in the session keeps the person's standing approval", "S1" in standing()
           and "still holds" in out)
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("after an agent's keys lock, the next grant goes straight through", code == 0
           and holds(s1, "WEATHER_API_KEY"))
    run("keys.py", "lock")
    expect("keys lock from the person's terminal ends the standing approval", "S1" not in standing())

    # 5a. the Nov 23 sequence: approve, grants near their end, approve again, grant, lock, grant
    reset()
    say("approve", "S1")
    grant(s1, "WEATHER_API_KEY", "MAPS_API_KEY")
    shift_standing("S1", 11.83)
    shift_grants("S1", 11.83)
    expect("set-up: the grants have about 10 minutes left", 5 * 60 < left("S1", "WEATHER_API_KEY") < 15 * 60)
    model("YES")
    out = say("and spritely you are approved", "S1")
    expect("a new approve renews the standing approval for 12 hours", "12 hours" in out
           and standing()["S1"]["until"] - time.time() > 11.9 * 3600)
    expect("a new approve renews the grants the session already holds", left("S1", "WEATHER_API_KEY") > 11.9 * 3600
           and left("S1", "MAPS_API_KEY") > 11.9 * 3600)
    # grants with 10 minutes left while the standing approval still has hours left
    (tmp / "grants.json").write_text(json.dumps({"callers": {"S1": {
        "WEATHER_API_KEY": time.time() + 600, "MAPS_API_KEY": time.time() + 600}}}))
    code, out = grant(s1, "WEATHER_API_KEY")
    expect("keys grant under a standing approval renews a grant near its end", code == 0
           and left("S1", "WEATHER_API_KEY") > 11.9 * 3600)
    expect("use.log says the grant was renewed", "renewed by the approve in the chat" in use_log())
    expect("keys grant renews only the keys it names", left("S1", "MAPS_API_KEY") < 15 * 60)
    run("keys.py", "lock", token=s1)
    code, out = grant(s1, "WEATHER_API_KEY", "MAPS_API_KEY")
    expect("after lock, the next grant goes straight through without a new approve", code == 0
           and "Reply approve" not in out and holds(s1, "MAPS_API_KEY") and left("S1", "MAPS_API_KEY") > 11.9 * 3600)
    cfg = json.loads((tmp / "config.json").read_text())
    (tmp / "config.json").write_text(json.dumps(dict(cfg, protected=["VAULT_TOKEN"])))
    reset()
    say("approve", "S1")
    # stand-in for the tap: a grant of the protected key with 10 minutes left
    (tmp / "grants.json").write_text(json.dumps({"callers": {"S1": {"VAULT_TOKEN": time.time() + 600}}}))
    grant(s1, "VAULT_TOKEN")
    expect("a protected key is not renewed by the standing approval", left("S1", "VAULT_TOKEN") < 15 * 60)
    (tmp / "config.json").write_text(json.dumps(cfg))

    # 5b. no model: nothing changes, and the agent is told
    reset()
    say("approve", "S1")
    grant(s1, "WEATHER_API_KEY")
    before = standing()
    no_model()
    out = say("no, don't use the keys", "S1")
    expect("no model: the standing approval and grants stay", standing() == before and holds(s1, "WEATHER_API_KEY"))
    expect("no model: the agent is told nothing changed", "couldn't be reached" in out
           and "changed no key approvals" in out)
    reset()
    grant(s1, "MAPS_API_KEY")
    no_model()
    out = say("approve", "S1")
    expect("no model: a waiting request stays waiting", "couldn't be reached" in out
           and not holds(s1, "MAPS_API_KEY") and json.loads((tmp / "pending.json").read_text()))
    reset()
    no_model()
    out = say("approve", "S1")
    expect("no model: nothing is approved in advance", not standing() and "couldn't be reached" in out)

    # 6. the model sees the agent's last message
    reset()
    model("YES")
    log = transcript("Status: the deploy is ready. I need R2_SECRET and WEATHER_API_KEY. Reply approve.")
    out = say("go, go", "S1", log)
    prompt = fake_model.last_prompt(FAKE)
    expect("the model is given the agent's last message", "I need R2_SECRET and WEATHER_API_KEY" in prompt
           and "a subagent's own message" not in prompt)
    expect("'go, go' after the agent asked for keys approves in advance", "approved key use in advance" in out)
    reset()
    model("UNCLEAR")
    out = say("fix the approve flow in the settings page", "S1", transcript("I changed the settings page."))
    expect("a message that only mentions approve doesn't approve", "additionalContext" not in out and not standing())
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
