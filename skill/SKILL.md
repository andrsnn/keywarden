---
name: keywarden
description: Use keywarden when a task needs API keys or other secrets. Save keys once and use their names in commands. Saved keys work automatically by default; optional approval controls restrict each session and subagent separately.
---

# keywarden

Use saved names such as `OPENAI_API_KEY`. Pass values through environment variables; never repeat a value in chat, source code, or tool arguments.

## Default workflow: save and keep building

- The user gives you a task and a key. The prompt integration saves detected keys, tells you their names, and confirms the actual access mode.
- Approval controls are **off by default**. Ordinary saved keys work automatically in Claude Code, Codex, other sessions, and their subagents. Do not ask the user to type approve in this mode.
- Follow the capture notice. In automatic mode, confirm: **“Saved as NAME. Ready for Claude Code, Codex, and their subagents.”** Then continue the task.
- Check `keys status` when unsure which mode is active. Explicitly protected keys still require their extra approval even when controls are off.
- Claude's plugin replaces detected values with names before forwarding. Codex and Claude's command-hook fallback continue with the original prompt, so the pasted value can reach the model and local history. Local transcript cleanup is best-effort; do not claim every copy was erased.
- If a pasted value was not detected, don't put it into another logged tool call to save it. Ask the person to run `keys add NAME --clipboard` in their terminal. This also works for users who prefer to keep values out of chat entirely.

## Optional approval and session binding

`keys approvals on` (or `chat`) enables the extra control. `keys approvals phone` enables phone/local biometric approval. `keys approvals off` disables it. Change the mode only when the user asks; follow any confirmation it requires.

When enabled:

1. Use an already approved key. A key supplied directly to a session works in that session for 12 hours by default; follow the actual notice and status.
2. Every other session and **every subagent** needs its own approval. Parent approval does not approve a subagent. Tell the user which caller needs which key and why.
3. Run `keys grant NAME --for "what you're doing"`. If it requests chat approval, ask: **“Use NAME for this task? Reply approve, approve 30m, approve 2h, or no.”** End your turn. The user-message hook applies the reply to the requesting caller. Retry after approval; stop using the key if denied.
4. With 1Password, `keys grant` may request an unlock first. Phone or protected-key policies may require a tap. Follow the configured route.
5. `keys lock` from an agent clears that caller's grants. The developer's terminal can clear everyone's. Expired or locked grants do not remove credentials already received by running processes.

When controls are off, ordinary access is renewed automatically. `keys lock` clears grants but does not revoke automatic access; do not claim it blocks future use. Enable the control if the user wants access to require approval.

## Use and discover keys

- Check names provided at session start or run `keys list` / `keys status`. Both agents use the same selected store; don't ask for a value already saved.
- Hooked Bash and PowerShell commands receive available keys through environment variables: `"$OPENAI_API_KEY"` or `$env:OPENAI_API_KEY`. Code should read from the environment, such as `os.environ["OPENAI_API_KEY"]`.
- Pass a key on stdin when supported: `docker login -u me --password-stdin <<< "$DOCKER_TOKEN"`.
- Use environment references in headers: `curl -H "Authorization: Bearer $OPENAI_API_KEY" ...`.
- `keys run NAME1 NAME2 -- command` passes selected keys explicitly under the configured policy. It also inherits its parent environment.
- Hooked output masks literal values and their base64 forms. `keys run` masks literal values. Avoid printing secrets even when a filter is present.

## Save generated keys

Pipe values directly into storage instead of printing them:

`curl -s ... | python -c "import json,sys; print(json.load(sys.stdin)['token'])" | keys add NEW_TOKEN`

Other inputs: `keys add NAME --from-env VAR`, `keys import FILE.env`. Use `keys note NAME "what it is"` for context. Don't create plaintext key files in the repository.

## Boundaries

- Never set, clear, or copy `AGENT_KEYS_CALLER`; hooks manage caller identity, including protected-key checks and optional binding.
- Do not bypass hooks blocking secret exposure or access to keywarden state. Use the supported commands and key names.
- These integrations require local hooks and the computer's key store. Codex requires hook trust review; cloud sessions do not inherit local keys.
- This is hook-based session control, not OS process isolation. It does not stop malicious code running as the same user.
- 1Password and phone/biometric layers exist, but real authentication and hardware remain unverified. Fake-backend tests are not real-device validation.
