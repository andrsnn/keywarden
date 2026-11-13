# keywarden

Coding agents can scatter API keys across project files and chat history. **keywarden saves a key once, grants temporary access, and lets your agent reuse it by name.**

For **Claude Code** and **local Codex**.

## Features

- 🔁 **One store, both agents.** Save a key with Claude, use it from Codex—or the other way around. No copying keys between tools.
- 💬 **Stay in the conversation.** Give your agent a task and a key; keywarden saves it while you keep building.
- ⏳ **One hour to get started.** Supplying a key with a task approves first use. After expiry, choose access in chat.
- 🔐 **Encrypted storage.** Keys live in your OS key store, with optional 1Password support.
- 🧹 **Fewer plaintext copies.** Saved-name references, output filtering, and best-effort local transcript cleanup.

<p align="center"><img src="docs/how-it-works.svg" alt="Give your agent a task and a key. keywarden saves it and approves one hour. Later, approve reuse in chat." width="910"></p>

## The workflow

**1. You're building something. Your agent needs an API key.**

**2. Give it the task and the key, in the same message.**

```text
You: Build an AI chat app with this OpenAI API key: sk-…
```

**3. keywarden saves it and approves access for one hour.** You already asked the agent to use it, so there's no extra approval step. The agent confirms and keeps working:

```text
Agent: Saved as OPENAI_API_KEY. Approved for 1 hour.
```

The value lives in your key store. The agent uses `OPENAI_API_KEY` in commands instead of copying the value into your project. A request to **just save** a key stores it without granting access.

**4. Next time, reuse the saved key.** Once its grant has expired, the agent asks:

```text
Agent: Use OPENAI_API_KEY for this task?
       Reply approve, approve 30m, approve 2h, or no.

You: approve 30m
```

No need to find or paste the value again. `approve` alone grants one hour by default. An active grant is shared by local sessions on this computer; it isn't scoped to one project. End access early with `keys lock`.

**Switch agents freely.** Claude and Codex discover the same saved names, use the same selected backend, and share approvals. You can start a task in Claude and continue in Codex without storing the key again. Locking a key ends access for both.

## Install once

Requires Python 3 and macOS or Windows. Keep the checkout: the installed command and hooks run its scripts.

```sh
git clone https://github.com/andrsnn/keywarden
cd keywarden
python3 install.py
keys setup local
```

Restart your agent. In **Codex**, review and trust keywarden in `/hooks`, or **Settings > Hooks** on desktop. If the installer changes your PATH, open a new terminal before `keys setup local`. On Windows, use `python` if `python3` isn't available.

The installer adds the command, hooks, and skill for both agents, plus Claude's prompt-replacement plugin. Use `--agent claude` or `--agent codex` to install just one integration.

## Why this helps

| Protection | What it does |
| --- | --- |
| **One key store** | Saves values in macOS Keychain or Windows Credential Manager, instead of scattering plaintext copies through projects. |
| **One-hour first use** | A key supplied with a task gets temporary access. Later use asks again after expiry. |
| **Names in commands** | Programs receive approved values through environment variables. Common attempts to print keys or write detected values into files are blocked. |
| **Filtered output** | Hooked commands replace matching values and their base64 forms with `<concealed by keywarden>`. |
| **Local cleanup** | A helper attempts transcript redaction in the background without replacing the live file. |

**Pasting still has limits.** Claude's plugin replaces detected values before forwarding to the model. Codex and Claude without the plugin continue with the original prompt, so the initial paste can reach the model and local history. Redaction is best-effort: it cannot erase in-memory context, provider copies, backups, or every local log. Detection is not exhaustive, and these hooks cannot stop a determined program running as you.

To keep the value out of chat entirely, copy it and run `keys add NAME --clipboard` in your terminal. That saves it and clears the clipboard; the agent requests approval when it needs the saved key.

## Optional layers

Start with the local store and chat approvals. These additional integrations exist, but **real 1Password authentication and MFA hardware have not been verified**.

| Layer | Purpose | Status |
| --- | --- | --- |
| **1Password** | Synced keys and shared vaults. Connect its app and CLI, then run `keys setup 1password`. | Storage behavior tested with a fake CLI; real vault authentication unverified. |
| **Phone / biometrics** | Passkey approval over Tailscale, or Touch ID / Windows Hello. Start with `keys phone`. | Signature logic tested; real phone and biometric approval unverified. |

`keys protect NAME` and phone-required approval mode keep their extra check: supplying a key does not bypass them. With 1Password, `keys strict on` disables local caching and reads fresh through 1Password's authentication; 1Password controls when it prompts.

<details>
<summary><strong>Useful commands</strong></summary>

| Command | Purpose |
| --- | --- |
| `keys list` / `keys status` | Find saved keys and see active grants. |
| `keys note NAME "description"` | Tell agents what a key is for. |
| `keys lock [NAME]` | End one grant, or all grants. |
| `keys add NAME --clipboard` | Save a copied key without pasting into chat. |
| `keys import FILE.env` | Save keys from an existing file. |
| `keys run NAME -- command` | Pass selected keys to a program, with approval. |
| `keys rename OLD NEW` / `keys remove NAME` | Rename or delete with approval. |
| `keys switch 1password` / `keys switch local` | Copy keys to another store and select it. Old copies remain. |
| `keys approvals chat` / `phone` / `off` | Choose approval mode. Turning approvals off needs approval. |
| `keys rescue --save --redact` | Save recognized keys and scrub Claude's local logs. This command does not search Codex logs. |

Grant requests and approvals are recorded in `~/.agent-keys/use.log` with names, reasons, and times. By default, 1Password values are cached in the OS store during approval; expired copies are cleared on the next cache check or `keys lock`.

Uninstall with `python3 install.py --uninstall`. Saved keys stay in their store.

</details>

## Tests

```sh
python3 test_all.py
```

Tests cover first-use approval, expiry, later chat approvals, secret detection, storage, output masking, transcript redaction, and installer updates. Native Claude and Codex tests use a local model stub and synthetic keys; no real model API is called. Storage tests use separate OS key-store namespaces and a fake 1Password CLI. Phone tests need `cryptography`; shell tests need Bash. Windows and real approval hardware remain unverified.

Hook references: [Codex](https://learn.chatgpt.com/docs/hooks), [Claude Code](https://code.claude.com/docs/en/hooks).
