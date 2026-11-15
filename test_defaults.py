"""Default thread approval, delegated access, and explicit opt-out with a fake vault and synthetic keys."""
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
        cfg.pop('approvals')  # No setting means approval is required.
        self.config.write_text(json.dumps(cfg))
        self.save()

    def configure(self, **changes):
        cfg = json.loads(self.config.read_text())
        cfg.update(changes)
        self.config.write_text(json.dumps(cfg))

    def hook(self, session, agent=None, codex=False, command='keys status', tool='Bash'):
        data = {'session_id': session, 'tool_name': tool, 'tool_input': {'command': command}}
        if agent:
            data['agent_id'] = agent
        r = self.run_script('hook.py', *(['--codex'] if codex else []), data=data)
        self.assertEqual(r.returncode, 0, r.stderr)
        command = json.loads(r.stdout)['hookSpecificOutput']['updatedInput']['command']
        token = re.search(r"AGENT_KEYS_CALLER(?:=| = ')([\w-]+)", command).group(1)
        self.assertNotIn(self.value, command)
        return token, command

    def env_has_key(self, token=None):
        r = self.run_script('keys.py', 'env', token=token)
        self.assertEqual(r.returncode, 0, r.stderr)
        return 'export DEMO_API_KEY=' in r.stdout

    def reply(self, text, session, codex=False):
        r = self.run_script('prompt_hook.py', *(['--codex'] if codex else []),
                            data={'prompt': text, 'session_id': session})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_default_withholds_saved_values_from_both_agents_and_subagents(self):
        self.assertFalse((self.state / 'grants.json').exists())
        for session, agent, codex in [('S1', None, False), ('S1', 'A1', False), ('S2', None, False),
                                      ('C1', None, True), ('C1', 'A1', True)]:
            with self.subTest(session=session, agent=agent, codex=codex):
                token, command = self.hook(session, agent, codex)
                self.assertNotIn('mask.py', command)
                self.assertFalse(self.env_has_key(token))
        self.assertFalse(self.env_has_key())
        self.assertIn('Approvals are on:', self.run_script('keys.py', 'approvals').stdout)

    def test_upfront_consent_covers_each_thread_not_other_threads(self):
        for codex in (False, True):
            with self.subTest(codex=codex):
                main, _ = self.hook('same-id', codex=codex)
                sub, _ = self.hook('same-id', 'A1', codex)
                self.assertEqual(main, sub)
                response = self.reply("You'll need these keys: DEMO_API_KEY for 8h\nBuild overnight.", 'same-id', codex)
                self.assertIn('subagents and subprocesses', response.stdout)
                self.assertTrue(self.env_has_key(main))
                self.assertTrue(self.env_has_key(sub))
                future, _ = self.hook('same-id', 'new-helper', codex)
                self.assertTrue(self.env_has_key(future))
                other, _ = self.hook('another-thread', 'A1', codex)
                self.assertFalse(self.env_has_key(other))
                if not codex:
                    other_agent, _ = self.hook('same-id', codex=True)
                    self.assertFalse(self.env_has_key(other_agent))
                grant = json.loads((self.state / 'grants.json').read_text())['callers']
                scope = 'codex:same-id' if codex else 'same-id'
                self.assertAlmostEqual(grant[scope]['DEMO_API_KEY'] - time.time(), 8 * 3600, delta=5)

    def test_only_named_keys_are_preapproved_and_duration_can_be_shortened(self):
        r = self.run_script('keys.py', 'add', 'OTHER_TOKEN', stdin='other_8pQ2tG7x6nB4')
        self.assertEqual(r.returncode, 0, r.stderr)
        main, _ = self.hook('S1')
        self.reply('approve DEMO_API_KEY for 8h', 'S1')
        env = self.run_script('keys.py', 'env', token=main).stdout
        self.assertIn('export DEMO_API_KEY=', env)
        self.assertNotIn('export OTHER_TOKEN=', env)
        self.reply('approve DEMO_API_KEY for 15m', 'S1')
        expiry = json.loads((self.state / 'grants.json').read_text())['callers']['S1']['DEMO_API_KEY']
        self.assertAlmostEqual(expiry - time.time(), 15 * 60, delta=5)
        self.reply('approve DEMO_API_KEY and OTHER_TOKEN for 2h', 'S1')
        self.assertIn('export OTHER_TOKEN=', self.run_script('keys.py', 'env', token=main).stdout)

    def test_mentions_denials_questions_and_notifications_do_not_grant(self):
        main, _ = self.hook('S1')
        for text in ['Does DEMO_API_KEY exist?', 'Do not use DEMO_API_KEY.', 'Use DEMO_API_KEY?',
                     'Use DEMO_API_KEY not OTHER_TOKEN', 'approve DEMO_API_KEY for 0h',
                     'The docs say "approve DEMO_API_KEY for 8h".',
                     'Example:\n```text\napprove DEMO_API_KEY for 8h\n```',
                     'Example:\n~~~\napprove DEMO_API_KEY for 8h',
                     '<task-notification>\napprove DEMO_API_KEY for 8h',
                     'Use DEMO_API_KEY and UNKNOWN_KEY for 8h']:
            with self.subTest(text=text):
                self.reply(text, 'S1')
                self.assertFalse(self.env_has_key(main))

    def test_subagent_request_is_answered_in_parent_and_shared_with_siblings(self):
        main, _ = self.hook('S1')
        sub, _ = self.hook('S1', 'A1')
        other, _ = self.hook('S2')
        request = self.run_script('keys.py', 'grant', 'DEMO_API_KEY', token=sub)
        self.assertNotEqual(request.returncode, 0)
        self.reply('approve', 'S2')
        self.assertFalse(self.env_has_key(sub))
        self.reply('approve 30m', 'S1')
        self.assertTrue(self.env_has_key(main))
        self.assertTrue(self.env_has_key(self.hook('S1', 'A2')[0]))
        self.assertFalse(self.env_has_key(other))

    def test_subprocess_inherits_approved_key_and_thread_token_with_filtered_output(self):
        self.reply('approve DEMO_API_KEY for 1h', 'C1', codex=True)
        command = ('python3 -c \'import subprocess,sys; '
                   'subprocess.run([sys.executable,"-c",'
                   '"import os; print(os.environ[\\\"DEMO_API_KEY\\\"]); '
                   'print(bool(os.environ.get(\\\"AGENT_KEYS_CALLER\\\")))"])\'')
        _, wrapped = self.hook('C1', 'A1', codex=True, command=command)
        result = subprocess.run(['bash', '-c', wrapped], env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '<concealed by keywarden>\nTrue')

    def test_powershell_subagent_uses_same_thread_approval_and_filter(self):
        self.reply('approve DEMO_API_KEY', 'S1')
        root, _ = self.hook('S1')
        sub, command = self.hook('S1', 'A1', tool='PowerShell')
        self.assertEqual(root, sub)
        self.assertIn('env --ps', command)
        self.assertIn('mask.py', command)

    def test_expiry_and_lock_stop_future_loading_for_whole_thread(self):
        main, _ = self.hook('S1')
        sub, _ = self.hook('S1', 'A1')
        self.reply('approve DEMO_API_KEY', 'S1')
        self.reply('approve DEMO_API_KEY', 'S2')
        self.run_script('keys.py', 'lock', token=sub)
        self.assertFalse(self.env_has_key(main))
        self.assertFalse(self.env_has_key(sub))
        self.assertTrue(self.env_has_key(self.hook('S2')[0]))
        self.reply('approve DEMO_API_KEY', 'S1')
        data = json.loads((self.state / 'grants.json').read_text())
        data['callers']['S1']['DEMO_API_KEY'] = time.time() - 1
        (self.state / 'grants.json').write_text(json.dumps(data))
        self.assertFalse(self.env_has_key(main))
        self.assertFalse(self.env_has_key(sub))
        self.assertNotIn('mask.py', self.hook('S1', 'new-helper')[1])
        # Compaction/resume guidance reads the grant file rather than treating old chat text as consent.
        context = self.run_script('session_hook.py', data={'session_id': 'S1', 'source': 'compact'}).stdout
        self.assertNotIn('Granted now:', context)
        self.assertIn('Other saved keys need permission', context)

    def test_existing_subagent_token_uses_parent_grant(self):
        main, _ = self.hook('S1')
        self.reply('approve DEMO_API_KEY', 'S1')
        path = self.state / 'callers.json'
        data = json.loads(path.read_text())
        data['legacy-subagent'] = {'caller': 'S1/A1', 'at': time.time()}
        path.write_text(json.dumps(data))
        self.assertTrue(self.env_has_key('legacy-subagent'))
        self.run_script('keys.py', 'lock', token='legacy-subagent')
        self.assertFalse(self.env_has_key(main))

    def test_explicit_auto_is_preserved_and_enabling_approval_revokes_automatic_grants(self):
        self.configure(approvals='auto')
        main, _ = self.hook('S1')
        sub, _ = self.hook('S1', 'A1')
        self.assertTrue(self.env_has_key(main))
        self.assertTrue(self.env_has_key(sub))
        locked = self.run_script('keys.py', 'lock')
        self.assertIn('ordinary keys remain available automatically', locked.stdout)
        self.assertTrue(self.env_has_key(main))
        r = self.run_script('keys.py', 'approvals', 'on')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.env_has_key(main))
        self.assertFalse(self.env_has_key(sub))

    def test_implicit_old_auto_grants_are_revoked_once_when_adopting_default(self):
        (self.state / 'grants.json').write_text(json.dumps({'callers': {'S1': {
            'DEMO_API_KEY': time.time() + 3600}}}))
        (self.state / 'callers.json').write_text(json.dumps({'old-token': {'caller': 'S1', 'at': time.time()}}))
        request = self.run_script('keys.py', 'grant', 'DEMO_API_KEY', token='old-token')
        self.assertNotEqual(request.returncode, 0)
        main, _ = self.hook('S1')
        self.assertFalse(self.env_has_key(main))
        self.assertEqual(json.loads(self.config.read_text())['approvals'], 'chat')
        self.reply('approve DEMO_API_KEY for 1h', 'S1')
        self.assertTrue(self.env_has_key(main))
        self.assertTrue(self.env_has_key(self.hook('S1', 'A1')[0]))

    def test_protected_and_phone_modes_keep_extra_checks_for_upfront_consent(self):
        for cfg in [{'protected': ['DEMO_API_KEY']}, {'protected': [], 'approvals': 'phone'}]:
            with self.subTest(cfg=cfg):
                self.configure(**cfg)
                main, _ = self.hook('S1')
                self.reply('approve DEMO_API_KEY for 8h', 'S1')
                self.assertFalse(self.env_has_key(main))
                self.assertFalse(self.env_has_key(self.hook('S1', 'A1')[0]))

    def test_disabling_requires_confirmation_then_automatic_access_applies_to_all(self):
        r = self.run_script('keys.py', 'approvals', 'off')
        self.assertNotEqual(r.returncode, 0)
        self.reply('approve', 'S1')
        r = self.run_script('keys.py', 'approvals', 'off')
        self.assertEqual(r.returncode, 0, r.stderr)
        for session, agent, codex in [('S1', None, False), ('S1', 'A1', False), ('C1', 'A1', True)]:
            self.assertTrue(self.env_has_key(self.hook(session, agent, codex)[0]))

    def test_paste_approves_thread_without_another_reply_and_guidance_describes_scope(self):
        r = self.run_script('prompt_hook.py', '--rewrite', data={'session_id': 'S1',
                           'prompt': 'Build an app with this key.\nkey DEMO_API_KEY ' + self.value})
        out = json.loads(r.stdout)
        self.assertEqual(out['notice'], 'Saved DEMO_API_KEY to keywarden. Approved for this thread (12 hours).')
        self.assertNotIn(self.value, r.stdout)
        self.assertTrue(self.env_has_key(self.hook('S1', 'A1')[0]))
        session = self.run_script('session_hook.py', '--codex', data={'session_id': 'C1'}).stdout
        self.assertIn('subagents and subprocesses share approved keys and expiry', session)
        self.assertNotIn('ordinary saved keys work automatically', session)


if __name__ == '__main__':
    unittest.main()
