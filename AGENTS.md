# Portfolio review context

Before preparing a portfolio review or briefing, read `docs/manual-stock-plan-holdings.md`. It records the owner's separate Synopsys E*TRADE stock-plan holdings, which the script does not retrieve.

Include this manual supplement in combined exposure calculations when relevant. Keep share counts fixed until the owner reports a share-changing event, refresh prices and values, and avoid double counting if automated coverage changes. Do not treat estimated stock-plan basis as verified tax basis.

## Daily workflow and verification

Follow the README's Daily workflow. `.\daily.cmd` authorizes when needed, builds the refreshed prompt, then waits for the copied model reply and imports it (`.\daily.cmd codex` selects that engine). `.\reply.cmd` imports a reply later from the clipboard. These commands can authorize API access, fetch live data, or import local proposals; they are not routine code-verification commands.

Importing a proposal does not execute a trade. Review ACCEPTED/REJECTED, reasoning, tax impact, and buying power, then place any approved trade manually in E*TRADE. Keep credentials and private portfolio output out of commits.

For code changes, use the existing environment to run `ruff check .` and `python -m unittest discover -s tests -v`. Do not run the live daily or reply workflow merely to verify documentation.
