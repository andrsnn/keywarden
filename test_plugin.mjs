import assert from 'node:assert/strict'
import fs from 'node:fs'
import { captureCommands, register, replaceCapturedKeys } from './hooks/register.ts'

const fixtures = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
let passed = 0
const check = (name, fn) => { fn(); passed++; console.log(`PASS ${name}`) }

for (const fixture of fixtures) {
  check(`literal replacement: ${fixture.label}`, () => {
    assert.equal(replaceCapturedKeys(fixture.prompt, fixture.capture.edits), fixture.expected)
    assert.equal(fixture.capture.text, fixture.expected)
  })
}
check('rejects overlapping or out-of-bounds replacement ranges', () => {
  assert.throws(() => replaceCapturedKeys('abcdef', [{ start: 0, end: 4, name: 'A' }, { start: 3, end: 6, name: 'B' }]))
  assert.throws(() => replaceCapturedKeys('abcdef', [{ start: -1, end: 4, name: 'A' }]))
  assert.throws(() => replaceCapturedKeys('abcdef', [{ start: 0, end: 7, name: 'A' }]))
})
check('chooses Windows shells for Windows roots, and Bash for POSIX roots', () => {
  const windows = captureCommands('C:\\A path\\keywarden').map(c => c[0])
  assert(windows.includes('pwsh') && windows.includes('powershell') && windows.includes('py'))
  assert(!windows.includes('bash'))
  const mac = captureCommands('/A path/keywarden').map(c => c[0])
  assert(mac.includes('bash') && !mac.includes('powershell'))
})
check('keeps values on stdin, with UTF-8 and no shell interpolation of the prompt', () => {
  for (const root of ['/A path/keywarden', 'C:\\A path\\keywarden']) {
    const commands = captureCommands(root, '/A path/python')
    assert(commands.every(c => !c.join(' ').includes(fixtures[0].prompt)))
    const powershell = commands.find(c => c[0] === 'powershell')
    if (powershell) {
      assert(powershell.includes('-NoProfile'))
      assert(powershell.at(-1).includes('[Console]::In.ReadToEnd()'))
      assert(powershell.at(-1).includes('PYTHONUTF8'))
    }
  }
})

async function runPlugin({ root = '/plugin', works, response, hint = '', event }) {
  let handler, forwarded, dropped
  const commands = [], toasts = []
  register((name, fn) => { assert.equal(name, 'prompt.submit'); handler = fn })
  const fixture = fixtures[0]
  const $ = {
    plugin: { root }, fs: { read: async () => hint },
    session: { cwd: async () => '/work', id: async () => 'test-session' },
    ui: { toast: text => toasts.push(text) },
    process: { run: async (command, options) => {
      commands.push(command)
      const input = JSON.parse(options.stdin)
      assert.equal(input.prompt, fixture.prompt)
      assert.equal(input.session_id, 'test-session')
      if (!works(command)) throw new Error('Launcher unavailable')
      return { exitCode: 0, stdout: JSON.stringify(response ?? fixture.capture) }
    } },
  }
  const inputEvent = event ?? { text: fixture.prompt }
  dropped = await handler($, inputEvent, async e => { forwarded = e; return e })
  return { commands, toasts, forwarded, dropped }
}

for (const [name, root, executable] of [
  ['installed CLI', '/plugin', 'keys'],
  ['Bash launcher on macOS/Linux', '/plugin', 'bash'],
  ['Zsh launcher', '/plugin', 'zsh'],
  ['POSIX sh launcher', '/plugin', 'sh'],
  ['PowerShell 7 on Windows', 'C:\\plugin', 'pwsh'],
  ['Windows PowerShell fallback', 'C:\\plugin', 'powershell'],
  ['Windows command shell fallback', 'C:\\plugin', 'cmd.exe'],
  ['configured Python with spaces', '/plugin', '/A path/python'],
  ['Python 3 after stale hint', '/plugin', 'python3'],
  ['Windows py launcher', 'C:\\plugin', 'py'],
]) {
  const result = await runPlugin({ root, hint: '/A path/python', works: c => c[0] === executable })
  assert.equal(result.forwarded.text, fixtures[0].expected)
  assert.equal(result.toasts[0], fixtures[0].capture.notice)
  assert.equal(result.commands.at(-1)[0], executable)
  passed++; console.log(`PASS plugin capture through ${name}`)
}
for (const [name, response] of [
  ['replacement mismatch', { ...fixtures[0].capture, text: 'wrong output' }],
  ['storage failure', { drop: 'Could not save this key.' }],
]) {
  const result = await runPlugin({ works: () => true, response })
  assert.equal(result.forwarded, undefined)
  assert(result.dropped.drop)
  passed++; console.log(`PASS blocks original prompt on ${name}`)
}
const unavailable = await runPlugin({ works: () => false })
assert.equal(unavailable.forwarded, undefined)
assert(unavailable.dropped.drop.includes('Rerun install.py'))
passed++; console.log('PASS blocks original prompt when no backend launcher works')
console.log(`${passed}/${passed} plugin checks passed`)
