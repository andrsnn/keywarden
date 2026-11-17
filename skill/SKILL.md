---
name: keywarden
description: Use keywarden when a task needs API keys or other secrets. Save keys once, use them by name, and request thread approval before access. Approved keys are shared with that thread's subagents and subprocesses until expiry.
---

# keywarden

Use saved names such as `OPENAI_API_KEY`. Pass values through environment variables; never repeat a value in chat, source code, or tool arguments.

## Approval workflow

- **Approvals are on by default.** Each Claude Code or Codex thread approves specific key names. Its subagents and subprocesses share that approved set and expiry. Another thread needs separate approval, even though both agents use the same store.
- A key supplied directly to the thread is saved and approved for 12 hours by default. Confirm the actual capture notice briefly, then continue. Protected keys and phone mode retain their extra checks.
- Users can preapprove saved names on a separate line: `You'll need these keys: OPENAI_API_KEY, GITHUB_TOKEN for 8h` or `approve OPENAI_API_KEY GITHUB_TOKEN for 8h`. The prompt hook records this consent; no second approval reply is needed. Without a duration, named consent lasts one hour. A named approval with a new duration sets its new expiry.
- Use `keys status` to check current access. For an unapproved key, run `keys grant NAME --for "what you're doing"`. If it requests chat approval, ask: **“Use NAME for this task? Reply approve, approve 30m, approve 2h, or no.”** End your turn and retry after approval. Stop using the key if denied.
- A subagent can request an additional key; the user answers in the parent thread. That approval covers the thread and its other helpers too.
- Long context, compaction, and resume do not renew grants. For Nightshift or other overnight work, ask for the named keys and duration up front. When an additional key or renewed access is needed, ask before continuing work that requires it. Do not treat remembering a key name or an old approval message as current permission.
- `keys lock` from the thread or a subagent clears the whole thread's grants. The developer's terminal can clear everyone's. Expired or locked grants do not remove credentials already received by running processes.
- With 1Password, `keys grant` may request an unlock first. Phone or protected-key policies may require a tap. Follow the configured route.

`keys approvals on` (or `chat`) enables the default control; `keys approvals phone` enables phone/local biometric approval. Explicit `keys approvals off` settings are preserved. In that mode, ordinary keys work automatically across threads; do not ask for redundant chat approval or claim `keys lock` revokes automatic access. Change modes only when the user asks; follow required confirmation.

## Capture and use keys

- Follow capture notices in a short normal reply, such as **“Saved NAME to keywarden. Approved for this thread (12 hours).”** Use the actual mode and duration. Do not quote hook names or implementation details.
- Claude's plugin replaces detected values with names before forwarding. Codex and Claude's command-hook fallback continue with the original prompt, so the pasted value can reach the model and local history. Local cleanup is best-effort; do not claim every copy was erased.
- A key file attached with `@` (a private key block or a known key format) is saved under the file's name and kept out of the chat. Use the saved name; don't read the file.
- If a pasted value was not detected, don't put it into another logged tool call to save it. Ask the person to run `keys add NAME --clipboard` in their terminal. This also avoids pasting values into chat entirely.
- Check names provided at session start or run `keys list` / `keys status`. Don't ask for a value already saved.
- Hooked Bash and PowerShell commands receive approved keys through environment variables: `"$OPENAI_API_KEY"` or `$env:OPENAI_API_KEY`. Code should read from the environment, such as `os.environ["OPENAI_API_KEY"]`.
- Pass a key on stdin when supported: `docker login -u me --password-stdin <<< "$DOCKER_TOKEN"`.
- Use environment references in headers: `curl -H "Authorization: Bearer $OPENAI_API_KEY" ...`.
- `keys run NAME1 NAME2 -- command` passes selected keys under the configured policy. It also inherits its parent environment.
- Hooked output masks literal values and their base64 forms. `keys run` masks literal values. Avoid printing secrets even when a filter is present.

## Save generated keys

Pipe values directly into storage instead of printing them:

`curl -s ... | python -c "import json,sys; print(json.load(sys.stdin)['token'])" | keys add NEW_TOKEN`

Other inputs: `keys add NAME --from-env VAR`, `keys import FILE.env`. Use `keys note NAME "what it is"` for context. Don't create plaintext key files in the repository.

## Boundaries

- Never set, clear, or copy `AGENT_KEYS_CALLER`; hooks manage thread identity.
- Do not bypass hooks blocking secret exposure or access to keywarden state. Use supported commands and key names.
- These integrations require local hooks and the computer's key store. Codex requires hook trust review; cloud sessions do not inherit local keys.
- This is hook-based thread control, not OS process isolation. It does not stop malicious code running as the same user or limit which API calls an approved credential can make.
- `keys touchid on` (optional, off by default) makes `keys grant` open a Touch ID prompt on the Mac (Windows Hello on Windows) before asking in the chat. If it says the key was denied with Touch ID, tell the person; don't run the grant again in a loop. If it says the chat asks instead, ask in the chat as usual. Don't turn it on or off yourself unless the person asks.
- 1Password and phone/biometric layers exist, but real authentication and hardware remain unverified. Fake-backend tests are not real-device validation.
