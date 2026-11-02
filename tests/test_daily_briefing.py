from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import daily_briefing
from etrade_auth import ET, update_env_tokens


def check_refreshed_credentials(tmp_path):
    env_path = tmp_path / '.env'
    update_env_tokens(env_path, 'old-token', 'old-secret', auth_date='2026-10-21')
    launched = []

    def run(command):
        launched.append(Path(command[1]).name)
        if launched[-1] == 'etrade_auth.py':
            update_env_tokens(env_path, 'new-token', 'new-secret', auth_date='2026-10-22')
        else:
            assert os.environ['ETRADE_AUTH_DATE'] == '2026-10-22'
            assert os.environ['ETRADE_ACCESS_TOKEN'] == 'new-token'
            assert os.environ['ETRADE_ACCESS_TOKEN_SECRET'] == 'new-secret'
        return subprocess.CompletedProcess(command, 0)

    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(daily_briefing, 'ENV_PATH', env_path),
        patch.object(daily_briefing, 'datetime') as clock,
        patch.object(daily_briefing.subprocess, 'run', side_effect=run),
    ):
        clock.now.return_value = datetime(2026, 10, 22, 9, tzinfo=ET)
        assert daily_briefing.main(['--no-wait']) == 0
    assert launched == ['etrade_auth.py', 'build_briefing_prompt.py']


class DailyBriefingTests(unittest.TestCase):
    def test_build_inherits_refreshed_credentials_after_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            check_refreshed_credentials(Path(directory))

    def test_reply_identity(self):
        prompt = 'Prompt\n```json\n{"briefing_id": "today"}\n```'
        for text, expected in [
            (prompt + '\n', False),
            ('```json\n{"briefing_id": "today"}\n```', True),
            ('{"briefing_id": "yesterday"}', False),
            ('No JSON here', False),
            ('```json\ninvalid\n```', False),
            ('```json\n[]\n```', False),
        ]:
            with self.subTest(text=text):
                self.assertEqual(daily_briefing.is_reply_for(text, 'today', prompt), expected)

    def run_wait(self, clipboard, engine='chatgpt', timeout=60, browser_error=None):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'briefings').mkdir()
            (project / 'prompts').mkdir()
            (project / 'briefings/briefing_latest.json').write_text(
                '{"briefing_id": "today"}', encoding='utf-8'
            )
            prompt = 'Prompt {"briefing_id": "today"}'
            (project / 'prompts/daily_briefing_prompt_latest.md').write_text(
                prompt, encoding='utf-8'
            )
            with (
                patch.object(daily_briefing, 'ENV_PATH', project / '.env'),
                patch.object(daily_briefing, 'read_clipboard_text', side_effect=clipboard),
                patch.object(daily_briefing.time, 'sleep') as sleep,
                patch.object(daily_briefing.subprocess, 'run') as run,
                patch.object(
                    daily_briefing.webbrowser, 'open', side_effect=browser_error
                ) as browser,
                patch('builtins.print') as output,
            ):
                run.return_value.returncode = 7
                result = daily_briefing.wait_for_reply(engine, timeout)
                return result, run, sleep, browser, output, project

    def test_wait_imports_matching_reply_once(self):
        result, run, sleep, browser, output, project = self.run_wait([
            'Prompt {"briefing_id": "today"}',
            RuntimeError('clipboard busy'),
            '{"briefing_id": "yesterday"}',
            '{"briefing_id": "yesterday"}',
            '{"briefing_id": "today"}',
        ])
        self.assertEqual(result, 7)
        run.assert_called_once_with([
            daily_briefing.sys.executable, str(project / 'import_response.py'),
            '--engine', 'chatgpt', '--from-clipboard', '--briefing-id', 'today',
        ])
        self.assertEqual(sleep.call_count, 4)
        sleep.assert_called_with(2)
        browser.assert_called_once_with('https://chatgpt.com/')
        self.assertEqual(
            output.call_args_list.count(unittest.mock.call(
                'Ignoring a reply for a different briefing_id.'
            )), 1,
        )

    def test_keyboard_interrupt_does_not_import(self):
        result, run, *_ = self.run_wait([KeyboardInterrupt()])
        self.assertEqual(result, 0)
        run.assert_not_called()

    def test_timeout_does_not_import_or_open_other_engine(self):
        result, run, sleep, browser, *_ = self.run_wait([], engine='claude', timeout=0)
        self.assertEqual(result, 1)
        run.assert_not_called()
        sleep.assert_not_called()
        browser.assert_not_called()

    def test_browser_failure_does_not_stop_wait(self):
        result, run, *_ = self.run_wait(
            ['{"briefing_id": "today"}'], browser_error=OSError('no browser')
        )
        self.assertEqual(result, 7)
        run.assert_called_once()

    def test_no_wait_does_not_poll(self):
        with (
            patch.object(daily_briefing, 'authorization_needed', return_value=False),
            patch.object(daily_briefing.subprocess, 'run') as run,
            patch.object(daily_briefing, 'read_clipboard_text') as clipboard,
            patch.object(daily_briefing.webbrowser, 'open') as browser,
        ):
            run.return_value.returncode = 0
            self.assertEqual(daily_briefing.main(['--no-wait']), 0)
            run.assert_called_once()
            clipboard.assert_not_called()
            browser.assert_not_called()
