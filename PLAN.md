# Plan

## Goal

Each market morning, one window takes the owner from the E*TRADE login to an imported, validated reply saved in the vault. The only manual steps are the login, pasting the prompt into the model, copying the reply, and placing any trade in E*TRADE.

The briefing flags concentration and stretched winners early enough to take profits before a sell-off. Two hard limits are binding. Everything else is a required review that the model answers with research. Nothing sells automatically on a drawdown.

## Why

Through a month-long sell-off every reply said KEEP. The prompt said a large gain or a soft-limit breach was never enough to reduce. Direct AI/semi was above its 40% ceiling in almost every snapshot. The prompt carried no price history or technicals, and focus ranking put small loss reviews ahead of the largest winners.

## Owner decisions (2026-11-05)

- No automatic REDUCE after a drawdown. Use technicals to spot stretched names near a top and require a profit-taking review.
- Trim size comes from diversification (dollars back to the 15% single-name target), and the model sizes the trim. Not a fixed share of the gain.
- Risk-off is the model's call from web research and the technicals, not a fixed drawdown threshold.
- The technicals come from Tiingo end-of-day prices (free key in `.env`).

## Items

### 1. Technicals from Tiingo

`market_data.py` fetches about 400 days of adjusted daily closes per held ticker plus SPY, QQQ and SMH. It caches them in `briefings/prices/` for the day and computes the distance from the 50- and 200-day averages, RSI(14), the distance from the 52-week high, and 1- and 3-month returns. A missing key or a failed fetch puts a "technicals unavailable" line in the prompt instead of failing the build.

Done when:
- Tests cover the indicator math, the cache, the auth header (the key never goes in the URL), and the missing-key and HTTP-error paths without network access.

### 2. Rules and reviews

Binding, answered with COMPLY (cut net exposure by at least the minimum after all trades) or EXCEPTION. The model can only request an exception. The limit stays unresolved, and fires daily, until the owner adds an entry to `limit_exceptions` in `portfolio_context.json` (`kind`, `scope`, `approved_until`, `note`) or the position is cut:
- `SLEEVE`: direct AI/semi above its soft ceiling for N consecutive snapshots. The minimum sale brings it back to the ceiling.
- `HARD_CAP`: a single name above the hard cap. The minimum sale brings it back to the 15% soft limit.

Required reviews, answered with TRIM (a net sale of the name) or HOLD (why holding beats trimming given concentration, uncertainty, tax and holding period; not a short-term price call):
- `REBALANCE`: a non-index name above the 15% target. The block shows the dollars to reach it, and the model sizes the trim.
- `STRETCHED`: a name with a gain whose technicals hit a stretch threshold (RSI, or distance above the 50- or 200-day average). Technicals with a last close more than 4 days old are shown with their date as context and never fire it.
- A review rests for `repeat_days` only after an accepted reply answered it. An unanswered or rejected review fires again the next day.

EXCEPTION, HOLD and the market regime each need a reason, plus a source with an http(s) URL and a real date on or before the data date.

Each item lists untaxed IRA/Roth shares first, then taxable lots by tax per dollar, with the estimated tax and the break-even drop. Shares with no cost basis show as unknown tax, and the break-even drop is left out. Items rank first in focus.

Done when:
- Tests cover each item firing and not firing, the repeat window, index exemption, sell order and tax math, and focus ranking.
- Replaying a saved snapshot where the sleeve has been above its ceiling for 5 or more days fires the sleeve rule with the cut back to the ceiling.

### 3. Market regime from the model

The prompt gives the portfolio drawdown and the SPY, QQQ and SMH technicals, and asks the model to research the market and set `market_regime` to RISK_ON, NEUTRAL or RISK_OFF with dated sources. RISK_OFF means working the AI/semi sleeve toward the glide level.

Done when:
- The importer rejects a researched reply with no valid regime, and warns on RISK_OFF with no AI/semi sale.

### 4. Import checks

`import_response.py` checks each COMPLY and TRIM against net exposure after all proposed trades, with a $1 rounding allowance. It rejects any buy that leaves a non-index name above the hard cap, unless the owner approved a `HARD_CAP` exception for that ticker in `limit_exceptions`. Only answers that passed validation in a researched reply reach the decision history, with their kind, scope and reasons, so only they start the review cooldown.

Done when:
- Import tests cover missing answers, a sale bought back in the same proposal, the rounding allowance, TRIM with no net sale, EXCEPTION and HOLD without full evidence, and briefings with no saved items (unchanged behavior).

### 5. Malformed replies are rejected, not crashed on

`_check_names` tests `view` and `action` with `in VIEWS` / `in ACTIONS`, and the trade check tests `side` with `in SIDES`. An unhashable value such as `"action": []` raises TypeError, so the importer exits before it saves its report. Check that each value is a string before the set lookup. The regime and rule-response checks already convert to `str` first and are safe.

Done when:
- A reply with a list or dict in `view`, `action` or `side` imports as REJECTED with a named error, and the report is saved.
- Tests cover each of the three fields.

### 6. The briefing ID covers what the reply is checked against

