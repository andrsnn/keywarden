import type { Register } from 'claude-code'

type Edit = { start: number; end: number; name: string }
type Capture = {
  text?: string; edits?: Edit[]; saved?: [string, string][]; drop?: string; notice?: string; context?: string
}

// Replacement uses the JavaScript runtime that is already running this plugin. The storage backend returns
// ranges and names, never copies of secret values. Slicing treats values literally, including $ and backslashes.
export function replaceCapturedKeys(text: string, edits: Edit[]): string {
  let boundary = text.length
  for (const edit of [...edits].sort((a, b) => b.start - a.start)) {
    if (!Number.isInteger(edit.start) || !Number.isInteger(edit.end) || edit.start < 0 ||
        edit.start >= edit.end || edit.end > boundary || !/^[A-Z][A-Z0-9_]*$/.test(edit.name)) {
      throw new Error('Invalid key replacement range')
    }
    text = text.slice(0, edit.start) + `[${edit.name}]` + text.slice(edit.end)
    boundary = edit.start
  }
  return text
}

export function captureCommands(root: string, python = ''): string[][] {
  const windows = /^[A-Za-z]:[\\/]|^\\\\/.test(root)
  const script = `${root}/prompt_hook.py`
  const powershell = "$env:PYTHONUTF8='1'; $OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " +
    "$payload=[Console]::In.ReadToEnd(); $entry=Join-Path $HOME '.local/bin/keys.cmd'; " +
    "$payload | & $entry _capture; exit $LASTEXITCODE"
  const posix = 'export PYTHONUTF8=1; exec "$HOME/.local/bin/keys" _capture'
  const shells = windows
    ? [['pwsh', '-NoProfile', '-NonInteractive', '-Command', powershell],
       ['powershell', '-NoProfile', '-NonInteractive', '-Command', powershell],
       ['cmd.exe', '/d', '/s', '/c', 'set "PYTHONUTF8=1" && call "%USERPROFILE%\\.local\\bin\\keys.cmd" _capture']]
    : ['bash', 'zsh', 'sh'].map(shell => [shell, '-c', posix])
  return [
    ['keys', '_capture'],
    ...shells,
    ...(python ? [[python, '-X', 'utf8', script, '--rewrite']] : []),
    ...(windows ? [['py', '-3', '-X', 'utf8', script, '--rewrite']] : []),
    ['python3', '-X', 'utf8', script, '--rewrite'],
    ['python', '-X', 'utf8', script, '--rewrite'],
  ]
}

export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    if (e.command || e.isMeta || !e.text.trim()) return next(e)
    const root = $.plugin.root
    let python = ''
    try {
      python = (await $.fs.read(`${root}/hooks/python-path.txt`)).trim()
    } catch {}
    const input = JSON.stringify({ prompt: e.text, cwd: await $.session.cwd(), session_id: await $.session.id() })
    let out: Capture | undefined
    for (const command of captureCommands(root, python)) {
      try {
        const run = await $.process.run(command, { stdin: input, timeoutMs: 20000 })
        if (run.exitCode !== 0) continue
        const parsed = JSON.parse(run.stdout) as Capture
        if (parsed.drop || (typeof parsed.text === 'string' && Array.isArray(parsed.saved) &&
            (!parsed.saved.length || Array.isArray(parsed.edits)))) {
          out = parsed
          break
        }
      } catch {}
    }
    if (!out) return { drop: 'keywarden could not save this message safely. Rerun install.py and restart Claude.' }
    if (out.drop) return { drop: out.drop }
    if (!out.saved?.length) return next(e)
    let text: string
    try {
      text = replaceCapturedKeys(e.text, out.edits!)
      if (text !== out.text) throw new Error('Replacement mismatch')
    } catch {
      return { drop: 'Your key was saved, but the message could not be updated safely. Resend using the saved key name.' }
    }
    if (out.notice) $.ui.toast(out.notice)
    return next({ ...e, text, context: [...(e.context ?? []), ...(out.context ? [out.context] : [])] })
  })
}
