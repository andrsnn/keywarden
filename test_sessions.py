"""Run: python test_sessions.py   Checks that approvals belong to one caller (a session, or a subagent inside it):
the session a key was given to can use it, and a subagent, another session or Codex has to ask. Uses a test key
store, so real keys are never touched."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.pop("AGENT_KEYS_CALLER", None)  # run as a person at a terminal, not as the agent running the tests

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="agent-keys-sessions-"))
base = dict(os.environ, AGENT_KEYS_STATE=str(tmp), AGENT_KEYS_NS="agent-keys-sessiontest")
base.pop("AGENT_KEYS_BACKEND", None)
ok, bad = [], []
VALUE = "wx_" + "Qm7Lp" * 6


def run(script, *args, stdin=None, token=None):
    env = dict(base)
    if token:
        env["AGENT_KEYS_CALLER"] = token
    r = subprocess.run([sys.executable, str(HERE / script), *args], input=stdin, capture_output=True, text=True,
                       env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr


def hook(session, command="keys status", agent=None, codex=False):
    """What the command hook does to a Bash command from this session (and subagent)."""
    data = {"session_id": session, "tool_name": "Bash", "tool_input": {"command": command}}
    if agent:
        data["agent_id"] = agent
    code, out = run("hook.py", *(["--codex"] if codex else []), stdin=json.dumps(data))
    return code, out


TOKEN = re.compile(r"AGENT_KEYS_CALLER(?:=| = ')([\w-]+)")


def token(session, agent=None, codex=False):
    out = json.loads(hook(session, agent=agent, codex=codex)[1])["hookSpecificOutput"]["updatedInput"]["command"]
    return TOKEN.search(out).group(1)


def gets_key(tok):
    return f"export WEATHER_API_KEY=" in run("keys.py", "env", token=tok)[1]


def say(text, session):
    return run("prompt_hook.py", stdin=json.dumps({"prompt": text, "session_id": session}))[1]


def expect(name, cond):
    (ok if cond else bad).append(name)


try:
    run("keys.py", "setup", "local")
    s1, s1_sub, s2 = token("S1"), token("S1", "A1"), token("S2")
    expect("a session keeps one token", token("S1") == s1)
    expect("a subagent gets its own token", s1_sub != s1)
    expect("another session gets its own token", s2 != s1)

    msg = "here is my weather api key, save this: " + VALUE + " and get the forecast"
    out = json.loads(run("prompt_hook.py", "--rewrite", stdin=json.dumps({"prompt": msg, "session_id": "S1"}))[1])
    expect("giving a key with a task approves it at once", out.get("approved") == ["WEATHER_API_KEY"]
           and "This session can use it" in out.get("notice", ""))
    expect("the session the key was given to gets it", gets_key(s1))
    expect("its subagent doesn't", not gets_key(s1_sub))
    expect("another session doesn't", not gets_key(s2))
    expect("a terminal with no token doesn't", not gets_key(None))
    expect("a made-up token gets nothing", not gets_key("not-a-real-token-123"))

    code, out = hook("S1", "curl https://example.com")
    cmd = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
    expect("the session's commands load its keys", " env)" in cmd and "mask.py" in cmd)
    code, out = hook("S1", "curl https://example.com", agent="A1")
    cmd = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
    expect("a subagent's commands only say who is asking", " env)" not in cmd and cmd.startswith("export "))

    code, out = run("keys.py", "grant", "WEATHER_API_KEY", "--for", "the forecast", token=s2)
    expect("another session has to ask", code != 0 and "Reply approve" in out)
    out = say("approve", "S2")
    expect("approve answers that request", "approved WEATHER_API_KEY" in out)
    expect("the session that asked gets the key", gets_key(s2))
    expect("the subagent that didn't ask still doesn't", not gets_key(s1_sub))

    code, out = run("keys.py", "grant", "WEATHER_API_KEY", "--for", "subagent work", token=s1_sub)
    expect("a subagent has to ask", code != 0 and "Reply approve" in out)
    say("approve", "S2")
    expect("approve in another session doesn't answer the subagent", not gets_key(s1_sub))
    say("approve 15m", "S1")
    expect("approve in the subagent's own session gives it the key", gets_key(s1_sub))

    run("keys.py", "lock", token=s1)
    expect("keys lock in a session ends only its own approval", not gets_key(s1) and gets_key(s2))
    run("keys.py", "lock")
    expect("keys lock at a terminal ends every approval", not gets_key(s2) and not gets_key(s1_sub))

    # Setting or clearing the token by hand is blocked; reading the name is fine.
    for c in ["export AGENT_KEYS_CALLER=abc; keys env", "unset AGENT_KEYS_CALLER; keys run X -- y",
              "env -u AGENT_KEYS_CALLER keys status", "$env:AGENT_KEYS_CALLER = 'abc'",
              "Remove-Item Env:AGENT_KEYS_CALLER", "python -c \"import os; os.environ.pop('AGENT_KEYS_CALLER')\""]:
        expect(f"blocks: {c}", hook("S1", c)[0] == 2)
    expect("allows searching for the name", hook("S1", "grep -n AGENT_KEYS_CALLER hook.py")[0] == 0)

    # Codex: only commands that load keys or call keys are changed, because a changed command skips its prompt.
    code, out = hook("C1", "ls", codex=True)
    expect("Codex: an ordinary command is left alone", code == 0 and out.strip() == "")
    code, out = hook("C1", "keys grant WEATHER_API_KEY", codex=True)
    expect("Codex: a keys command carries its own token", bool(TOKEN.search(out)) and '"allow"' in out)
    code, out = run("keys.py", "grant", "WEATHER_API_KEY", "--for", "codex", token=token("C1", codex=True))
    expect("a Claude session's grant doesn't reach Codex", code != 0 and "Reply approve" in out)

    # Keys in 1Password: a new session is approved by unlocking 1Password instead of typing approve.
    sys.path.insert(0, str(HERE))
    from test_backends import FAKE_OP
    op_dir = tmp / "op"
    op_dir.mkdir()
    (op_dir / "fakeop.py").write_text(FAKE_OP)
    db = op_dir / "op.json"
    db.write_text(json.dumps({"vaults": {"Agent Keys": {"WEATHER_API_KEY": VALUE}}}))
    base.update(AGENT_KEYS_BACKEND="1password", AGENT_KEYS_OP=f'"{sys.executable}" "{op_dir / "fakeop.py"}"',
                FAKE_OP_DB=str(db), OP_SERVICE_ACCOUNT_TOKEN="ops_fake_service_token")
    s5 = token("S5")
    code, out = run("keys.py", "grant", "WEATHER_API_KEY", "--for", "the forecast", token=s5)
    state = json.loads(db.read_text())
    expect("1Password: unlocking approves a new session", code == 0 and "approved by unlocking 1Password" in out)
    expect("1Password: the unlock goes through the app, not the saved token",
           state.get("unlock_prompts") == 1 and not state.get("saw_token"))
    expect("1Password: the unlocked session gets the key", gets_key(s5))
    expect("1Password: another session still doesn't", not gets_key(token("S6")))
    base["FAKE_OP_UNLOCK"] = "deny"
    code, out = run("keys.py", "grant", "WEATHER_API_KEY", "--for", "the forecast", token=token("S7"))
    expect("1Password: a dismissed unlock falls back to the chat", code != 0 and "didn't approve" in out
           and "Reply approve" in out and not gets_key(token("S7")))
    base["FAKE_OP_UNLOCK"] = "no-app"
    code, out = run("keys.py", "grant", "WEATHER_API_KEY", "--for", "the forecast", token=token("S8"))
    expect("1Password: with no 1Password app, approve in the chat", code != 0 and "Reply approve" in out)
    say("approve", "S8")
    expect("1Password: the chat approval still works", gets_key(token("S8")))
finally:
    sys.path.insert(0, str(HERE))
    os.environ.update(AGENT_KEYS_NS="agent-keys-sessiontest", AGENT_KEYS_STATE=str(tmp))
    try:
        import local_store
        for n in local_store.names():
            local_store.delete(n)
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

for name in bad:
    print("WRONG:", name)
print(f"{len(ok)}/{len(ok) + len(bad)} session cases right")
sys.exit(1 if bad else 0)
