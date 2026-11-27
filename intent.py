"""Reads what the person's chat message means for key approvals, with a language model. No word lists.

Owner, Nov 21 2026: "it should not be using string matching ... it should be LLM based." Owner, Nov 22: "Delete
any hard-coded string matching check in keywarden and make it all LLM based." A chat yes is not a security check
(anything that can type in the chat can type yes; the real check is Touch ID, a phone tap or a TOTP code), so the
point is to get the person's intent right: typos, dictation errors, angry messages and replies that mention other
things all count by what they mean.

read() gets the agent's last message too, when the hook can read it, so "approve, go" after a status message that
lists the keys reads as the answer it is. It also says whether a request is waiting and whether the session already
holds an approval, so "stop the build" with an approval in place reads as being about the build. It asks Claude
Haiku through the local `claude` command (the person's own login, no API key needed) and returns
{"answer": True | False | None, "minutes": number | None, "keys": [names]}. It raises Unavailable when no model
could be reached, and then the hook changes nothing.
"""
import json
import os
import re
import shutil
import subprocess

TIMEOUT = 30
CLASSIFY_TIMEOUT = 15  # under the 20 seconds the prompt plugin waits for a capture
MODEL = os.environ.get("KEYWARDEN_INTENT_MODEL", "haiku")
# Set in the child process so keywarden's own prompt hook does nothing inside the model call.
GUARD = "KEYWARDEN_INTENT_CALL"
ANSWERS = {"YES": True, "NO": False, "UNCLEAR": None}
CALLER_VAR = "AGENT_KEYS_" + "CALLER"


class Unavailable(Exception):
    pass


def _prompt(text, requests, context=None, held=False):
    asked = "\n".join(f"- {', '.join(r.get('names') or [])}: {r.get('what') or 'no reason given'}" for r in requests)
    if requests:
        head = f"An AI coding agent asked a person for permission to use these saved API keys:\n{asked}\n\n"
    else:
        head = ("An AI coding agent works for a person and may need the person's permission to use saved API keys. "
                "No key request is waiting right now: the agent may have asked in its message without filing one, "
                "or the person may be approving ahead of time.\n\n")
    head += ("The person has already approved key use for this session, and that approval is still in place.\n\n"
             if held else "The session holds no key approval right now.\n\n")
    if context:
        head += ("The agent's last message to the person, for context (it may list the keys it needs):\n"
                 "<<<AGENT\n" + context[-3000:] + "\nAGENT>>>\n\n")
    return (
        head +
        "Below is the person's next chat message, between the markers. The person often dictates by voice, so words "
        "can be wrong ('stop' for 'start', 'aprove' for 'approve'). Decide what the message says about letting the "
        "agent use the keys.\n"
        "YES: the message gives permission, in any words, with typos, swearing or other topics mixed in "
        "(for example 'apprve go', 'GO', 'approve, approve, go, go', 'ok do it', 'I approve using a key to get this "
        "done'). A complaint that the person already said approve, or that approving keeps failing, is a YES "
        "('I keep saying approve, it's not working, please fix'). A bare 'yes' or 'go' is a YES only when a key "
        "request is waiting or the agent's last message asked for keys.\n"
        "NO: the message clearly refuses or withdraws permission to use the keys.\n"
        "- With a key request waiting, a 'no', 'stop', 'deny' or 'don't' that answers it is a NO, including a "
        "bare 'no' or 'stop' on its own.\n"
        "- With no key request waiting, only a message that explicitly withdraws permission to use keys is a NO "
        "('no, don't use the keys', 'revoke the keys', 'stop using my keys', 'I take back the approval').\n"
        "UNCLEAR: everything else. That includes a message about the work that doesn't mention keys, approval or "
        "permission, even when it says stop, wait, cancel or no ('stop the build, wrong branch', 'stop that agent', "
        "'stop, do X instead', 'no, the other file'). It also includes hesitation ('wait', 'not yet', 'why do you "
        "need it?') and a yes to some other question.\n"
        "A 'no', 'not', 'stop' or 'don't' about something else in the message is not a refusal of the keys.\n"
        "minutes: how long the person says the approval should last, in minutes ('approve for 2h' is 120), or null "
        "when they don't say.\n"
        "keys: saved key names the person approves by name in this message ('approve OPENAI_API_KEY', 'you can use "
        "R2_SECRET for 1h'), written exactly as the person wrote them. Names that are only mentioned in a task "
        "don't count. Usually [].\n"
        'Reply with one line of JSON and nothing else: {"answer": "YES" or "NO" or "UNCLEAR", "minutes": null or a '
        'number, "keys": []}\n\n'
        "<<<MESSAGE\n" + text[:4000] + "\nMESSAGE>>>"
    )


