import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_import_response import ImportResponseTests, reply_text

import vault_notes


class VaultNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, **kwargs):
        defaults = dict(
            vault_dir=self.root / "vault", reply="hello\r\nworld", meta={"briefing_id": "b1"},
            engine="chatgpt", status="ACCEPTED", report="ok", date_str="2026-09-18",
            response_md=Path("reply.md"),
        )
        defaults.update(kwargs)
        return vault_notes.write_vault_note(**defaults)

    def test_frontmatter_and_body(self):
        body = self.write().read_text(encoding="utf-8")
        self.assertTrue(body.startswith(
            '---\ntype: briefing\ncreated: 2026-09-18\ntags: [etrade, briefing]\n'
            'provider: chatgpt\nreview_status: pending\nengine: chatgpt\nstatus: ACCEPTED\n'
            'briefing_id: "b1"\n---\n'
        ))
        self.assertIn("# Etrade briefing 2026-09-18 (chatgpt)", body)
        self.assertIn("## Import result\n```text\nok\n```", body)
        self.assertIn("Saved reply: reply.md", body)
        self.assertTrue(body.endswith("## Reply\nhello\nworld\n"))
        self.assertNotIn("Previous:", body)

    def test_unknown_engine_is_mixed_provider(self):
        body = self.write(engine="grok").read_text(encoding="utf-8")
        self.assertIn("provider: mixed\n", body)

    def test_same_day_note_gets_suffix_and_keeps_the_first(self):
        first = self.write()
        first.write_text("keep", encoding="utf-8")
        second = self.write()
        self.assertEqual(second.name, "2026-09-18 chatgpt 2.md")
        self.assertEqual(first.read_text(encoding="utf-8"), "keep")
        self.assertIn("Previous: [[2026-09-18 chatgpt]]", second.read_text(encoding="utf-8"))

    def test_previous_links_the_most_recent_note(self):
        older = self.write(date_str="2026-09-16")
        newer = self.write(date_str="2026-09-17")
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        current = self.write(date_str="2026-09-18")
        self.assertIn("Previous: [[2026-09-17 chatgpt]]", current.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_folder_in_git_must_be_ignored(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        target = repo / "vault"
        self.assertIsNotNone(vault_notes.check_vault_dir(target))
        (repo / ".gitignore").write_text("vault/\n", encoding="utf-8")
        self.assertIsNone(vault_notes.check_vault_dir(target))


class ImportVaultNoteTests(unittest.TestCase):
    setUp = ImportResponseTests.setUp
    write_briefing = ImportResponseTests.write_briefing
    run_import = ImportResponseTests.run_import

    def test_main_writes_a_note_for_accepted_and_rejected_replies(self):
        vault = Path(self._tmp.name) / "vault"
        for reply, status, expected_code in (
            (reply_text(), "ACCEPTED", 0),
            ("no json", "REJECTED", 1),
        ):
            with self.subTest(status=status):
                code, out = self.run_import(reply, extra=("--vault-dir", str(vault)))
                self.assertEqual(code, expected_code)
                self.assertIn("Vault note:", out)
                notes = sorted(vault.glob("*.md"), key=lambda p: p.stat().st_mtime)
                self.assertIn(f"status: {status}\n", notes[-1].read_text(encoding="utf-8"))
        self.assertEqual(len(list(vault.glob("*.md"))), 2)

    def test_main_skips_the_note_when_no_folder_is_set(self):
        code, out = self.run_import(reply_text())
        self.assertEqual(code, 0)
        self.assertIn("Vault note skipped: set ETRADE_VAULT_BRIEFINGS_DIR", out)


if __name__ == "__main__":
    unittest.main()
