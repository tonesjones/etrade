"""Write one Obsidian note per imported briefing reply."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROVIDERS = {"chatgpt", "claude", "codex"}


def _unique_note_path(vault_dir: Path, engine: str, date_str: str) -> Path:
    base = f"{date_str} {engine}"
    path = vault_dir / f"{base}.md"
    suffix = 2
    while path.exists():
        path = vault_dir / f"{base} {suffix}.md"
        suffix += 1
    return path


def write_vault_note(
    vault_dir: Path,
    *,
    reply: str,
    meta: dict,
    engine: str,
    status: str,
    report: str,
    date_str: str,
    response_md: Path,
) -> Path:
    vault_dir.mkdir(parents=True, exist_ok=True)
    earlier = list(vault_dir.glob("*.md"))
    previous = max(earlier, key=lambda p: p.stat().st_mtime) if earlier else None
    path = _unique_note_path(vault_dir, engine, date_str)
    lines = [
        "---",
        "type: briefing",
        f"created: {date_str}",
        "tags: [etrade, briefing]",
        f"provider: {engine if engine in PROVIDERS else 'mixed'}",
        "review_status: pending",
        f"engine: {engine}",
        f"status: {status}",
        f'briefing_id: "{meta.get("briefing_id") or ""}"',
        "---",
        "",
        f"# Etrade briefing {date_str} ({engine})",
    ]
    if previous:
        lines += ["", f"Previous: [[{previous.stem}]]"]
    lines += [
        "", "## Import result", "```text", report.rstrip("\r\n"), "```",
        "", f"Saved reply: {response_md}",
        "", "## Reply", reply.replace("\r\n", "\n").rstrip("\n"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def check_vault_dir(vault_dir: Path) -> str | None:
    """Refuse a folder that git would commit: the note holds real portfolio data."""
    target = vault_dir.resolve()
    ancestor = target
    while not ancestor.exists():
        ancestor = ancestor.parent
    try:
        inside = subprocess.run(
            ["git", "-C", str(ancestor), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    ignored = subprocess.run(
        ["git", "-C", str(ancestor), "check-ignore", "-q", str(target / "probe.md")],
        capture_output=True, check=False,
    )
    if ignored.returncode == 0:
        return None
    if ignored.returncode == 1:
        return f"{vault_dir} is inside a git repository that doesn't ignore it"
    return f"could not check whether git ignores {vault_dir}"
