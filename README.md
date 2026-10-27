# E*TRADE daily portfolio briefing

This project pulls your live E\*TRADE positions from every active account and builds a briefing prompt for a language model. The script copies the prompt to your clipboard and saves a dated copy. You paste the prompt into a model with web search, such as Codex, ChatGPT, Grok, or Claude. Then you import the model's reply, and a validator checks every proposed trade against your actual positions.

The project is read-only. It never places, previews, or cancels orders. You place any trade yourself in E\*TRADE.

Each holding in the prompt carries E\*TRADE's price paid, cost per share, total cost, unrealized P/L, and date acquired. Taxable lots are marked ST (held one year or less) or LT (held more than one year). IRA and Roth accounts show economic P/L, but the prompt does not treat their sales as capital-gains events. The prompt also computes two concentration weights: the direct AI and semiconductor sleeve, and the broader AI-cycle sleeve. Concentration risk therefore covers more than the six chip tickers.

## Set up once

1. Create a virtual environment and install the dependencies. `daily.cmd` and `briefing.cmd` expect the environment at `.venv`.

   ```powershell
   cd path\to\etrade-briefings
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Copy the secrets template:

   ```powershell
   copy .env.example .env
   ```

3. Add your E\*TRADE app keys to `.env`. Set `ETRADE_DEV=true` for the sandbox or `ETRADE_DEV=false` for production, and use the matching keys.

   ```env
   ETRADE_CONSUMER_KEY=...
   ETRADE_CONSUMER_SECRET=...
   ETRADE_DEV=false
   ```

4. Run `.\daily.cmd` as described in [Daily workflow](#daily-workflow). It prompts you to authorize the app when needed.

The scripts pull every active account by default. To pull one account only, set `ETRADE_ACCOUNT_ID_KEY` in `.env`.

## Daily workflow

### 1. Build the live briefing

Run this command from PowerShell in the project folder:

```powershell
.\daily.cmd
```

`daily.cmd` uses the project's `.venv` interpreter. If either access token is missing or `ETRADE_AUTH_DATE` is not today's US Eastern date, it starts authorization:

1. Open the URL that the script prints, log in, and authorize the app.
2. Paste the verification code into the terminal.

Authorization saves the access token pair and `ETRADE_AUTH_DATE` to `.env` without printing the token values. The launcher reloads those values, builds the live prompt, and copies it to the clipboard. Later runs on the same Eastern day reuse the saved tokens.

If authorization or the build fails, the launcher stops. A failed run leaves the clipboard unchanged, so don't send its existing contents as a new briefing.

E\*TRADE access tokens expire at midnight US Eastern. If E\*TRADE rejects tokens dated today, authorize again and rerun the launcher:

```powershell
.\.venv\Scripts\python.exe etrade_auth.py
.\daily.cmd
```

### 2. Send the prompt to a model

Open a fresh chat in a model with web search, paste the prompt with **Ctrl+V**, and send it.

#### Build manually or from saved data

If your virtual environment is active and today's tokens are valid, `python build_briefing_prompt.py` builds the live prompt directly.

For an offline build, use a portfolio dump you saved earlier. To create a dump, run `python get_portfolio.py` with valid tokens. That command contacts E\*TRADE, saves the portfolio, and copies it with an embedded JSON payload to the clipboard. Then run:

```powershell
.\briefing.cmd
```

`briefing.cmd` builds the prompt from the clipboard without contacting E\*TRADE. It validates the JSON payload and rejects older text-only dumps that lack exact account and lot identity. Offline input older than 24 hours requires `--allow-stale`.

If another program replaced the clipboard, build from the saved portfolio file instead:

```powershell
python build_briefing_prompt.py --input-file .\briefings\portfolio_YYYY-MM-DD.txt
```

The `python` commands in this README assume you activated `.venv`. The `.cmd` launchers select that environment themselves.

### 3. Import the model's reply

The prompt asks the model to end its reply with a fenced `json` block of proposed actions. The importer reads that block. You don't send it to another model.

Copy the whole reply with the message's copy button, then run:

```powershell
.\reply.cmd
```

`reply.cmd` imports a ChatGPT reply from the clipboard. For another model, name its engine: `.\reply.cmd codex`. The engines are `codex`, `claude`, `chatgpt`, `grok`, and `other`. To import from a file, run `python import_response.py --engine chatgpt --input-file reply.md`.

To save each reply as a note in your Obsidian vault, set `ETRADE_VAULT_BRIEFINGS_DIR` in `.env` to a folder that git ignores, for example a `95 Private` folder:

```env
ETRADE_VAULT_BRIEFINGS_DIR=C:\path\to\Vault\95 Private\Etrade\Briefings
```

The importer then writes `YYYY-MM-DD <engine>.md` to that folder for every import, accepted or rejected. The note holds the import result, a link to the previous note, and the full reply. The note contains real portfolio data, so the importer refuses a folder inside a git repository that doesn't ignore it. `--vault-dir PATH` overrides the setting for one run.

The importer reads the reply's `briefing_id` to select the saved portfolio for that prompt, even if you built another prompt afterward. Use `--briefing-id ID` to select it explicitly. A date alone is ambiguous when several briefings exist for that day.

The importer checks the reply against that portfolio:

- The reply answers every focus name.
- Every ticker and account exists.
- Each sale quantity fits within the named lot.
- Each buy is funded inside its own account. Only actual `CASH` counts as cash. To spend `SGOV`, `BIL`, or `SGOVX`, the reply must include a same-account `SELL` of it and name that trade's ID in the buy's `funded_by`.
- No buy creates a wash sale. The lookback covers all accounts.
- No buy adds to the direct AI and semiconductor sleeve when the sleeve is at its no-add weight.

The importer then recomputes proceeds, realized gain per lot, ST or LT status, days until long-term, and portfolio weights before and after. It prints `ACCEPTED` or `REJECTED`. Don't use any trade from a `REJECTED` reply.

The importer doesn't know settlement dates or E\*TRADE buying power, so its funding status always ends in `SETTLEMENT_UNKNOWN`. Confirm buying power in E\*TRADE before you place a buy funded by a same-day sale.

`ACCEPTED` means the trades fit your positions. It doesn't mean they are good trades. Read the reasoning, and read the realized-gain numbers in the saved `.json`, before you place anything.

The importer saves each reply to `responses/YYYY-MM-DD_<engine>.md` and `.json`. Another import on the same day adds a suffix, such as `_2`, so nothing is overwritten. To re-check a saved reply, pass that file:

```powershell
python import_response.py --engine codex --input-file .\responses\YYYY-MM-DD_codex.md
```

If the copy dropped the code fences, the importer still finds the last JSON object that has a `briefing_id`. If it reports `no_json_block`, the model didn't write the block. Ask the model to "end with the fenced json actions block exactly as the prompt specified", then import the new reply.

### 4. Compare two models (optional)

Paste the same prompt into two models in fresh chats, and import each reply with its own `--engine`. Compare their sources, reasoning, and the tax impact in each `responses/*.json` file. Agreement between models is not independent evidence that a trade is suitable.

### Run the workflow from Claude Code

Run `claude` in the project folder, or open the folder in the Code tab of the Claude desktop app. Claude can run the build and import scripts, read `responses/`, and compare replies for you. You still log in for `etrade_auth.py`, paste the prompt into the outside model, and place trades in E\*TRADE.

### Get updates

The default branch is `master`.

```powershell
git pull origin master
```

After you pull, rebuild the prompt before you send it, so the model's reply matches the current JSON contract.

## What each command writes

| Command | Output |
|---------|--------|
| `daily.cmd` | Saves refreshed tokens to `.env` when needed, then writes the same outputs as the live prompt build. |
| `build_briefing_prompt.py` | Prompt to the clipboard and `prompts/`. Writes `briefings/weights_*.json` (portfolio delta) and `briefings/observations_*.json` (account, quantity, and lot reconciliation). |
| `briefing.cmd` | The same outputs, built offline from the clipboard payload. |
| `get_portfolio.py` | Full per-account dump with cost, P/L, and ST or LT status, to the clipboard and `briefings/portfolio_YYYY-MM-DD.txt`. For diagnostics and offline builds. Don't paste it into the model. |
| `import_response.py` | `responses/YYYY-MM-DD_<engine>.md` and `.json`, with the checks, computed trades, and weights. A vault note when `ETRADE_VAULT_BRIEFINGS_DIR` is set. |
| `reply.cmd` | The same outputs as `import_response.py`, from a reply on the clipboard. |

## What the prompt asks the model to do

The prompt asks for decision support, not a holdings summary. It assumes a model with web search. The model has no trading authority, and its output is a proposal, not an approved trade. The model must:

- Research first. For each focus name, find dated news since the last snapshot. Check the market backdrop, and scan the monitor-only names for material events. "No material news" counts as a finding.
- Work from one focus list. Each focus name carries its reason: a limit breach, a taxable loss to review, a weight or position change, or size and loss on a decision day. The list holds at most `max_focus_names` names, ranked by reason and then by value. Names past the cap and other sized holdings are monitor-only.
- Give each focus name a fundamental view and, separately, one portfolio action: `KEEP`, `REDUCE`, `REPLACE`, `DEPLOY`, or `NO ACTION`. `NO ACTION` requires a named missing fact that would change the decision.
- Treat a soft concentration breach as "review and don't add", not as an automatic sale. Any cut for a limit starts from the computed smallest dollar cut.
- Name the account and the exact taxable lot for every sale, and respect wash-sale rules on taxable losses.
- Cite the source and date of every company-specific fact. Replace vague time horizons with observable triggers.
- Reconcile earlier proposals when a decision history is supplied.

The prompt leaves out empty sections, such as research notes when you have none, or the $10k example table on routine days. Each lot appears once, in a single table that covers taxable, IRA, and Roth accounts.

The prompt includes two deltas against the previous pull:

- The portfolio delta shows weight moves of 0.5 percentage points or more, sleeve weights, and breaches of the 15% single-name cap.
- The account delta compares per-account quantities and lot identity. It reports a position that appeared or disappeared, a quantity that rose or fell, a lot that changed, or no change.

The account delta shows that holdings changed. It doesn't prove a buy or a sale, because transfers, splits, and other corporate actions also change positions. It also doesn't show why a holding changed, or whether you acted on a model's proposal. The prompt marks earlier proposals `NOT CAPTURED` unless your decision history records them.

A position worth less than `analyze_market_value_floor` on both days doesn't count as a change, so it can't turn a routine day into a decision day. The account delta still lists it.

The deltas compare against the most recent earlier calendar day, not an earlier run on the same day. They need a previous `briefings/weights_*.json` or dated prompt.

## Change the decision policy

The briefing math and most of the prompt's instructions read `portfolio_policy.json`. When a cap, sleeve, or threshold changes, edit that file, not the Python. `--policy path.json` makes `build_briefing_prompt.py`, `get_portfolio.py`, or `import_response.py` load a different policy file. `schema_version` must stay `1`. The loader stops if a required sleeve is missing.

| Key | Effect |
|-----|--------|
| `single_name_cap_pct` | Soft maximum weight for one liquid name. A breach gets a minimum-dollar cut. |
| `cluster_do_not_increase_pct` | Direct AI and semiconductor sleeve: no buys at or above this weight. |
| `cluster_soft_cap_pct` | Direct AI and semiconductor sleeve soft ceiling. Holding above it is allowed only when a cut would realize a costly short-term gain. |
| `analyze_weight_floor_pct` | Names at or above this weight must appear in the position table. |
| `analyze_market_value_floor` | Minimum market value before a harvest or material-loss name must appear in the table. |
| `material_loss_dollars`, `material_loss_pct` | A sized name past either loss threshold stays in the table, even under the weight floor. |
| `max_focus_names` | Optional. Most names the prompt asks the model to analyze in full. Defaults to 8. The manual `SNPS` entry is added on top. |
| `harvest_loss_dollars` | Taxable unrealized-loss flag. Use a negative number. |
| `risk_off_glide_pct` | Direct-sleeve weight to work toward in Risk-Off, using tax-aware lots. |
| `st_gain_flag_dollars`, `lt_gain_flag_dollars` | Taxable short-term and long-term unrealized-gain flags in the tax section. |
| `cash_symbols` | Cash equivalents for portfolio classification and weights. Names containing `GOVERNMENT` also count. Trade funding uses actual `CASH` or a named same-account sale. |
| `sleeves` | Sleeve membership. The direct AI and semiconductor sleeve is a subset of the broad AI-cycle sleeve. A membership change updates both the math and the prompt labels. |
| `aliases` | Optional display names, for example `CRWV` shown as CoreWeave. |
| `marginal_examples` | Rows in the $10k example table. Set `symbol` to `null` for a generic diversifier. |

To change the prompt's wording, edit `templates/briefing_prompt.md`. `build_briefing_prompt.py` fills each `{placeholder}` with a computed value.

After either change, run the tests:

```powershell
python -m unittest discover -s tests -v
```

## Keep research between days

To give the model context that E\*TRADE can't supply, copy `portfolio_context.example.json` to `portfolio_context.json` and fill it in. Git ignores the file. All sections are optional:

- `owner_profile` holds your horizon, cash reserve, withdrawals, tax facts, and hard limits.
- `research` holds one dated record per held name or recurring candidate: the valuation or entry condition, the decision trigger, metrics, and primary sources.
- `decision_history` holds earlier proposals and their conditions. Record an approval or an execution only when you have evidence of it. A model's recommendation is not an approval. An `ACCEPTED` import appends its proposals here, marked `NOT APPROVED / NOT EXECUTED`.
- `candidate_review_history` records the dates when an imported reply completed a candidate review.

The prompt reports a blank owner profile as "not supplied", and the model flags a missing field only where it would change a trade. Without research notes, the model researches each focus name itself. Update the notes after earnings or a material event.

The prompt asks for a candidate review (new places to put cash) on the first run and when seven days have passed since the last requested review. The build saves the request date in `briefings/candidate_refresh_state.json`, so the weekly review works whether or not you import replies.

## Include stock-plan shares that the API doesn't return

The E\*TRADE API doesn't return the Synopsys stock-plan account. Record those lots in `docs/manual-stock-plan-holdings.md`. Git ignores the file. The build reads its `SNPS` rows and adds a separate supplement to the prompt:

- On a live build, the script fetches a read-only `SNPS` quote and shows the combined total and combined `SNPS` weight next to the API-only totals.
- If the quote fails, or the build is offline, the prompt marks combined exposure as incomplete.
- If an API account reports the same `SNPS` lots, the build doesn't add them again.
- `SNPS` is always a focus name. The manual shares never count as cash for trade funding, and their basis is an estimate, not tax basis.

## Data completeness

The scripts fetch every page of positions and the account balance. The weight denominator is total account value, including residual cash. Each account must reconcile before the scripts build a prompt:

- Positions plus cash match the reported account value, within five basis points to allow for mark timing.
- Each holding's tax lots add up to its share quantity.

A malformed response, a missing or mismatched lot, an account that doesn't reconcile, or a failed account pull stops the build. The scripts never shrink the portfolio silently.

The scripts retry only network errors, throttling (HTTP 429), and server errors (5xx). An expired token (HTTP 401) stops the run with a message to re-authorize.

For diagnostics only, `--allow-partial` makes `get_portfolio.py` or `build_briefing_prompt.py` produce a clearly marked incomplete result. Don't size trades from partial output.

Holding periods come from each lot's acquired date. E\*TRADE doesn't document the lot `termCode` field, so the scripts use it only when the date is missing, and the lot table flags any disagreement. Option positions stay separate from their underlying stock and show at market value. The scripts don't compute option delta exposure.

Lot detail costs a few extra read-only API calls per run, which adds a few seconds.

## Security

The local output is your real portfolio: dollar values, cost basis, lots, and the last four digits of each account number. Treat it that way.

- Git ignores `.env`, `.env.*` (except `.env.example`), `briefings/`, `prompts/`, `responses/`, and `portfolio_context.json`. Before your first push, run `git check-ignore -v .env`. It must print a match.
- Never run `git add -f` on an ignored file. Commit only `.env.example`, with placeholders.
- Never hard-code API keys.
- Keep the GitHub repository private.
- Share the prompt only with a model service you trust to receive your portfolio data. Keep generated files out of public gists and other unintended destinations.
- `etrade_auth.py` saves tokens to `.env` without printing them. Pass `--no-write-env` with `--print-tokens` only when you need to handle tokens by hand.
- The OAuth tokens could authorize E\*TRADE's trading APIs. This code never calls an order endpoint. It uses OAuth login and read-only account, position, tax-lot, balance, and quote requests.

## Files

| File | Purpose |
|------|---------|
| `daily.cmd` | Daily launcher. Authorizes when needed, then builds the live prompt. |
| `daily_briefing.py` | Runs authorization and the live build for `daily.cmd`. |
| `build_briefing_prompt.py` | Builds the prompt from live data or a saved portfolio. |
| `briefing.cmd` | Offline build from the clipboard payload. |
| `get_portfolio.py` | Portfolio dump with cost, P/L, and ST or LT status. |
| `import_response.py` | Validates a model reply against the briefing's portfolio data and saves it. |
| `reply.cmd` | Imports a reply from the clipboard. |
| `vault_notes.py` | Writes the vault note for an imported reply. |
| `etrade_auth.py` | OAuth login and atomic `.env` token update. |
| `templates/briefing_prompt.md` | Prompt wording, with `{placeholders}`. |
| `briefing_snapshots.py` | Daily weight and observation snapshots, and the two deltas. |
| `briefing_formatting.py` | Shared money and weight formatting. |
| `portfolio_policy.json` | Limits, sleeves, harvest thresholds, and $10k examples. |
| `portfolio_policy.py` | Loads and validates `portfolio_policy.json`. |
| `portfolio_context.example.json` | Template for owner constraints, dated research, and earlier proposals. |
| `docs/history-retention.md` | How much briefing history to keep. |
| `.env.example` | Secrets template. |
| `briefings/`, `prompts/`, `responses/` | Local output. Git ignores these folders. |

## Develop

CI runs lint and the test suite on every push. To run both locally:

```powershell
pip install ruff
ruff check .
python -m unittest discover -s tests -v
```