def _parse(out):
    """The model's JSON reply. A bare YES, NO or UNCLEAR is also understood."""
    for blob in reversed(re.findall(r"\{[^{}]*\}", out)):
        try:
            got = json.loads(blob)
        except ValueError:
            continue
        word = str(got.get("answer", "")).strip().upper()
        if word in ANSWERS:
            minutes = got.get("minutes")
            minutes = float(minutes) if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) else None
            keys = [k for k in got.get("keys") or [] if isinstance(k, str)]
            if minutes is not None and minutes <= 0:  # "approve for 0h" approves nothing
                return {"answer": None, "minutes": None, "keys": []}
            return {"answer": ANSWERS[word], "minutes": minutes, "keys": keys}
    words = [w.strip(".!*\"'`:") for w in out.upper().split()]
    word = next((w for w in reversed(words) if w in ANSWERS), None)
    if word is None:
        return None
    return {"answer": ANSWERS[word], "minutes": None, "keys": []}


def read(text, requests, context=None, held=False):
    exe = os.environ.get("KEYWARDEN_CLAUDE") or shutil.which("claude")
    if not exe:
        raise Unavailable("the claude command isn't installed")
    env = dict(os.environ, **{GUARD: "1"})
    env.pop(CALLER_VAR, None)
    try:
        proc = subprocess.run([exe, "-p", "--model", MODEL, "--output-format", "text"],
                              input=_prompt(text, requests, context, held), capture_output=True, text=True,
                              encoding="utf-8", timeout=TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(str(e)[:200])
    got = _parse(proc.stdout or "") if proc.returncode == 0 else None
    if got is None:
        raise Unavailable(f"model answer not understood (exit {proc.returncode})")
    return got


def _classify_prompt(text, items, context=None):
    listed = "\n".join(f"- [{i['label']}]: {i['shape']}" + (f", keywarden would call it {i['name']}" if i.get("name") else "")
                       for i in items)
    head = ("A person sent a chat message to an AI coding agent. keywarden, their key store, found values in it that "
            "might be secrets to save, or might be public identifiers that are not secret. The values are replaced "
            f"by labels, and here is what each one looks like:\n{listed}\n\n")
    if context:
        head += ("The agent's last message to the person, for context (it may ask for a key and name it):\n"
                 "<<<AGENT\n" + context[-3000:] + "\nAGENT>>>\n\n")
    return (
        head +
        "For each label, decide whether it is a SECRET the person wants saved (an API key, client secret, password, "
        "token or private key, including one sent on its own after the agent asked for it) or an ID that is not "
        "secret (a product, sandbox, deployment, client, app, account or project ID, a GUID, a commit hash, a request "
        "id, a database row id). When the message or the agent calls the value an ID, it is an ID. A client ID is an "
        "ID; a client secret is a SECRET.\n"
        "name: for a SECRET, the key name the person or the agent gave it (like HARD_BURN_EOS_CLIENT_SECRET), in "
        "upper case with underscores, or null when neither gave one.\n"
        'Reply with one line of JSON and nothing else, one entry per label: {"VALUE1": {"secret": true or false, '
        '"name": null or "NAME"}}\n\n'
        "<<<MESSAGE\n" + text[:4000] + "\nMESSAGE>>>"
    )


def classify(text, items, context=None):
    """Which found values are secrets to save and which are public IDs. text has each value swapped for [LABEL].
    items: [{"label", "shape", "name"}]. Returns {label: {"secret": bool, "name": str | None}} for the labels the
    model answered. Raises Unavailable when no model answered."""
    exe = os.environ.get("KEYWARDEN_CLAUDE") or shutil.which("claude")
    if not exe:
        raise Unavailable("the claude command isn't installed")
    env = dict(os.environ, **{GUARD: "1"})
    env.pop(CALLER_VAR, None)
    try:
        proc = subprocess.run([exe, "-p", "--model", MODEL, "--output-format", "text"],
                              input=_classify_prompt(text, items, context), capture_output=True, text=True,
                              encoding="utf-8", timeout=CLASSIFY_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(str(e)[:200])
    labels = {i["label"] for i in items}
    for blob in reversed(re.findall(r"\{.*\}", proc.stdout or "", re.S)):
        try:
            got = json.loads(blob)
        except ValueError:
            continue
        if not isinstance(got, dict):
            continue
        out = {}
        for label, v in got.items():
            if label in labels and isinstance(v, dict) and isinstance(v.get("secret"), bool):
                name = v.get("name")
                out[label] = {"secret": v["secret"],
                              "name": name if isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", name)
                              else None}
        if out:
            return out
    raise Unavailable(f"model answer not understood (exit {proc.returncode})")


def judge(text, requests, context=None, held=False):
    """True (yes), False (no) or None (the message doesn't answer about the keys)."""
    return read(text, requests, context, held)["answer"]


if __name__ == "__main__":
    import sys
    reqs = [{"names": ["EXAMPLE_KEY"], "what": "testing"}]
    print(json.dumps(read(sys.stdin.read(), reqs)))
