"""Default automatic access and opt-in approval/session binding, using a fake vault and synthetic keys."""
import json
import re
import subprocess
import time
import unittest

import test_integrations as fixtures


class AccessModeTests(unittest.TestCase):
    run_script = fixtures.CodexFlowTests.run_script
    save = fixtures.CodexFlowTests.save

    def setUp(self):
        fixtures.CodexFlowTests.setUp(self)
        self.config = self.state / 'config.json'
        cfg = json.loads(self.config.read_text())
        cfg.pop('approvals')  # New installs must work without a setting or an approval reply.
        self.config.write_text(json.dumps(cfg))
        self.save()

    def hook(self, session, agent=None, codex=False):
        data = {'session_id': session, 'tool_name': 'Bash', 'tool_input': {'command': 'keys status'}}
        if agent:
            data['agent_id'] = agent
        r = self.run_script('hook.py', *(['--codex'] if codex else []), data=data)
        self.assertEqual(r.returncode, 0, r.stderr)
        command = json.loads(r.stdout)['hookSpecificOutput']['updatedInput']['command']
        token = re.search(r'AGENT_KEYS_CALLER=([\w-]+)', command).group(1)
        self.assertNotIn(self.value, command)
        return token, command

    def env_has_key(self, token=None):
        r = self.run_script('keys.py', 'env', token=token)
        self.assertEqual(r.returncode, 0, r.stderr)
        return 'export DEMO_API_KEY=' in r.stdout

    def reply(self, text, session):
        return self.run_script('prompt_hook.py', data={'prompt': text, 'session_id': session})

    def test_new_install_works_for_both_agents_and_their_subagents(self):
        self.assertFalse((self.state / 'grants.json').exists())
        for session, agent, codex in [('S1', None, False), ('S1', 'A1', False), ('S2', None, False),
                                      ('C1', None, True), ('C1', 'A1', True)]:
            with self.subTest(session=session, agent=agent, codex=codex):
                token, command = self.hook(session, agent, codex)
                self.assertIn('mask.py', command)
                self.assertTrue(self.env_has_key(token))
        self.assertTrue(self.env_has_key())  # Explicit env loading from the developer's terminal works too.
        self.assertFalse((self.state / 'pending.json').exists())
        status = self.run_script('keys.py', 'status').stdout
        self.assertIn('session binding are off', status)
        self.assertIn('Available without approval: DEMO_API_KEY', status)

    def test_hook_automatically_injects_and_masks_a_stored_key(self):
        data = {'session_id': 'C1', 'agent_id': 'A1', 'tool_name': 'Bash', 'tool_input': {
            'command': 'python3 -c \'import os; print(os.environ["DEMO_API_KEY"])\''}}
        r = self.run_script('hook.py', '--codex', data=data)
        out = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'allow')
        result = subprocess.run(['bash', '-c', out['updatedInput']['command']], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '<concealed by keywarden>')
        self.assertFalse((self.state / 'pending.json').exists())

    def test_automatic_access_does_not_expire_or_pretend_lock_revokes_it(self):
        token, _ = self.hook('S1')
        grant_file = self.state / 'grants.json'
        data = json.loads(grant_file.read_text())
        for grant in data['callers'].values():
            grant['DEMO_API_KEY'] = time.time() - 1
        grant_file.write_text(json.dumps(data))
        self.assertTrue(self.env_has_key(token))
        locked = self.run_script('keys.py', 'lock')
        self.assertIn('ordinary keys remain available automatically', locked.stdout)
        self.assertTrue(self.env_has_key(token))

    def test_enabling_control_revokes_automatic_grants_and_binds_subagents(self):
        main, _ = self.hook('S1')
        sub, _ = self.hook('S1', 'A1')
        other, _ = self.hook('C1', codex=True)
        r = self.run_script('keys.py', 'approvals', 'on')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.config.read_text())['approvals'], 'chat')
        for token in (main, sub, other):
            self.assertFalse(self.env_has_key(token))
        grant = self.run_script('keys.py', 'grant', 'DEMO_API_KEY', token=main)
        self.assertNotEqual(grant.returncode, 0)
        self.reply('approve', 'S1')
        self.assertTrue(self.env_has_key(main))
        self.assertFalse(self.env_has_key(sub))
        self.assertFalse(self.env_has_key(other))
        self.run_script('keys.py', 'grant', 'DEMO_API_KEY', token=sub)
        self.reply('approve', 'unrelated-session')
        self.assertFalse(self.env_has_key(sub))
        self.reply('approve 30m', 'S1')
        self.assertTrue(self.env_has_key(sub))
        self.run_script('keys.py', 'lock', token=main)
        self.assertFalse(self.env_has_key(main))
        self.assertTrue(self.env_has_key(sub))

    def test_protected_key_stays_protected_with_controls_off(self):
        cfg = json.loads(self.config.read_text())
        cfg['protected'] = ['DEMO_API_KEY']
        self.config.write_text(json.dumps(cfg))
        main, command = self.hook('S1')
        sub, _ = self.hook('S1', 'A1')
        self.assertNotIn('mask.py', command)
        self.assertFalse(self.env_has_key(main))
        self.assertFalse(self.env_has_key(sub))
        capture = self.run_script('prompt_hook.py', data={'session_id': 'S1', 'prompt': 'key DEMO_API_KEY ' + self.value})
        out = json.loads(capture.stdout)
        self.assertNotIn('Ready for', out['systemMessage'])
        self.assertFalse(self.env_has_key(main))
        (self.state / 'grants.json').write_text(json.dumps({'callers': {'S1': {'DEMO_API_KEY': time.time() + 60}}}))
        self.assertTrue(self.env_has_key(main))
        self.assertFalse(self.env_has_key(sub))

    def test_disabling_enabled_control_requires_confirmation_then_applies_to_all(self):
        self.run_script('keys.py', 'approvals', 'on')
        r = self.run_script('keys.py', 'approvals', 'off')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(self.config.read_text())['approvals'], 'chat')
        self.reply('approve', 'S1')
        r = self.run_script('keys.py', 'approvals', 'off')
        self.assertEqual(r.returncode, 0, r.stderr)
        for session, agent, codex in [('S1', None, False), ('S1', 'A1', False), ('C1', 'A1', True)]:
            token, _ = self.hook(session, agent, codex)
            self.assertTrue(self.env_has_key(token))

    def test_default_capture_and_session_guidance_do_not_request_approval(self):
        r = self.run_script('prompt_hook.py', '--rewrite', data={'session_id': 'S1',
                           'prompt': 'Build an app with this key.\nkey DEMO_API_KEY ' + self.value})
        out = json.loads(r.stdout)
        self.assertEqual(out['notice'], 'Saved as DEMO_API_KEY. Ready for Claude Code, Codex, and their subagents.')
        self.assertNotIn(self.value, r.stdout)
        session = self.run_script('session_hook.py', '--codex', data={'session_id': 'C1'}).stdout
        self.assertIn('ordinary saved keys work automatically', session)
        self.assertNotIn('a subagent you start asks for its own', session)


if __name__ == '__main__':
    unittest.main()
