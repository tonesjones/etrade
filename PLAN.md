# Plan

## Goal

Make the daily loop finish in a chat window and save each reply to the Obsidian vault without manual file handling:

1. `.\daily.cmd` builds the prompt and copies it.
2. The owner pastes it into ChatGPT and copies the reply.
3. `.\reply.cmd` imports the reply, validates it, records continuity, and writes a vault note.

## Items

### 1. Stop dust positions from triggering decision days

Positions under `analyze_market_value_floor` ($5,000) on both the prior and current snapshot no longer count as weight changes or position changes for decision-day and focus purposes.

Done when:
- A $300 position appearing, or a 1-share position disappearing, leaves a ROUTINE DAY routine.
- The same change at or above the floor still triggers a decision day.
- Tests cover both cases.

### 2. Candidate review runs weekly, not on every decision day

Done when:
- `refresh_candidates` is true only on the first run or when the weekly cadence is due.
- A decision day within the 7-day window does not request candidates.
- Tests cover it.

### 3. Cap fully analysed focus names

New optional policy key `max_focus_names` (default 8). Focus names are ranked by reason priority, then market value. Names over the cap move to the monitor list with their reasons. The manual SNPS entry is added on top of the cap.

Done when:
- A prompt with more qualifying names lists at most 8 API focus names, plus SNPS when present.
- Deferred names appear in the monitor scan with their reasons.
- The importer requires answers only for names in the saved `focus_symbols`.
- Tests cover ranking and overflow.

### 4. `reply.cmd` and the vault note

`reply.cmd [engine]` (default `chatgpt`) runs `import_response.py --from-clipboard`. When `ETRADE_VAULT_BRIEFINGS_DIR` is set, the importer also writes a Markdown note there with frontmatter, the import report, a link to the previous note, and the full reply. It refuses a directory inside a git repo that the repo does not ignore, because the note contains real portfolio data.

Done when:
- `reply.cmd` with a copied reply prints ACCEPTED or REJECTED and the note path.
- The note is written for both statuses and never overwrites an earlier note.
- An unset variable skips the note with a one-line message; an unignored git path is refused.
- Tests cover note content, naming, and the git-ignore guard.

### 5. Docs

README daily workflow, `.env.example`, and the files table describe `reply.cmd` and the vault note.

## Status

- 2026-10-27: plan written. Items 1–3 delegated to Codex (Sol), item 4 to Codex (Luna), item 5 kept.
- 2026-10-27: items 1–5 done on `feat/reply-vault-focus-cap`. 238 tests pass and ruff is clean. Replaying the 2026-10-27 portfolio gives 9 focus names (was 17) and no candidate review. Not yet run end to end with a real ChatGPT reply.

## Next

- Set `ETRADE_VAULT_BRIEFINGS_DIR` in `.env` and run `.\reply.cmd` on the next real reply.
- Check that ChatGPT finishes a decision-day reply with 9 names.

## Open decisions

- Ranking puts every loss review ahead of size-only names. On 2026-10-27 that analyzes MP (1.2%) and ETHA (1.0%) in full but defers GOOGL (12.7%) and NVDA (11.2%) to the monitor scan. Decide whether large holdings should outrank small losses.
