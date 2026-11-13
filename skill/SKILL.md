---
name: keywarden
description: Use whenever a task needs an API key, token, password or other secret, or when someone pastes one into the chat. Keys live in 1Password or the computer's key store. Run `keys grant NAME --for "why"`, and then every command you run has those keys as environment variables. You never see, print or write the values.
---

# keywarden

You work with key names like `CLOUDFLARE_API_TOKEN`. You never handle the values.

## To use keys
1. Run `keys status`. It lists the approved keys with their time left, and the names of the others.
2. For keys you need that aren't approved, run `keys grant NAME1 NAME2 --for "what you're doing"`. Ask only for the keys the task needs.
   - If the key already has a grant, you get it at once.
   - If not, the command says it needs the person's OK. Ask them in a plain chat message (not a question popup): "I need NAME. Reason: what you're doing. Reply approve, or approve 1h." Then end your turn. When they reply approve, a note tells you it's granted, so run `keys grant` again. If they say no, stop and ask what they want.
   - If a key is protected and their phone is set up, it waits for a tap instead: tell them "Approve NAME on your phone."
3. While approved, every Bash and PowerShell command you run has those keys as environment variables. Use them by name: `"$CLOUDFLARE_API_TOKEN"` in Bash, `$env:CLOUDFLARE_API_TOKEN` in PowerShell. If a command prints a key, it shows as `<concealed by keywarden>`.
   - Pass a key on stdin when a tool allows it: `docker login -u me --password-stdin <<< "$DOCKER_TOKEN"`.
   - Use a header: `curl -H "Authorization: Bearer $OPENAI_API_KEY" ...`.
   - Write a tool's config file only when the tool can't read the key any other way, and never inside a git repo.
4. Ask for all the keys a task needs in one `keys grant`, so the person approves once.

To pass selected keys explicitly to one program, use `keys run NAME1 NAME2 -- command`. It asks for approval if none is active. Like other commands, it also inherits any keys already in its parent environment.

## To save a key
- Save every key you create or receive (an API token you made, a key in a tool's output, a key in a `.env` file) right away, so other agents can use it. Name it in capitals, like `CLOUDFLARE_API_TOKEN`.
- From a command's output, pipe it straight in, so the value never prints: `curl -s ... | python -c "import json,sys; print(json.load(sys.stdin)['token'])" | keys add NEW_TOKEN`.
- From a variable: `keys add NEW_TOKEN --from-env NEW_TOKEN`.
- From a `.env` file: `keys import FILE.env`. Then delete the file if nothing else reads it.
- From the person: don't ask them to paste a key into the chat. Ask them to copy it, then run `keys add NAME --clipboard`. That saves it and clears the clipboard.
- If someone already pasted one, run `keys rescue --save --redact`, then tell them to rotate the key, because it was also sent to the model.
- `keys add` confirms with the first 4 characters and the length. That's all you ever see.

## Keys from the person
- Prefer `keys add NAME --clipboard` for both Claude Code and local Codex. The value stays out of the chat.
- In Claude Code with the plugin loaded, a pasted key is saved and replaced with `[NAME]` before you see it. Carry on with their request using the saved name.
- In Codex, or Claude Code without the plugin, the prompt hook saves a detected key and blocks the message. The person clears the value from the input and resends their request using the saved name. Don't claim Codex rewrites messages.
- Detection recognizes save requests, `key NAME value`, and well-known formats such as sk-..., ghp_..., AKIA..., and private key blocks. Run `keys list` to see the names, and `keys rename OLD NEW` if they want a different name.
- At the start of every session a note lists the saved key names. Check them before saying you don't have a key.
- The hook also stops you from writing a saved key, or anything shaped like a key, into a file or a command. Use `"$NAME"` instead. If code needs a key, read it from the environment, like `os.environ["NAME"]`.

## To change or delete a key
- `keys add NAME --replace`, `keys rename OLD NEW` and `keys remove NAME` go through the same check as `keys grant`. `keys unprotect` and `keys approvals off` always need the person's OK. Don't run them unless the person asks.

## Rules
- Never print, echo, log or copy a key's value, and never write one into a repo, a brief or a report.
- A hook blocks `printenv`, `env`, `echo $..._KEY`, `cat .env`, reading `.secrets`, reading the key store directly and `keys env`. If a command is blocked, find a way to pass the key by name. Don't look for a way around the block.
- If they deny the request, stop and ask what they want. Don't ask again in a loop.
- If `keys` says nobody approved and no phone is set up, tell them to run `keys phone`.
- These hooks run in local Claude Code and Codex sessions after installation and, for Codex, hook trust review. They do not provide key injection or chat approval in a cloud session without the local hooks. Use `keys run NAME -- command` when a command runs outside the hooked shell.
