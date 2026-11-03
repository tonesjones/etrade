#!/usr/bin/env python3
"""Authorize when needed, build a live prompt, then import the copied model reply."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from etrade_auth import AUTH_DATE_KEY, ENV_PATH, ET
from get_portfolio import read_clipboard_text
from import_response import ENGINES, extract_json_block


def authorization_needed(today: datetime | None = None) -> bool:
    load_dotenv(ENV_PATH, override=True)
    today_text = (today or datetime.now(ET)).strftime("%Y-%m-%d")
    return (
        os.getenv(AUTH_DATE_KEY) != today_text
        or not os.getenv("ETRADE_ACCESS_TOKEN")
        or not os.getenv("ETRADE_ACCESS_TOKEN_SECRET")
    )


def reply_briefing_id(text: str, prompt_text: str) -> str | None:
    """Return the briefing_id of a copied reply, ignoring the copied prompt itself.

    The prompt check compares letters and digits only, so a clipboard read that
    garbles symbols such as ≈ or … still recognizes the prompt.
    """
    if re.sub(r"[^A-Za-z0-9]", "", text) == re.sub(r"[^A-Za-z0-9]", "", prompt_text):
        return None
    block = extract_json_block(text)
    if block is None:
        return None
    try:
        reply = json.loads(block)
    except json.JSONDecodeError:
        return None
    return reply.get("briefing_id") if isinstance(reply, dict) else None


def is_reply_for(text: str, briefing_id: str, prompt_text: str) -> bool:
    return reply_briefing_id(text, prompt_text) == briefing_id


def wait_for_reply(engine: str, timeout_minutes: float) -> int:
    """Wait for the current briefing reply and import it once."""
    project = ENV_PATH.parent
    try:
        meta = json.loads((project / "briefings/briefing_latest.json").read_text(encoding="utf-8"))
        briefing_id = meta["briefing_id"]
        if not isinstance(briefing_id, str) or not briefing_id:
            raise ValueError("Missing briefing identity")
        prompt_text = (project / "prompts/daily_briefing_prompt_latest.md").read_text(
            encoding="utf-8"
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Cannot wait for reply: {exc}", file=sys.stderr)
        return 1
    print("Paste the prompt into the model you choose. When done, copy the whole reply.")
    print("This window imports it automatically. Ctrl+C to stop and use reply.cmd later.")
    deadline = time.monotonic() + timeout_minutes * 60
    noticed = set()
    try:
        while time.monotonic() < deadline:
            try:
                text = read_clipboard_text()
            except Exception:
                text = ""
            reply_id = reply_briefing_id(text, prompt_text)
            if reply_id == briefing_id:
                return subprocess.run([
                    sys.executable, str(project / "import_response.py"),
                    "--engine", engine, "--from-clipboard", "--briefing-id", briefing_id,
                ]).returncode
            if reply_id and text not in noticed:
                print("Ignoring a reply for a different briefing_id.")
                noticed.add(text)
            time.sleep(2)
    except KeyboardInterrupt:
        print(f"Nothing was imported. After copying the reply, run .\\reply.cmd {engine}")
        return 0
    print(f"Timed out; nothing was imported. After copying the reply, run .\\reply.cmd {engine}")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", nargs="?", choices=ENGINES, default="chatgpt")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout-minutes", type=float, default=60)
    args = parser.parse_args(argv)
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
    if build.returncode or args.no_wait:
        return build.returncode
    return wait_for_reply(args.engine, args.timeout_minutes)


if __name__ == "__main__":
    raise SystemExit(main())
