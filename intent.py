"""Reads the person's reply to a waiting key request with a language model, not word lists.

Owner, Nov 21 2026: "it should not be using string matching ... it should be LLM based." A chat yes is not a
security check (anything that can type in the chat can type yes; the real check is Touch ID, a phone tap or a TOTP
code), so the point is to get the person's intent right: typos, angry messages and replies that mention other
things all count by what they mean.

judge() asks Claude Haiku through the local `claude` command (the person's own login, no API key needed). It
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


def _prompt(text, requests):
    asked = "\n".join(f"- {', '.join(r.get('names') or [])}: {r.get('what') or 'no reason given'}" for r in requests)
    return (
        "An AI coding agent asked a person for permission to use these saved API keys:\n"
        f"{asked or '- (keys not named)'}\n\n"
        "Below is the person's next chat message, between the markers. Decide what it says about that request.\n"
        "YES: the message gives permission, in any words, with typos, swearing or other topics mixed in "
        "(for example 'apprve go', 'GO', 'ok do it', 'I approve using a key to get this done').\n"
        "NO: the message refuses or withdraws permission for those keys.\n"
        "UNCLEAR: the message doesn't answer the request, or it hesitates ('wait', 'not yet', 'why do you need it?').\n"
        "A 'no' or 'don't' about something else in the message is not a refusal of the keys.\n"
        "Reply with exactly one word: YES, NO or UNCLEAR.\n\n"
        "<<<MESSAGE\n" + text[:4000] + "\nMESSAGE>>>"
    )


def judge(text, requests):
    exe = os.environ.get("KEYWARDEN_CLAUDE") or shutil.which("claude")
    if not exe:
        raise Unavailable("the claude command isn't installed")
    env = dict(os.environ, **{GUARD: "1"})
    env.pop("AGENT_KEYS_CALLER", None)
    try:
        proc = subprocess.run([exe, "-p", "--model", MODEL, "--output-format", "text"], input=_prompt(text, requests),
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
