"""Behavioral coverage for the October 2026 audited workflow improvements."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
import daily_briefing
import get_portfolio as portfolio
import import_response as imp

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 10, 21, 9, 30, tzinfo=ET)


def manual_document() -> str:
    return """# Manual Synopsys stock-plan holdings

| Symbol | Acquired | Benefit type | Shares | Estimated basis/share | Gains status |
|---|---|---|---:|---:|---|
| SNPS | 2023-02-28 | ESPP | 35 | $332.49 | Long term |
| SNPS | 2023-08-31 | ESPP | 35 | $332.49 | Long term |
| SNPS | 2023-09-15 | Restricted stock | 6 | $451.93 | Long term |
| SNPS | 2023-09-15 | Restricted stock | 58 | $451.93 | Long term |
| SNPS | 2024-08-30 | ESPP | 35 | $332.49 | Long term |

Total: 169 shares.

## Dated reference quote

The owner supplied SNPS $472.59 on October 1, 2026. Historical reference only.
"""


def api_results(*holdings):
    return [{
        "label": "Brokerage (…1111)",
        "tax_bucket": "taxable",
        "holdings": list(holdings),
        "total_value": sum(h["market_value"] for h in holdings),
    }]


def holding(symbol, quantity, price, lots=None):
    return {
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "market_value": quantity * price,
        "total_cost": quantity * price,
        "total_gain": 0.0,
        "lots": lots or [],
    }


class ManualSupplementTests(unittest.TestCase):
    def test_five_lots_and_combined_denominator_when_current_price_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.md"
            path.write_text(manual_document(), encoding="utf-8")
            manual = briefing.load_manual_holdings(path)
        self.assertEqual([lot["shares"] for lot in manual["lots"]], [35, 35, 6, 58, 35])
        self.assertEqual(manual["total_shares"], 169)

        results = api_results(holding("SNPS", 10, 500), holding("CASH", 1000, 1))
        text, identity = briefing.format_manual_supplement(manual, results, 6000, AS_OF)
        self.assertIn("169", text)
        self.assertIn("$84,500", text)
        self.assertIn("$90,500", text)
        self.assertIn("98.9%", text)
        self.assertIn("unverified", text.lower())
        self.assertEqual(identity["price"], 500)
        self.assertEqual(identity["price_as_of"], AS_OF.isoformat())

    def test_missing_current_price_marks_combined_exposure_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.md"
            path.write_text(manual_document(), encoding="utf-8")
            manual = briefing.load_manual_holdings(path)
        text, identity = briefing.format_manual_supplement(
            manual, api_results(holding("CASH", 1000, 1)), 1000, AS_OF
        )
        self.assertIn("incomplete", text.lower())
        self.assertIn("current quote", text.lower())
        self.assertNotIn("$472.59", text)
        self.assertIsNone(identity["price"])

    def test_exact_api_coverage_prevents_double_counting(self):
        lots = [
            {"qty": 35, "acquired": datetime(2023, 2, 28, tzinfo=ET)},
            {"qty": 35, "acquired": datetime(2023, 8, 31, tzinfo=ET)},
            {"qty": 6, "acquired": datetime(2023, 9, 15, tzinfo=ET)},
            {"qty": 58, "acquired": datetime(2023, 9, 15, tzinfo=ET)},
            {"qty": 35, "acquired": datetime(2024, 8, 30, tzinfo=ET)},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.md"
            path.write_text(manual_document(), encoding="utf-8")
            manual = briefing.load_manual_holdings(path)
        text, identity = briefing.format_manual_supplement(
            manual, api_results(holding("SNPS", 169, 500, lots)), 84_500, AS_OF
        )
        self.assertIn("already covered", text.lower())
        self.assertEqual(identity["coverage"], "api")
        self.assertNotIn("$169,000", text)

    def test_market_quote_read_uses_market_client_and_retry(self):
        response = {"QuoteResponse": {"quoteData": [{
            "dateTimeUTC": 1792593000,
            "all": {"lastTrade": 501.25},
        }]}}
        market = Mock()
        market.get_quote.return_value = response
        with (patch.object(portfolio, "_credentials", return_value={
                  "consumer_key": "key", "consumer_secret": "secret",
                  "access_token": "token", "access_token_secret": "token-secret",
                  "dev": False,
              }),
              patch.object(portfolio, "check_auth_date") as auth_check,
              patch.object(portfolio.pyetrade, "ETradeMarket", return_value=market) as market_ctor,
              patch.object(portfolio, "_retry_read", side_effect=lambda call: call()) as retry):
            price, timestamp = portfolio.fetch_snps_quote()
        market_ctor.assert_called_once_with("key", "secret", "token", "token-secret", dev=False)
        auth_check.assert_called_once_with()
        market.get_quote.assert_called_once_with(["SNPS"], resp_format="json")
        retry.assert_called_once()
        self.assertEqual(price, 501.25)
        self.assertEqual(timestamp, datetime.fromtimestamp(1792593000, tz=ET))

    def test_quote_parser_accepts_single_quote_data_and_rejects_invalid_values(self):
        parsed = portfolio.parse_snps_quote({"QuoteResponse": {"quoteData": {
            "dateTime": "2026-10-21T09:30:00-04:00",
            "intraday": {"lastTrade": "501.25"},
        }}})
        self.assertEqual(parsed, (501.25, AS_OF))
        legacy_time = datetime(2026, 10, 21, 15, 17, tzinfo=ET)
        parsed = portfolio.parse_snps_quote({"QuoteResponse": {"quoteData": {
            "dateTime": "15:17:00 EDT 10-21-2026",
            "fundamental": {"lastTrade": 502},
        }}})
        self.assertEqual(parsed, (502.0, legacy_time))
        for data in (
            {"QuoteResponse": {"quoteData": {"dateTimeUTC": 1792593000,
                                                "all": {"lastTrade": 0}}}},
            {"QuoteResponse": {"quoteData": {"dateTime": "bad-date",
                                                "fundamental": {"lastTrade": 501}}}},
            {"QuoteResponse": {"quoteData": {"dateTime": "2026-10-21",
                                                "all": {"lastTrade": 501}}}},
            {"QuoteResponse": {"quoteData": {"all": {"lastTrade": 501}}}},
        ):
            with self.assertRaises(portfolio.PortfolioDataError):
                portfolio.parse_snps_quote(data)

    def _build_with_quote(self, results=None, quote_result=None, quote_error=None,
                          live_build=True):
        holdings = [holding("CASH", 1000, 1) | {"weight": 100}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patches = [
                patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"),
                patch.object(briefing, "OUT_DIR", root / "prompts"),
                patch.object(briefing, "load_manual_holdings", return_value={
                    "lots": [{"symbol": "SNPS", "acquired": "2024-01-01",
                              "benefit_type": "ESPP", "shares": 169,
                              "estimated_basis_per_share": 300}],
                    "total_shares": 169,
                }),
            ]
            with patches[0], patches[1], patches[2], \
                 patch.object(briefing, "fetch_snps_quote", return_value=quote_result,
                              side_effect=quote_error) as fetch_quote:
                result = briefing.build_briefing(
                    holdings, [], 1000, AS_OF, "",
                    results=results or api_results(holding("CASH", 1000, 1)),
                    live_build=live_build,
                )
        return result, fetch_quote

    def test_live_quote_updates_combined_totals_weight_and_identity_timestamp(self):
        quote_time = datetime(2026, 10, 21, 9, 15, tzinfo=ET)
        result, fetch_quote = self._build_with_quote(quote_result=(500, quote_time))
        fetch_quote.assert_called_once_with()
        self.assertIn("$84,500", result.prompt)
        self.assertIn("$85,500", result.prompt)
        self.assertIn("98.8%", result.prompt)
        self.assertIn("2026-10-21 09:15 EDT", result.prompt)
        self.assertEqual(result.meta["manual_supplement"]["price"], 500)
        self.assertEqual(result.meta["manual_supplement"]["price_as_of"], quote_time.isoformat())
        self.assertEqual(result.meta["manual_supplement"]["quote_price"], 500)
        self.assertEqual(
            result.meta["manual_supplement"]["quote_timestamp"], quote_time.isoformat()
        )
        changed_result, _ = self._build_with_quote(
            quote_result=(501, datetime(2026, 10, 21, 9, 16, tzinfo=ET)),
        )
        self.assertNotEqual(result.meta["briefing_id"], changed_result.meta["briefing_id"])

    def test_quote_failure_is_nonfatal_and_keeps_exposure_incomplete_with_reason(self):
        result, fetch_quote = self._build_with_quote(
            quote_error=portfolio.AuthExpiredError("expired token"),
        )
        fetch_quote.assert_called_once_with()
        self.assertIn("combined valuation/exposure incomplete", result.prompt.lower())
        self.assertIn("expired token", result.prompt.lower())
        self.assertIn("AuthExpiredError", result.prompt)
        self.assertEqual(result.meta["manual_supplement"]["coverage"], "manual")

    def test_api_price_precedes_quote_and_offline_build_skips_quote(self):
        results = api_results(holding("SNPS", 10, 500), holding("CASH", 1000, 1))
        api_result, fetch_quote = self._build_with_quote(results=results)
        fetch_quote.assert_not_called()
        self.assertEqual(api_result.meta["manual_supplement"]["price"], 500)
        self.assertEqual(api_result.meta["manual_supplement"]["price_as_of"], AS_OF.isoformat())

        covered = api_results(holding(
            "SNPS", 169, 500,
            [{"qty": 169, "acquired": datetime(2024, 1, 1, tzinfo=ET)}],
        ))
        covered_result, fetch_quote = self._build_with_quote(results=covered)
        fetch_quote.assert_not_called()
        self.assertEqual(covered_result.meta["manual_supplement"]["coverage"], "api")

        offline_result, fetch_quote = self._build_with_quote(
            quote_result=(500, AS_OF), live_build=False,
        )
        fetch_quote.assert_not_called()
        self.assertIsNone(offline_result.meta["manual_supplement"]["price"])

    def test_offline_input_file_run_does_not_fetch_quote_or_live_portfolio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_file = root / "portfolio.txt"
            input_file.write_text("portable", encoding="utf-8")
            args = type("Args", (), {
                "from_clipboard": False,
                "input_file": str(input_file),
                "allow_partial": False,
                "allow_stale": False,
                "policy": None,
            })()
            parser = briefing.ArgumentParser()
            with (patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"),
                  patch.object(briefing, "OUT_DIR", root / "prompts"),
                  patch.object(portfolio, "PROJECT_ROOT", root),
                  patch.object(briefing, "parse_portable_portfolio_text", return_value=(
                      "portable", 1000, 1, api_results(holding("CASH", 1000, 1)), AS_OF,
                  )),
                  patch.object(briefing, "now_et", return_value=AS_OF),
                  patch.object(briefing, "fetch_snps_quote") as fetch_quote,
                  patch.object(briefing, "fetch_portfolio_block") as fetch_portfolio,
                  patch.object(briefing, "save_briefing_result",
                               return_value=(root / "dated", root / "latest")),
                  patch.object(briefing, "copy_to_clipboard", return_value=False)):
                self.assertEqual(briefing._run(args, parser), 0)
            fetch_quote.assert_not_called()
            fetch_portfolio.assert_not_called()


class FundingTests(unittest.TestCase):
    def check(self, trades, holdings, total=None):
        results = api_results(*holdings)
        total = total or results[0]["total_value"]
        payload = {"briefing_id": "b", "web_research": True, "names": [], "trades": trades}
        reply = "```json\n" + json.dumps(payload) + "\n```"
        checks, _parsed, computed = imp.validate_reply(
            reply, {"briefing_id": "b", "focus_symbols": []}, results, total, AS_OF
        )
        return [c["code"] for c in checks.items if c["level"] == "ERROR"], computed

    @staticmethod
    def buy(tid, amount, funded_by="CASH"):
        return {"id": tid, "side": "BUY", "account": "…1111", "ticker": "NEW",
                "quantity": 1, "est_price": amount, "lot_acquired": None,
                "funded_by": funded_by}

    @staticmethod
    def sell(tid, symbol, quantity):
        return {"id": tid, "side": "SELL", "account": "…1111", "ticker": symbol,
                "quantity": quantity, "lot_acquired": None, "funded_by": "CASH"}

    def test_real_cash_funds_buy_but_sgov_label_alone_does_not(self):
        errors, computed = self.check([self.buy("b1", 75)], [holding("CASH", 100, 1)])
        self.assertEqual(errors, [])
        self.assertEqual(
            computed["funding_status"], "STRUCTURAL_CHECKS_PASSED_SETTLEMENT_UNKNOWN"
        )

        errors, _ = self.check(
            [self.buy("b1", 75, "SGOV")], [holding("CASH", 10, 1), holding("SGOV", 100, 1)]
        )
        self.assertIn("funding_sale_required", errors)

        missing = self.buy("b1", 25)
        missing.pop("funded_by")
        errors, _ = self.check([missing], [holding("CASH", 100, 1)])
        self.assertIn("funded_by_missing", errors)

    def test_same_account_sgov_sale_funds_once_and_must_be_sufficient(self):
        source = holding("SGOV", 100, 1)
        errors, _ = self.check(
            [self.sell("s1", "SGOV", 80), self.buy("b1", 80, "s1")], [source]
        )
        self.assertEqual(errors, [])

        errors, _ = self.check(
            [self.sell("s1", "SGOV", 80), self.buy("b1", 60, "s1"),
             self.buy("b2", 40, "s1")], [source]
        )
        self.assertIn("funding_overallocated", errors)

        errors, _ = self.check(
            [self.sell("s1", "SGOV", 50), self.buy("b1", 75, "s1")], [source]
        )
        self.assertIn("funding_insufficient", errors)


class ContinuityAndCadenceTests(unittest.TestCase):
    def test_quiet_week_uses_last_completed_candidate_review(self):
        prior = AS_OF.replace(day=20)
        base = briefing.snapshot_dict(
            [], {"grand_total": 0, "cluster_w": 0, "broad_w": 0, "cash_w": 0,
                 "top5_w": 0, "breaches": []}, prior
        )
        observations = {"positions": [], "reviews": []}
        recent = {"candidate_review_history": [{"completed_at": "2026-10-18"}]}
        old = {"candidate_review_history": [{"completed_at": "2026-10-13"}]}
        policy = briefing.briefing_output_policy(
            [], [], [], base | {"as_of": AS_OF.isoformat(), "date": "2026-10-21"},
            base, observations, observations, AS_OF, recent
        )
        self.assertFalse(policy.refresh_candidates)
        policy = briefing.briefing_output_policy(
            [], [], [], base | {"as_of": AS_OF.isoformat(), "date": "2026-10-21"},
            base, observations, observations, AS_OF, old
        )
        self.assertTrue(policy.refresh_candidates)

    def test_quiet_daily_builds_refresh_on_day_one_and_day_eight(self):
        base = briefing.snapshot_dict(
            [], {"grand_total": 0, "cluster_w": 0, "broad_w": 0, "cash_w": 0,
                 "top5_w": 0, "breaches": []}, AS_OF
        )
        observations = {"positions": [], "reviews": []}
        context = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"),
                  patch.object(briefing, "OUT_DIR", root / "prompts"),
                  patch.object(portfolio, "PROJECT_ROOT", root)):
                decisions = []
                for day in range(1, 11):
                    as_of = AS_OF.replace(day=day)
                    prior_date = (datetime(2026, 9, 30) if day == 1
                                  else AS_OF.replace(day=day - 1))
                    prior = base | {"date": prior_date.strftime("%Y-%m-%d")}
                    policy = briefing.briefing_output_policy(
                        [], [], [], prior, prior, observations, observations,
                        as_of, context,
                    )
                    decisions.append(policy.refresh_candidates)
                    result = briefing.BriefingBuildResult(
                        "prompt", base, observations,
                        {"briefing_id": f"2026-10-{day:02d}",
                         "candidate_refresh_requested": policy.refresh_candidates},
                    )
                    briefing.save_briefing_result(result, "", as_of)
                self.assertEqual(
                    [index + 1 for index, refresh in enumerate(decisions) if refresh],
                    [1, 8],
                )

    def test_same_day_rebuild_refreshes_without_advancing_state_date(self):
        base = briefing.snapshot_dict(
            [], {"grand_total": 0, "cluster_w": 0, "broad_w": 0, "cash_w": 0,
                 "top5_w": 0, "breaches": []}, AS_OF
        )
        observations = {"positions": [], "reviews": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"), \
                 patch.object(briefing, "OUT_DIR", root / "prompts"), \
                 patch.object(portfolio, "PROJECT_ROOT", root):
                first = briefing.BriefingBuildResult(
                    "prompt", base, observations,
                    {"briefing_id": "2026-10-21-one",
                     "candidate_refresh_requested": True},
                )
                briefing.save_briefing_result(first, "", AS_OF)
                second_policy = briefing.briefing_output_policy(
                    [], [], [], base, base, observations, observations, AS_OF, {},
                )
                self.assertTrue(second_policy.refresh_candidates)
                second = briefing.BriefingBuildResult(
                    "prompt", base, observations,
                    {"briefing_id": "2026-10-21-two",
                     "candidate_refresh_requested": second_policy.refresh_candidates},
                )
                briefing.save_briefing_result(second, "", AS_OF)
                state = json.loads(
                    (root / "briefings" / "candidate_refresh_state.json").read_text()
                )
        self.assertEqual(state, {"last_requested": "2026-10-21",
                                 "briefing_id": "2026-10-21-two"})

    def test_import_history_and_saved_state_share_the_later_cadence_anchor(self):
        base = briefing.snapshot_dict(
            [], {"grand_total": 0, "cluster_w": 0, "broad_w": 0, "cash_w": 0,
                 "top5_w": 0, "breaches": []}, AS_OF
        )
        observations = {"positions": [], "reviews": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "briefings"
            state_dir.mkdir()
            (state_dir / "candidate_refresh_state.json").write_text(
                json.dumps({"last_requested": "2026-10-18", "briefing_id": "saved"}),
                encoding="utf-8",
            )
            with patch.object(briefing, "BRIEFINGS_DIR", state_dir):
                policy = briefing.briefing_output_policy(
                    [], [], [], base | {"date": "2026-10-20"}, base,
                    observations, observations, AS_OF,
                    {"candidate_review_history": [{"completed_at": "2026-10-20"}]},
                )
        self.assertFalse(policy.refresh_candidates)

    def test_failed_save_does_not_record_candidate_refresh(self):
        result = briefing.BriefingBuildResult(
            "prompt", {"date": "2026-10-21"}, {"date": "2026-10-21"},
            {"briefing_id": "failed", "candidate_refresh_requested": True},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(briefing, "OUT_DIR", root / "prompts"),
                  patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"),
                  patch.object(portfolio, "PROJECT_ROOT", root),
                  patch.object(
                      briefing, "save_weights_snapshot", side_effect=OSError("disk full")
                  )):
                with self.assertRaises(OSError):
                    briefing.save_briefing_result(result, "", AS_OF)
            self.assertFalse((root / "briefings" / "candidate_refresh_state.json").exists())

    def test_decision_day_forces_candidate_refresh(self):
        base = briefing.snapshot_dict(
            [], {"grand_total": 0, "cluster_w": 0, "broad_w": 0, "cash_w": 0,
                 "top5_w": 0, "breaches": []}, AS_OF
        )
        prior_observations = {"positions": [{"symbol": "AAA", "quantity": 10}],
                              "reviews": []}
        today_observations = {"positions": [{"symbol": "AAA", "quantity": 11}],
                              "reviews": []}
        context = {"candidate_review_history": [{"completed_at": "2026-10-20"}]}
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "candidate_refresh_state.json").write_text(
                json.dumps({"last_requested": "2026-10-18", "briefing_id": "recent"}),
                encoding="utf-8",
            )
            with patch.object(briefing, "BRIEFINGS_DIR", state_dir):
                quiet_policy = briefing.briefing_output_policy(
                    [], [], [], base, base, prior_observations, prior_observations,
                    AS_OF, context,
                )
                policy = briefing.briefing_output_policy(
                    [], [], [], base, base, today_observations, prior_observations,
                    AS_OF, context,
                )
        self.assertFalse(quiet_policy.refresh_candidates)
        self.assertTrue(policy.refresh_candidates)

    def test_build_without_save_does_not_create_candidate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"):
                briefing.build_briefing([], [], 0, AS_OF, "")
            self.assertFalse((root / "briefings" / "candidate_refresh_state.json").exists())

    def test_accepted_import_records_once_and_rejected_import_does_not(self):
        parsed = {
            "web_research": True,
            "candidate_review_completed": True,
            "names": [{"ticker": "AAA", "view": "ATTRACTIVE", "action": "KEEP",
                       "trigger": "FCF yield above 4%", "sources": []}],
            "trades": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps({"owner_profile": {"notes": "keep me"},
                                        "research": {"AAA": {"note": "owner note"}},
                                        "decision_history": []}), encoding="utf-8")
            imp.record_accepted_response(
                path, parsed, {"briefing_id": "bid", "as_of": AS_OF.isoformat(),
                               "candidate_refresh_requested": True},
                "chatgpt", "abc123"
            )
            imp.record_accepted_response(
                path, parsed, {"briefing_id": "bid", "as_of": AS_OF.isoformat(),
                               "candidate_refresh_requested": True},
                "chatgpt", "abc123"
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["owner_profile"]["notes"], "keep me")
        self.assertEqual(saved["research"]["AAA"]["note"], "owner note")
        self.assertEqual(len(saved["decision_history"]), 1)
        self.assertEqual(saved["decision_history"][0]["source_briefing_id"], "bid")
        self.assertEqual(saved["decision_history"][0]["approval_state"],
                         "NOT APPROVED / NOT EXECUTED")
        self.assertEqual(len(saved["candidate_review_history"]), 1)

    def test_incomplete_candidate_review_is_not_recorded(self):
        parsed = {
            "web_research": False,
            "candidate_review_completed": True,
            "names": [],
            "trades": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            imp.record_accepted_response(
                path, parsed,
                {"briefing_id": "bid", "as_of": AS_OF.isoformat(),
                 "candidate_refresh_requested": True},
                "chatgpt", "hash"
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("candidate_review_history", saved)

    def test_different_model_replies_keep_provenance(self):
        parsed = {"names": [], "trades": [], "candidate_review_completed": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            imp.record_accepted_response(
                path, parsed, {"briefing_id": "bid", "as_of": AS_OF.isoformat()},
                "chatgpt", "hash-one"
            )
            imp.record_accepted_response(
                path, parsed, {"briefing_id": "bid", "as_of": AS_OF.isoformat()},
                "claude", "hash-two"
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["decision_history"]), 2)
        self.assertEqual({row["engine"] for row in saved["decision_history"]},
                         {"chatgpt", "claude"})


class IdentityAndNewlineTests(unittest.TestCase):
    def test_two_same_day_runs_keep_distinct_artifacts_and_earlier_import(self):
        result1 = briefing.BriefingBuildResult(
            "prompt one\n", {"date": "2026-10-21"}, {"date": "2026-10-21"},
            {"briefing_id": "2026-10-21-one", "as_of": AS_OF.isoformat(),
             "portfolio_file": "briefing_2026-10-21-one_portfolio.txt"}
        )
        result2 = briefing.BriefingBuildResult(
            "prompt two\n", {"date": "2026-10-21"}, {"date": "2026-10-21"},
            {"briefing_id": "2026-10-21-two", "as_of": AS_OF.isoformat(),
             "portfolio_file": "briefing_2026-10-21-two_portfolio.txt"}
        )
        portable = portfolio.PORTABLE_BEGIN + "\n{}\n" + portfolio.PORTABLE_END
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(briefing, "OUT_DIR", root / "prompts"),
                  patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"),
                  patch.object(portfolio, "PROJECT_ROOT", root)):
                briefing.save_briefing_result(result1, portable, AS_OF)
                briefing.save_briefing_result(result2, portable, AS_OF)
            self.assertTrue((root / "prompts" / "daily_briefing_prompt_2026-10-21-one.md").exists())
            self.assertTrue((root / "prompts" / "daily_briefing_prompt_2026-10-21-two.md").exists())
            self.assertEqual(
                json.loads(
                    (root / "briefings" / "briefing_latest.json").read_text()
                )["briefing_id"],
                "2026-10-21-two",
            )

    def test_crlf_and_lf_have_same_saved_content_and_hash(self):
        text_lf = "Analysis\n\n```json\n{}\n```\n"
        self.assertEqual(imp.normalize_newlines(text_lf.replace("\n", "\r\n")), text_lf)
        self.assertEqual(imp.reply_hash(text_lf), imp.reply_hash(text_lf.replace("\n", "\r\n")))


class DailyLauncherTests(unittest.TestCase):
    def test_runs_builder_without_auth_when_current(self):
        with patch.object(daily_briefing, "authorization_needed", return_value=False), \
             patch.object(daily_briefing.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(daily_briefing.main(), 0)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(str(run.call_args.args[0][1]).endswith("build_briefing_prompt.py"))

    def test_auth_failure_stops_before_build(self):
        with patch.object(daily_briefing, "authorization_needed", return_value=True), \
             patch.object(daily_briefing.subprocess, "run") as run:
            run.return_value.returncode = 1
            self.assertEqual(daily_briefing.main(), 1)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(str(run.call_args.args[0][1]).endswith("etrade_auth.py"))

    def test_build_failure_is_returned(self):
        with patch.object(daily_briefing, "authorization_needed", return_value=False), \
             patch.object(daily_briefing.subprocess, "run") as run:
            run.return_value.returncode = 3
            self.assertEqual(daily_briefing.main(), 3)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
