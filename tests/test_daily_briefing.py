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
        assert daily_briefing.main() == 0
    assert launched == ['etrade_auth.py', 'build_briefing_prompt.py']


class DailyBriefingTests(unittest.TestCase):
    def test_build_inherits_refreshed_credentials_after_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            check_refreshed_credentials(Path(directory))
