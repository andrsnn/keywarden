"""Native CLI smoke tests with synthetic keys and a loopback model stub; no real API calls.

Skips a CLI when it is absent. KEYWARDEN_CODEX_CLI can point to an otherwise undiscoverable Codex binary.
"""
import gzip
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import install
from test_integrations import CodexFlowTests

HERE = Path(__file__).resolve().parent
BUNDLED_CODEX = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex')


class ModelStub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.headers.get('Content-Encoding') == 'gzip':
            body = gzip.decompress(body)
        self.server.requests.append(body)
        data = json.loads(body)
        self.send_response(200)
        if 'count_tokens' in self.path:
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"input_tokens":1}')
            return
        # Minimal Anthropic stream, used only after Claude's plugin has sanitized the prompt.
        msg = {'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': data.get('model', 'test'),
               'content': [], 'stop_reason': None, 'stop_sequence': None,
               'usage': {'input_tokens': 1, 'output_tokens': 0}}
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        events = [
            {'type': 'message_start', 'message': msg},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'text_delta', 'text': 'Synthetic API response.'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
             'usage': {'output_tokens': 1}},
            {'type': 'message_stop'},
        ]
        for event in events:
            self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()


def main():
    claude = shutil.which('claude')
    codex = os.environ.get('KEYWARDEN_CODEX_CLI') or shutil.which('codex')
    if not codex and BUNDLED_CODEX.exists():
        codex = str(BUNDLED_CODEX)
    if not claude and not codex:
        print('SKIP native CLI tests: neither Claude Code nor Codex is installed')
        return
    case = CodexFlowTests()
    case.setUp()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ModelStub)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        endpoint = f'http://127.0.0.1:{server.server_port}'
        env = dict(case.env, CLAUDE_CONFIG_DIR=str(case.root / 'claude'),
                   ANTHROPIC_API_KEY='synthetic-test-key', ANTHROPIC_BASE_URL=endpoint,
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CODEX_HOME=str(case.root / 'codex'),
                   OPENAI_API_KEY='synthetic-test-key', OPENAI_BASE_URL=endpoint + '/v1')
        prompt = 'key DEMO_API_KEY ' + case.value
        if claude:
            plugin = case.root / 'plugin'
            shutil.copytree(HERE, plugin, ignore=shutil.ignore_patterns('.git', '__pycache__'))
            (plugin / 'hooks/python-path.txt').write_text(sys.executable)
            settings = {'hooks': {'UserPromptSubmit': [{'hooks': [{
                'type': 'command', 'command': f'"{sys.executable}" "{plugin / "prompt_hook.py"}"'}]}]}}
            base = [claude, '-p', '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config',
                    '--settings', json.dumps(settings)]
            result = subprocess.run([*base, '--plugin-dir', str(plugin), prompt], env=env, cwd=case.root,
                                    input='', capture_output=True, text=True, timeout=45)
            assert result.returncode == 0, result.stderr
            assert 'Synthetic API response.' in result.stdout, result.stdout
            assert server.requests, 'Claude did not forward the sanitized prompt'
            assert not any(case.value.encode() in body for body in server.requests), 'Value reached model stub'
            assert any(b'[DEMO_API_KEY]' in body for body in server.requests), 'No saved-name reference'
            db = json.loads((case.root / 'op.json').read_text())
            assert db['vaults']['Agent Keys']['DEMO_API_KEY'] == case.value
            print('PASS native Claude plugin: saves the key, forwards its name, keeps the value out of model requests', flush=True)
            server.requests.clear()
            result = subprocess.run([*base, prompt], env=env, cwd=case.root, input='',
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            assert 'UserPromptSubmit operation blocked by hook:' in result.stdout, result.stdout
            assert not server.requests, 'Blocked Claude prompt reached the model stub'
            assert case.value not in result.stdout + result.stderr
            print('PASS native Claude fallback: saves/blocks a pasted key before a model request', flush=True)
        else:
            print('SKIP native Claude tests: CLI not installed', flush=True)
        if codex:
            home = Path(env['CODEX_HOME'])
            home.mkdir()
            (home / 'config.toml').write_text(
                'model_provider = "keywarden_test"\n[model_providers.keywarden_test]\n'
                f'name = "Keywarden Test"\nbase_url = "{endpoint}/v1"\n'
                'wire_api = "responses"\nrequires_openai_auth = false\n')
            with patch.object(install, 'CODEX_HOOKS', home / 'hooks.json'), \
                    patch.object(install, 'CODEX_SKILL', case.root / 'agents/skills/keywarden'):
                install.install_codex()
            # Trust bypass is confined to this temporary test configuration and these vetted hook scripts.
            result = subprocess.run([codex, '--no-daemon', 'exec', '--json', '--ephemeral',
                                     '--skip-git-repo-check', '--dangerously-bypass-hook-trust',
                                     '-C', str(case.root), prompt], env=env, cwd=case.root, input='',
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            completed = [event for event in events if event.get('type') == 'turn.completed']
            assert completed and completed[0]['usage']['input_tokens'] == 0, result.stdout
            assert not server.requests, 'Blocked Codex prompt reached the model stub'
            assert (case.state / 'meta.json').exists(), 'Codex did not save the key'
            assert case.value not in result.stdout + result.stderr
            print('PASS native Codex: installed prompt hook saves/blocks a pasted key with zero model tokens', flush=True)
        else:
            print('SKIP native Codex test: CLI not installed', flush=True)
    finally:
        server.shutdown()
        server.server_close()
        case.doCleanups()


if __name__ == '__main__':
    main()
