# Plan

## Goal

Each market morning, one window takes the owner from the E*TRADE login to an imported, validated reply saved in the vault. The only manual steps are the login, pasting the prompt into the model, copying the reply, and placing any trade in E*TRADE.

## Items

### 1. Remove the offline prompt build

`briefing.cmd` and the `--from-clipboard`, `--input-file`, and `--allow-stale` options of `build_briefing_prompt.py` are removed. The portable portfolio format and its parser stay, because `import_response.py` reads saved briefings with it.

Done when:
- `briefing.cmd` is gone and `build_briefing_prompt.py` always builds from live data.
- `import_response.py` still imports saved replies; tests pass.

### 2. One-window daily run

`daily.cmd [engine]` opens the E*TRADE authorization page, builds the prompt, opens ChatGPT for the `chatgpt` engine, then waits for a copied reply whose `briefing_id` matches the new prompt and imports it. `--no-wait` keeps the old build-only behavior. Ctrl+C stops waiting without importing.

Done when:
- Unit tests cover the reply match (prompt text, matching reply, other briefing, no JSON), a wait that imports after non-matching polls, `--no-wait`, and Ctrl+C.
- One real morning run imports the reply with no `reply.cmd`.

### 3. Docs match the daily use

The README daily section describes the one-window run first. Offline and two-model sections are gone. `docs/history-retention.md` and `routing-log.md` are deleted.

Done when:
- No repo doc mentions `briefing.cmd`, the offline build, or the retired files.

## Status

- 2026-10-27: reply import, vault note, and focus cap done (PR #5). 238 tests pass.
- 2026-10-27 to 2026-11-02: five market days imported as `ACCEPTED` with ChatGPT.
- 2026-11-02: scope audit. Items 1 to 3 built on `chore/daily-flow-cleanup`. Items 1 and 2 drafted by Codex (Luna and Sol), reviewed in the main session.

## Next

- Run the one-window flow on the next market morning and confirm the vault note appears.

## Open decisions

- Focus ranking puts every loss review ahead of size-only names, so large holdings can drop to the monitor scan while small losses get full analysis. Decide whether large holdings should outrank small losses.
- Later, if copying and pasting still costs too much time: call a model API with web search instead of the chat window (per-run cost, different data-retention terms).
