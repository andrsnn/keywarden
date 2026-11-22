"""Reads the person's reply to a waiting key request with a language model, not word lists.

Owner, Nov 21 2026: "it should not be using string matching ... it should be LLM based." A chat yes is not a
security check (anything that can type in the chat can type yes; the real check is Touch ID, a phone tap or a TOTP
code), so the point is to get the person's intent right: typos, angry messages and replies that mention other
things all count by what they mean.

judge() gets the agent's last message too, when the hook can read it, so "approve, go" after a status message
that lists the keys reads as the answer it is. It asks Claude Haiku through the local `claude` command (the person's own login, no API key needed). It
returns True (yes), False (no), None (the reply doesn't answer the request), or raises Unavailable when no model
could be reached, so the caller can fall back to the old word lists.
"""
import json
import os
import shutil
import subprocess

TIMEOUT = 30
MODEL = os.environ.get("KEYWARDEN_INTENT_MODEL", "haiku")
# Set in the child process so keywarden's own prompt hook does nothing inside the model call.
GUARD = "KEYWARDEN_INTENT_CALL"


class Unavailable(Exception):
    pass


def _prompt(text, requests, context=None):
    asked = "\n".join(f"- {', '.join(r.get('names') or [])}: {r.get('what') or 'no reason given'}" for r in requests)
    if requests:
        head = f"An AI coding agent asked a person for permission to use these saved API keys:\n{asked}\n\n"
    else:
        head = ("An AI coding agent works for a person and may need the person's permission to use saved API keys. "
                "No key request is waiting right now: the agent may have asked in its message without filing one, "
                "or the person may be approving ahead of time.\n\n")
    if context:
        head += ("The agent's last message to the person, for context (it may list the keys it needs):\n"
                 "<<<AGENT\n" + context[-3000:] + "\nAGENT>>>\n\n")
    return (
        head +
        "Below is the person's next chat message, between the markers. Decide what it says about letting the agent "
        "use the keys.\n"
        "YES: the message gives permission, in any words, with typos, swearing or other topics mixed in "
        "(for example 'apprve go', 'GO', 'approve, approve, go, go', 'ok do it', 'I approve using a key to get this "
        "done'). A complaint that the person already said approve, or that approving keeps failing, is a YES "
        "('I keep saying approve, it's not working, please fix').\n"
        "NO: the message refuses or withdraws permission for those keys, or tells the agent to stop.\n"
        "UNCLEAR: the message doesn't answer about the keys, or it hesitates ('wait', 'not yet', 'why do you need "
        "it?'). A yes to some other question is UNCLEAR.\n"
        "A 'no', 'not' or 'don't' about something else in the message is not a refusal of the keys.\n"
        "Reply with exactly one word: YES, NO or UNCLEAR.\n\n"
        "<<<MESSAGE\n" + text[:4000] + "\nMESSAGE>>>"
    )


def judge(text, requests, context=None):
    exe = os.environ.get("KEYWARDEN_CLAUDE") or shutil.which("claude")
    if not exe:
        raise Unavailable("the claude command isn't installed")
    env = dict(os.environ, **{GUARD: "1"})
    env.pop("AGENT_KEYS_CALLER", None)
    try:
        proc = subprocess.run([exe, "-p", "--model", MODEL, "--output-format", "text"], input=_prompt(text, requests, context),
                              capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(str(e)[:200])
    words = (proc.stdout or "").strip().upper().split()
    word = next((w.strip(".!*\"'`") for w in reversed(words) if w.strip(".!*\"'`") in ("YES", "NO", "UNCLEAR")), None)
    if proc.returncode != 0 or word is None:
        raise Unavailable(f"model answer not understood (exit {proc.returncode})")
    return {"YES": True, "NO": False, "UNCLEAR": None}[word]


if __name__ == "__main__":
    import sys
    reqs = [{"names": ["EXAMPLE_KEY"], "what": "testing"}]
    print(json.dumps(judge(sys.stdin.read(), reqs)))
