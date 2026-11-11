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
- **A pasted key never reaches Claude.** keywarden adds a hook to Claude Code that reads each message before Claude does. When it finds a key, it saves the key and stops the message. So the key never lands in Claude's chat log on your disk, and it's never sent to the model provider.
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

Keys go in 1Password if its app is connected. Otherwise they go in Windows Credential Manager or the Mac Keychain.

## Commands

```
keys list                       names of saved keys
keys add NAME --clipboard       save the key you copied
keys grant NAME --for "why"     give Claude a key for 12 hours
keys status                     which keys have a grant
keys lock                       end every grant now
```
