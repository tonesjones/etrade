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

## Status

- 2026-11-02: one-window `daily.cmd`, offline build removed, docs updated (PRs #6 to #8).
- 2026-11-05: first version of the rules built on `feat/trim-rules` (mechanical drawdown and gain trims). The owner redirected the design the same day, and items 1 to 4 replace that version.
- 2026-11-05: items 1 to 4 built on `feat/trim-rules`. 273 tests pass and ruff is clean. A replay of a saved snapshot fires the sleeve rule and a rebalance review as expected. STRETCHED is unverified against live Tiingo data until a key is set.
- 2026-11-05: review fixes on PR #9: net-exposure compliance, cooldown after an accepted answer, stale technicals kept out of reviews, unknown tax kept unknown, owner-approved exceptions, a balanced HOLD, and evidence checks. 276 tests pass.
- 2026-11-05: second review on PR #9: a hard-cap check on buys after all trades, and the cooldown counts only validated answers. 278 tests pass.
- The repo is public. Keep portfolio values, weights and dollar amounts out of this file, commits and PR text.

## Next

- Owner creates a free Tiingo key and adds `TIINGO_API_KEY` to `.env`.
- Owner reviews the stretch thresholds in `portfolio_policy.json`. The shipped values are placeholders.

## Open decisions

- Later, if copying and pasting still costs too much time: call a model API with web search instead of the chat window (per-run cost, different data-retention terms).