`make_briefing_id` hashes the portfolio, policy, context, supplement, weights and observations, but not the technicals or the fired rule items. Two builds with the same holdings and different technicals get the same ID. The second build overwrites the first one's metadata, so a reply to the first prompt is checked against the second prompt's rules. Add the fired rule items (`trim`), the focus list (`output_policy`) and the technicals to the identity.

Done when:
- A test builds twice from the same portfolio with different technicals that change the fired rules, and gets two different IDs.
- Two builds from identical inputs still get the same ID.

### 7. README covers the Tiingo setup

The README doesn't mention Tiingo. Add the free-key step, the `TIINGO_API_KEY` line in `.env`, the `briefings/prices/` cache, and what the prompt shows when the key is missing.

Done when:
- A new user can set up technicals from the README alone.

### 9. Record which proposals the owner actually traded

An accepted import records the model's trades as `NOT APPROVED / NOT EXECUTED`. Nothing updates that when the owner places the trade, so the next briefing still treats an executed sale as an open suggestion. Each live build now checks the open proposals in `decision_history` against two sources and updates them in `portfolio_context.json`:

- E\*TRADE transactions (read-only Accounts API): a `Sold` or `Bought` transaction in the proposal's account and symbol, dated on or after the proposal. A full match sets `EXECUTED CONFIRMED`.
- Position changes: the share count in the proposal's account against the observation snapshot saved with the proposal's briefing. A full match with no transaction sets `EXECUTED (INFERRED FROM POSITIONS)`, because a transfer or split looks the same.

Partial fills set `PARTIALLY EXECUTED`. Each record gets an `execution` block with the method, quantities, dates and prices. Only the newest open proposal per account, symbol and side is checked; older ones are marked `SUPERSEDED`. Proposals stop being checked 7 days after their date. A failed transactions fetch or an account that failed to load falls back or is skipped; it never marks a trade executed and never fails the build.

Done when:
- Tests cover a confirmed sale, a partial fill, a position-only match, no change, a failed account, a failed transactions fetch, supersession and the 7-day cutoff, without network access.
- The build prints what it matched and the prompt's decision history shows the new state.

### 8. Cleanup (nice later)

- Delete `tax_bucket_label()` and `format_taxable_cost_table()`. Nothing calls them, not even tests.
- `collect_tax_flags()`, `build_prompt()` and `is_reply_for()` are called only from tests. Either move those tests to the production entry points and delete the functions, or leave them.
- `portfolio_policy.POLICY` is still used by `tests/test_money_math.py` and `tests/test_portfolio.py`. Move those tests to `load_policy()` before deleting it.
- `get_portfolio.py` imports `requests` directly, but only `pyetrade` installs it. Declare `requests` explicitly.
- Keep the dependency list in `pyproject.toml` only, and make `requirements.txt` install `-e .` or delete it.
- Move the repeated lot fallback in the importer, the trim rules and the briefing formatting into one shared helper.
- Delete the local `feat/trim-rules` and `fix/clipboard-prompt-import` branches, their remote branches, and `Etrade-feat-trim-rules.bundle`. PR #9 is merged, so nothing depends on the bundle.

## Status

- 2026-11-02: one-window `daily.cmd`, offline build removed, docs updated (PRs #6 to #8).
- 2026-11-05: first version of the rules built on `feat/trim-rules` (mechanical drawdown and gain trims). The owner redirected the design the same day, and items 1 to 4 replace that version.
- 2026-11-05: items 1 to 4 built on `feat/trim-rules`. 273 tests pass and ruff is clean. A replay of a saved snapshot fires the sleeve rule and a rebalance review as expected. STRETCHED is unverified against live Tiingo data until a key is set.
- 2026-11-05: review fixes on PR #9: net-exposure compliance, cooldown after an accepted answer, stale technicals kept out of reviews, unknown tax kept unknown, owner-approved exceptions, a balanced HOLD, and evidence checks. 276 tests pass.
- 2026-11-05: second review on PR #9: a hard-cap check on buys after all trades, and the cooldown counts only validated answers. 278 tests pass.
- 2026-11-05: items 5 to 7 built on `fix/review-followups`. 282 tests pass and ruff is clean. Item 8 and the open decisions are still open.
- 2026-11-12: item 9 built on `feat/execution-tracking`. 301 tests pass and ruff is clean. The transactions request is unverified against the live API until the next daily build.
- The repo is public. Keep portfolio values, weights and dollar amounts out of this file, commits and PR text.

## Next

- Owner creates a free Tiingo key and adds `TIINGO_API_KEY` to `.env`.
- Owner reviews the stretch thresholds in `portfolio_policy.json`. The shipped values are placeholders.

## Open decisions

From the post-#9 review. These are policy questions for the owner, not bugs:
- A single name above the hard cap must be cut back to the 15% target, not just to the cap. Is the larger cut intended?
- Sleeve enforcement counts saved snapshots above the ceiling, not consecutive trading days. Skipped days don't reset the count.
- At or above the no-add level, every direct-sleeve buy is rejected, even inside a rotation that cuts the sleeve overall.
- An EXCEPTION reply can still import as ACCEPTED with an unresolved-limit warning. Would a separate "needs owner decision" status be better?
- The tax rates are fixed estimates in the policy file, not verified tax figures.

- Later, if copying and pasting still costs too much time: call a model API with web search instead of the chat window (per-run cost, different data-retention terms).
