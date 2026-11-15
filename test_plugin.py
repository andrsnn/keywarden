"""Run the actual TypeScript plugin with Node's built-in type stripping and real backend capture plans."""
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import test_integrations as fixtures


def main():
    node = shutil.which('node')
    if not node:
        print('SKIP plugin unit tests: Node.js 22.6+ is not installed (native Claude tests still exercise the plugin)')
        return
    version = subprocess.run([node, '--version'], capture_output=True, text=True, check=True).stdout.strip().lstrip('v')
    if tuple(int(part) for part in version.split('.')[:2]) < (22, 6):
        print('SKIP plugin unit tests: Node.js 22.6+ is required for built-in TypeScript stripping')
        return
    case = fixtures.CodexFlowTests()
    case.setUp()
    try:
        value = case.value
        cases = [
            ('repeated value with emoji prefix', f'🔐 Build it.\nkey DEMO_API_KEY {value}\nRepeat: {value}',
             '🔐 Build it.\nkey DEMO_API_KEY [DEMO_API_KEY]\nRepeat: [DEMO_API_KEY]'),
            ('literal metacharacters', 'key LITERAL_KEY a$b\\c.*?12xY', 'key LITERAL_KEY [LITERAL_KEY]'),
            ('multiline private key', '🚀\nkey PRIVATE_KEY\n-----BEGIN PRIVATE KEY-----\nQm7L9p2R8x4T6v3B\n-----END PRIVATE KEY-----',
             '🚀\nkey PRIVATE_KEY\n[PRIVATE_KEY]'),
            ('multiple keys', f'key FIRST_KEY {value}\nkey SECOND_KEY other_8pQ2tG7x6nB4',
             'key FIRST_KEY [DEMO_API_KEY]\nkey SECOND_KEY [SECOND_KEY]'),
        ]
        output = []
        for label, prompt, expected in cases:
            result = case.run_script('keys.py', '_capture', data={'prompt': prompt, 'session_id': 'test-session'})
            if result.returncode:
                raise RuntimeError(f'Capture failed: {label}: {result.stderr}')
            capture = json.loads(result.stdout)
            assert capture.get('text') == expected, f'Unexpected capture output: {label}'
            assert not any(secret in result.stdout for secret in [value, 'a$b\\c.*?12xY', 'Qm7L9p2R8x4T6v3B'])
            output.append({'label': label, 'prompt': prompt, 'expected': expected, 'capture': capture})
        data = case.root / 'captures.json'
        data.write_text(json.dumps(output))
        if shutil.which('bash'):
            home = case.root / 'Home with spaces'
            command = home / '.local/bin/keys'
            command.parent.mkdir(parents=True)
            repo = Path(__file__).resolve().parent
            command.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(repo / "keys.py"))} "$@"\n')
            command.chmod(0o755)
            result = subprocess.run(['bash', '-c', 'export PYTHONUTF8=1; exec "$HOME/.local/bin/keys" _capture'],
                                    env=dict(case.env, HOME=str(home)), input=json.dumps({'prompt': cases[0][1]}),
                                    capture_output=True, text=True, timeout=20, check=True)
            assert json.loads(result.stdout)['text'] == cases[0][2]
            print('PASS actual Bash launcher with a spaced home path and Unicode input', flush=True)
        subprocess.run([node, '--experimental-strip-types', str(Path(__file__).with_suffix('.mjs')), str(data)], check=True)
    finally:
        case.doCleanups()


if __name__ == '__main__':
    main()
