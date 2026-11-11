# keywarden

keywarden keeps your API keys in one place so Claude can use them.

It fixes two problems:

1. Claude copies keys you paste into its chat logs, `.env` files and scripts all over your disk.
2. Claude says it doesn't have a key when it does. It forgot where it put it.

## How it works

1. **You paste a key into Claude and say "save this".** keywarden saves it before Claude sees it and tells you the name it used. If it spots a known kind of key (`sk-`, `ghp_`, `AKIA`, a private key) without "save this", it saves it anyway and tells you. To pick the name, send `key NAME value` instead.
2. **Claude looks in keywarden first.** Every Claude session starts with a list of the saved key names, so Claude uses one of those before saying it doesn't have a key.
3. **Claude asks for access.** If the key already has a grant, Claude gets it. If not, Claude asks you in the chat, and you reply approve (or approve 1h) to create a grant. A grant lasts 12 hours unless you say otherwise. With a grant, every command Claude runs has the key as `$NAME`, and Claude never sees the value.

## How it keeps your keys safe

- **Keys never sit in a file.** They're stored in 1Password or your computer's key store (Windows Credential Manager or the Mac Keychain), which encrypt them on disk. keywarden never writes a key to a `.env` file, a script or a log.
- **A pasted key never reaches Claude.** keywarden adds a hook to Claude Code that reads each message before Claude does. When it finds a key, it saves the key and stops the message, so the key is never sent to the model provider. Claude Code still writes the stopped message into the session's log file, so keywarden overwrites the key there with asterisks a second later.
- **Only you can approve.** When Claude asks for a key, the request waits until you reply approve. That reply goes through the same hook, which only sees messages you type, so Claude can't approve itself. Any other agent can ask with `keys grant` or `keys run NAME -- command`, and you approve it the same way. A grant only covers the keys you approved, and it ends after 12 hours, or sooner with `keys lock`.
- **Claude uses keys without seeing them.** While a key has a grant, a second hook puts the key into each command Claude runs as `$NAME`. If a command prints a key, the hook replaces it with `<concealed by keywarden>` before Claude reads the output.
- **Claude can't copy a key out.** The second hook blocks commands that would show a key, like `echo $OPENAI_API_KEY`, `printenv` or `cat .env`. It also stops Claude from writing a key into a file or a command, and from editing keywarden's own files or removing its hooks.
- **Every request and grant is logged** in `~/.agent-keys/use.log`, with the keys, the reason and the time.

What it can't stop: a program that runs as you on your computer, like malware, can still read the key store or fake a grant. keywarden keeps keys out of chats, logs and files. It doesn't protect a computer that's already hacked. With `keys strict on` and 1Password, every program that reads a key has to pass 1Password's own fingerprint or face check, which makes that harder.

## Install

```
git clone https://github.com/andrsnn/keywarden
cd keywarden
python install.py
keys setup
```

`install.py` also installs the 1Password command line tool if winget (Windows) or Homebrew (Mac) is there.

## Where your keys are stored

keywarden keeps keys in one of two places. `keys setup` picks 1Password if it's connected, and your computer's key store if not. Run it again at any time to see or change the choice.

**1Password (preferred).** Your keys sync to your other devices and sit in a vault you can see in the 1Password app.

1. Install the 1Password app and sign in.
2. In the app, open Settings, then Developer, and turn on "Integrate with 1Password CLI".
3. Run `keys setup 1password`. keywarden makes a vault called "Agent Keys". To use a different vault, run `keys setup 1password "Vault Name"`.

On a computer that runs without you at it, like a server, use a 1Password service account. Save its token with `keys add OP_SERVICE_ACCOUNT_TOKEN --clipboard`, then run `keys setup 1password`.

**Your computer's key store.** On Windows that's Credential Manager, and on a Mac it's the Keychain. Nothing to install. Run `keys setup local`. On Linux, use 1Password, because this option only works on Windows and Mac.

**Moving between them.** `keys switch 1password` copies every key into 1Password and uses it from then on. `keys switch local` goes the other way. Copying needs your approval. The old copies stay where they were until you delete them, so you can check the new store first.

**Strict mode (1Password only).** Normally, once you approve a key, keywarden keeps a copy in your computer's key store until the grant ends. `keys strict on` turns that off. Every time a command needs a key, keywarden reads it fresh from 1Password, and 1Password asks for your fingerprint or face. Turning strict mode off needs your approval.

## How you approve

- **In the chat (default).** Claude asks, and you reply approve, approve 1h, or no. Set it with `keys approvals chat`.
- **On your phone.** Run `keys phone` and open the link it prints on your phone. It works over Tailscale and approves with Face ID. Then `keys approvals phone` sends every request to your phone, or `keys protect NAME` does it for one key only.
- **No question.** `keys approvals off` gives a grant without asking. Changing to this needs your approval.

## Commands

```
keys list                       saved keys, with what each is and where it came from
keys add NAME --clipboard       save the key you copied
keys note NAME "what it is"     add a note that you and Claude see in the list
keys grant NAME --for "why"     give Claude a key for 12 hours
keys status                     which keys have a grant
keys lock [NAME]                end a grant now, or every grant
keys setup                      see or change where keys are stored
```
