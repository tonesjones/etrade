#!/usr/bin/env python3
"""Run the interactive daily authorization when needed, then build a live prompt."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from dotenv import load_dotenv

from etrade_auth import AUTH_DATE_KEY, ENV_PATH, ET


def authorization_needed(today: datetime | None = None) -> bool:
    load_dotenv(ENV_PATH, override=True)
    today_text = (today or datetime.now(ET)).strftime("%Y-%m-%d")
    return (
        os.getenv(AUTH_DATE_KEY) != today_text
        or not os.getenv("ETRADE_ACCESS_TOKEN")
        or not os.getenv("ETRADE_ACCESS_TOKEN_SECRET")
    )


def main() -> int:
    if authorization_needed():
        auth = subprocess.run([sys.executable, str(ENV_PATH.parent / "etrade_auth.py")])
        if auth.returncode:
            print("Daily authorization failed; no briefing was built.", file=sys.stderr)
            return auth.returncode
        # Authorization updates the file in a child; refresh what the build inherits.
        load_dotenv(ENV_PATH, override=True)
    build = subprocess.run([sys.executable, str(ENV_PATH.parent / "build_briefing_prompt.py")])
    if build.returncode:
        print(
            "Live briefing build failed; the clipboard was not updated by this run.",
            file=sys.stderr,
        )
    return build.returncode


if __name__ == "__main__":
    raise SystemExit(main())
