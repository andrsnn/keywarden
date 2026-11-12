import type { Register } from 'claude-code'

// When you paste a key, keywarden saves it and swaps it for [NAME] in your message, and the message goes on to
// Claude without the key. This runs before Claude Code's own UserPromptSubmit hooks, so keywarden's
// prompt_hook.py then sees no key. If this mod fails for any reason, prompt_hook.py still stops a message
// that has a key in it.
export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    if (e.command || e.isMeta || !e.text.trim()) return next(e)
    const root = $.plugin.root
    let python = 'python'
    try {
      python = (await $.fs.read(`${root}/hooks/python-path.txt`)).trim() || python // written by install.py
    } catch {}
    const run = await $.process.run([python, `${root}/prompt_hook.py`, '--rewrite'], {
      stdin: JSON.stringify({ prompt: e.text, cwd: await $.session.cwd() }),
      timeoutMs: 20000,
    })
    if (run.exitCode !== 0) return next(e)
    const out = JSON.parse(run.stdout) as { text?: string; saved?: [string, string][]; drop?: string }
    if (out.drop) return { drop: out.drop }
    if (!out.saved?.length || out.text === undefined) return next(e)
    const names = out.saved.map(([name]) => name)
    $.ui.toast(`keywarden saved your key as ${names.join(', ')}`)
    const note =
      `keywarden: the person pasted a key, and keywarden saved it as ${names.join(', ')} before you saw it. ` +
      `In their message it shows as [${names[0]}]. It's saved, so don't ask them for it again. ` +
      `To use it, run keys grant ${names[0]} --for "why".`
    return next({ ...e, text: out.text, context: [...(e.context ?? []), note] })
  })
}
