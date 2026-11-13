---
name: keywarden
description: Use keywarden when a task needs API keys or other secrets. Save supplied keys once, use their names in commands, and request expiring access to existing keys. A user supplying a key with a task grants one hour in the default chat mode.
---

# keywarden

Use saved names such as `OPENAI_API_KEY`. Once saved, pass the value through environment variables; never repeat it in chat, source code, or tool arguments.

## A person supplies a key with a task

The normal workflow is: they are building something, supply a key with their request, and continue building.

- The prompt integration saves detected keys. In default chat mode, a key supplied with a task receives a one-hour grant because the person explicitly asked to use it. A save-only request stores the key without granting access. Protected keys and phone approval mode retain their stronger checks.
- Follow the capture notice. If it says a key is saved and approved, confirm exactly: **“Saved as NAME. Approved for 1 hour.”** Then continue their task without asking for the same approval again. Run `keys status` if you need to check the actual grant.
- Claude Code's plugin replaces the value with `[NAME]` before forwarding the message to the model.
- Codex and Claude's command-hook fallback save the key and continue the task, but cannot replace the submitted message. The raw paste can reach the model and local history. State that limitation when relevant; never claim those paths keep the paste away from the provider.
- A helper attempts to redact the supplied local transcript in the background. Do not read or edit session files yourself, and do not claim all copies were erased.
- If a supplied value was not detected, don't put it in a shell command to save it: that creates another logged copy. Ask the person to run `keys add NAME --clipboard` in their terminal. This is also an alternative for anyone who wants to avoid pasting into chat.

## Reuse a saved key

1. Check `keys status` and the saved names provided at session start. Use an existing saved key before asking for its value again.
2. If it is approved, continue. Grants are shared by local sessions on this computer until expiry or `keys lock`.
3. If it has expired or is unapproved, run `keys grant NAME --for "what you're doing"`. Request only the keys the task needs, together in one command.
4. If approval is pending, ask in chat: **“Use NAME for this task? Reply approve, approve 30m, approve 2h, or no.”** End your turn. The user-message hook processes the reply; then retry the command. A plain `approve` grants one hour by default. If they deny, stop using the key.
5. If phone or local biometric approval is required, tell them where to approve. Do not bypass it.

## Use keys in programs

Approved Bash and PowerShell commands receive approved values as environment variables. Use `"$OPENAI_API_KEY"` in Bash or `$env:OPENAI_API_KEY` in PowerShell. In code, read from the environment, for example `os.environ["OPENAI_API_KEY"]`.

- Pass credentials on stdin when supported: `docker login -u me --password-stdin <<< "$DOCKER_TOKEN"`.
- Use environment references in headers: `curl -H "Authorization: Bearer $OPENAI_API_KEY" ...`.
- `keys run NAME1 NAME2 -- command` passes selected keys explicitly and asks if approval is needed. It also inherits keys already present in its parent environment.
- Hooked output masks literal values and their base64 forms as `<concealed by keywarden>`. `keys run` masks literal values. Treat filtering as a guardrail; avoid printing secrets in the first place.

## Save keys created during work

Pipe generated keys directly into storage so they do not print:

`curl -s ... | python -c "import json,sys; print(json.load(sys.stdin)['token'])" | keys add NEW_TOKEN`

Also available: `keys add NAME --from-env VAR`, `keys import FILE.env`, and `keys note NAME "what it is"`. Do not create plaintext key files in the repository. Remove a source file only if the user authorized that and nothing still needs it.

## Changes and limits

- Rename, remove, replace, unprotect, and approval-mode changes must follow their built-in approval flow and the person's instructions.
- Use `keys lock NAME` to end one grant, or `keys lock` for all grants.
- If a hook blocks a command such as `printenv`, reading `.env`, or accessing keywarden's state, pass the key by name through the supported interface. Do not work around the block.
- These integrations require local hooks and access to the computer's key store. Codex requires hook trust review. Cloud sessions do not inherit local keys or hooks.
- 1Password and phone/biometric layers are optional; actual vault authentication and hardware approval remain unverified. Do not describe unit tests or fake-backend checks as real-device validation.
