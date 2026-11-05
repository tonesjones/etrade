"""Owner trim rules: price history, triggers, sell order, ranking, and import checks."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import test_import_response as tir

import build_briefing_prompt as briefing
import trim_rules as tr
from market_data import Technicals
from portfolio_policy import active_policy, load_policy, set_active_policy

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 11, 5, 9, 30, tzinfo=ET)
ROOT = Path(__file__).resolve().parents[1]


def snap(day, total=1_000_000.0, cluster=30.0, **prices):
    return {
        "date": day,
        "grand_total": total,
        "cluster_w": cluster,
        "holdings": [
            {"symbol": s, "price": p, "market_value": p * 100, "weight": 1.0}
            for s, p in prices.items()
        ],
    }


def days(n, start=1):
    return [f"2026-10-{d:02d}" for d in range(start, start + n)]


def holding(symbol, value, weight, gain=None, gain_pct=None):
    return {
        "symbol": symbol, "market_value": value, "weight": weight, "price": 100.0,
        "total_gain": gain, "total_gain_pct": gain_pct, "lots": [],
    }


def lot(qty, cost, acquired):
    return {"qty": qty, "total_cost": cost, "acquired": datetime.fromisoformat(acquired)}


def account(label, bucket, *holdings):
    return {"label": label, "tax_bucket": bucket, "holdings": list(holdings)}


def position(symbol, qty, price, lots=()):
    return {
        "symbol": symbol, "quantity": qty, "price": price,
        "market_value": qty * price, "lots": list(lots),
    }


class TrimRuleTestCase(unittest.TestCase):
    def setUp(self):
        previous = set_active_policy(load_policy())
        self.addCleanup(set_active_policy, previous)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def rules(self, **changes):
        policy = active_policy()
        set_active_policy(replace(policy, trim_rules=replace(policy.trim_rules, **changes)))

    def review(self, holdings, history, results=(), total=1_000_000.0, cluster_mv=0.0,
               technicals=None):
        return tr.review(holdings, list(results), total, cluster_mv, history, AS_OF, self.root,
                         technicals or {})


class PolicyTests(TrimRuleTestCase):
    def test_trim_rules_load_and_are_optional(self):
        rules = active_policy().trim_rules
        self.assertEqual(rules.sleeve_breach_enforce_days, 5)
        self.assertEqual(rules.rebalance_target_pct, 15.0)
        self.assertEqual(rules.stretch_rsi, 70.0)
        raw = json.loads((ROOT / "portfolio_policy.json").read_text(encoding="utf-8"))
        path = self.root / "policy.json"
        path.write_text(json.dumps({k: v for k, v in raw.items() if k != "trim_rules"}))
        self.assertIsNone(load_policy(path).trim_rules)

    def test_bad_trim_rule_values_are_rejected(self):
        raw = json.loads((ROOT / "portfolio_policy.json").read_text(encoding="utf-8"))
        path = self.root / "policy.json"
        bad = [
            ("sleeve_breach_enforce_days", 2.5),
            ("sleeve_breach_enforce_days", 0),
            ("single_name_hard_cap_pct", "20"),
            ("rebalance_target_pct", 150),
            ("repeat_days", True),
        ]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                rules = dict(raw["trim_rules"], **{key: value})
                path.write_text(json.dumps(raw | {"trim_rules": rules}))
                with self.assertRaisesRegex(ValueError, key):
                    load_policy(path)
        rules = dict(raw["trim_rules"], stretch={"rsi": 120, "above_sma50_pct": 20,
                                                "above_sma200_pct": 40})
        path.write_text(json.dumps(raw | {"trim_rules": rules}))
        with self.assertRaisesRegex(ValueError, "rsi"):
            load_policy(path)


class SignalTests(TrimRuleTestCase):
    def test_drawdown_and_changes_from_history(self):
        prices = [100, 120, 150, 140, 130, 120, 90]
        history = [snap(d, AAA=p) for d, p in zip(days(7), prices, strict=True)]
        signal = tr.position_signals(history)["AAA"]
        self.assertEqual((signal.high, signal.high_date), (150, "2026-10-03"))
        self.assertAlmostEqual(signal.from_high_pct, -40.0)
        self.assertAlmostEqual(signal.change_5, (90 / 120 - 1) * 100)
        self.assertIsNone(signal.change_20)

    def test_split_and_reverse_split_restart_the_series(self):
        for before, after in ((300.0, 100.0), (20.0, 60.0)):
            with self.subTest(before=before, after=after):
                history = [snap("2026-10-01", AAA=before), snap("2026-10-02", AAA=before)]
                history.append(snap("2026-10-03", AAA=after))
                history[-1]["holdings"][0]["market_value"] = before * 100
                signal = tr.position_signals(history)["AAA"]
                self.assertEqual(signal.high, after)
                self.assertEqual(signal.from_high_pct, 0.0)

    def test_a_crash_with_falling_value_is_not_a_split(self):
        history = [snap("2026-10-01", AAA=100.0), snap("2026-10-02", AAA=40.0)]
        self.assertAlmostEqual(tr.position_signals(history)["AAA"].from_high_pct, -60.0)

    def test_portfolio_peak_and_streak(self):
        totals = [1.0, 1.3, 1.2, 1.04]
        clusters = [39.0, 41.0, 45.0, 42.0]
        rows = zip(days(4), totals, clusters, strict=True)
        history = [snap(d, total=t * 1e6, cluster=c) for d, t, c in rows]
        signal = tr.portfolio_signal(history)
        self.assertEqual(signal.peak_date, "2026-10-02")
        self.assertAlmostEqual(signal.drawdown_pct, -20.0)
        self.assertEqual(tr.sleeve_breach_streak(history, 40.0), 3)
        self.assertEqual(tr.sleeve_breach_streak(history[:1], 40.0), 0)

    def test_history_reads_only_earlier_dated_snapshots(self):
        for name, day in (("weights_2026-10-01.json", "2026-10-01"),
                          ("weights_2026-10-02-abcd1234.json", "2026-10-02"),
                          ("weights_latest.json", "2026-10-03"),
                          ("weights_2026-11-05.json", "2026-11-05")):
            (self.root / name).write_text(json.dumps(snap(day)), encoding="utf-8")
        (self.root / "weights_2026-10-04.json").write_text("{broken", encoding="utf-8")
        today = snap("2026-11-05", total=5.0)
        history = tr.load_snapshot_history(self.root, today)
        self.assertEqual([s["date"] for s in history], ["2026-10-01", "2026-11-05"])
        self.assertIs(history[-1], today)


def tech(symbol, rsi=50.0, vs50=0.0, vs200=0.0):
    return Technicals(symbol, 100.0, "2026-11-04", vs50, vs200, rsi, -5.0, 1.0, 2.0)


class TriggerTests(TrimRuleTestCase):
    def test_sleeve_rule_fires_on_day_n_not_n_minus_one(self):
        cluster_mv = 450_000.0
        for breach_days, expected in ((4, False), (5, True)):
            with self.subTest(breach_days=breach_days):
                history = [snap(d, cluster=45.0) for d in days(breach_days)]
                trim = self.review([], history, cluster_mv=cluster_mv)
                fired = [t for t in trim.triggers if t.kind == "SLEEVE"]
                self.assertEqual(bool(fired), expected)
                if fired:
                    self.assertTrue(fired[0].binding)
                    self.assertAlmostEqual(fired[0].min_sell, 50_000.0)

    def test_portfolio_drawdown_alone_requires_nothing(self):
        history = [snap("2026-10-01", total=1.5e6, AAA=100),
                   snap("2026-10-02", total=1.0e6, AAA=40)]
        self.assertEqual(self.review([holding("AAA", 50_000, 5.0)], history).triggers, ())

    def test_hard_cap_cuts_back_to_the_target_and_skips_rebalance(self):
        trim = self.review([holding("AAA", 210_000, 21.0)], [snap("2026-11-05", AAA=100)])
        self.assertEqual([t.kind for t in trim.triggers], ["HARD_CAP"])
        self.assertAlmostEqual(trim.triggers[0].min_sell, 60_000.0)

    def test_rebalance_review_above_target_with_index_exempt(self):
        rows = [holding("AAA", 160_000, 16.0), holding("VOO", 200_000, 20.0),
                holding("BBB", 150_000, 15.0)]
        trim = self.review(rows, [snap("2026-11-05", AAA=100)])
        [item] = trim.triggers
        self.assertEqual((item.kind, item.scope, item.binding, item.min_sell),
                         ("REBALANCE", "AAA", False, 0.0))
        self.assertIn("Selling $10,000 would bring it back to 15%", item.detail)

    def test_stretched_needs_a_gain_and_a_hit(self):
        rows = [holding("UP", 50_000, 5.0, gain=10_000), holding("DOWN", 50_000, 5.0, gain=-1),
                holding("CALM", 50_000, 5.0, gain=10_000), holding("TINY", 4_000, 0.4, gain=1)]
        technicals = {
            "UP": tech("UP", rsi=75, vs200=45), "DOWN": tech("DOWN", rsi=80),
            "CALM": tech("CALM", rsi=69.9, vs50=19.9, vs200=39.9), "TINY": tech("TINY", rsi=90),
        }
        trim = self.review(rows, [snap("2026-11-05")], technicals=technicals)
        [item] = trim.triggers
        self.assertEqual((item.kind, item.scope), ("STRETCHED", "UP"))
        self.assertIn("RSI 75 (≥ 70)", item.detail)
        self.assertIn("+45% vs 200-day (≥ 40%)", item.detail)

    def test_reviews_wait_out_the_repeat_window(self):
        rows = [holding("AAA", 160_000, 16.0, gain=10_000)]
        technicals = {"AAA": tech("AAA", rsi=80)}
        first = self.review(rows, [snap("2026-11-05")], technicals=technicals)
        self.assertEqual([t.kind for t in first.triggers], ["REBALANCE", "STRETCHED"])
        meta = {"rule_triggers": [t.to_meta() for t in first.triggers]}
        for fired_on, expected in (("2026-11-01", 0), ("2026-10-29", 2)):
            with self.subTest(fired_on=fired_on):
                path = self.root / f"briefing_{fired_on}.json"
                path.write_text(json.dumps(meta))
                again = self.review(rows, [snap("2026-11-05")], technicals=technicals)
                self.assertEqual(len(again.triggers), expected)
                path.unlink()

    def test_rules_off_without_trim_rules(self):
        set_active_policy(replace(active_policy(), trim_rules=None))
        trim = self.review([holding("AAA", 300_000, 30.0)], [snap("2026-11-05", AAA=1)])
        self.assertEqual(trim.triggers, ())
        self.assertIn("Trim rules are off", tr.format_rule_triggers(trim))


class FormatTests(TrimRuleTestCase):
    def test_technicals_table_and_market_lines(self):
        rows = [holding("AAA", 50_000, 5.0)]
        technicals = {"AAA": tech("AAA", rsi=72, vs50=12), "SPY": tech("SPY", vs200=8)}
        trim = self.review(rows, [snap("2026-11-05", AAA=100)], technicals=technicals)
        text = tr.format_risk_signals(trim, rows)
        self.assertIn("- **SPY:** +0.0% vs 50-day, +8.0% vs 200-day, RSI 50", text)
        self.assertIn("| AAA | +12.0% | +0.0% | 72 |", text)
        self.assertNotIn("Tracked high", text)

    def test_snapshot_fallback_and_missing_note(self):
        rows = [holding("AAA", 50_000, 5.0)]
        history = [snap("2026-10-01", AAA=100), snap("2026-11-05", AAA=80)]
        trim = tr.review(rows, [], 1e6, 0.0, history, AS_OF, self.root, {},
                         ["TIINGO_API_KEY is not set in .env"])
        text = tr.format_risk_signals(trim, rows)
        self.assertIn("Technicals missing: TIINGO_API_KEY is not set in .env.", text)
        self.assertIn("| AAA | $80.00 | $100.00 (2026-10-01) | -20.0%", text)


class SellOrderTests(TrimRuleTestCase):
    def results(self):
        taxable = account(
            "Brokerage (…1111)", "taxable",
            position("AAA", 30, 100.0, [
                lot(10, 500.0, "2024-01-02T12:00:00-05:00"),   # LT gain 500
                lot(10, 900.0, "2026-09-01T12:00:00-04:00"),   # ST gain 100
                lot(10, 1500.0, "2026-08-01T12:00:00-04:00"),  # ST loss -500
            ]),
        )
        roth = account("Roth IRA (…2222)", "roth", position("AAA", 5, 100.0))
        ira = account("IRA (…3333)", "traditional", position("AAA", 5, 100.0))
        return [taxable, roth, ira]

    def test_untaxed_then_losses_then_cheapest_tax(self):
        legs = tr.candidate_legs(self.results(), ["AAA"], AS_OF, active_policy().trim_rules)
        self.assertEqual(
            [(leg.tax_bucket, leg.lot_acquired) for leg in legs],
            # Tax per dollar sold: loss -16.7%, ST lot 3.3%, LT lot 12.2%.
            [("traditional", None), ("roth", None), ("taxable", "2026-08-01"),
             ("taxable", "2026-09-01"), ("taxable", "2024-01-02")],
        )
        self.assertAlmostEqual(legs[2].tax, -500 * 0.333)
        self.assertAlmostEqual(legs[4].tax, 500 * 0.243)

    def test_plan_covers_the_minimum_in_whole_shares(self):
        legs = tr.candidate_legs(self.results(), ["AAA"], AS_OF, active_policy().trim_rules)
        plan = tr.sell_plan(legs, 2_050.0)
        self.assertEqual([leg.quantity for leg in plan], [5, 5, 10, 1])
        self.assertAlmostEqual(sum(leg.value for leg in plan), 2_100.0)
        self.assertEqual(plan[-1].term, "ST")
        self.assertAlmostEqual(plan[-1].tax, 10 * 0.333)

    def test_break_even_drop_in_the_block(self):
        rows = [holding("AAA", 4_000, 25.0)]
        trim = self.review(rows, [snap("2026-11-05", AAA=100)], self.results(), total=16_000)
        text = tr.format_rule_triggers(trim)
        self.assertIn("R1 HARD_CAP", text)
        self.assertIn("IRA …3333 AAA: 5 sh $500 (no tax)", text)
        self.assertIn("Lowest-tax order:", text)


class RankingTests(TrimRuleTestCase):
    def test_reviews_outrank_small_losses(self):
        rows = [
            holding("LOSS", 6_000, 0.6, gain=-6_000, gain_pct=-50.0),
            holding("BIG", 160_000, 16.0, gain=60_000),
            holding("HOT", 60_000, 6.0, gain=20_000),
        ]
        trim = self.review(rows, [snap("2026-11-05")], technicals={"HOT": tech("HOT", rsi=80)})
        set_active_policy(replace(active_policy(), max_focus_names=2))
        policy = briefing.briefing_output_policy(
            rows, [{"symbol": "LOSS"}], [], {"holdings": []}, None,
            {"positions": []}, None, AS_OF, None, trim,
        )
        self.assertEqual(set(policy.material_symbols), {"BIG", "HOT"})
        self.assertIn("LOSS", policy.monitor_symbols)


class RuleResponseImportTests(unittest.TestCase):
    setUp = tir.ImportResponseTests.setUp
    write_briefing = tir.ImportResponseTests.write_briefing
    validate = tir.ImportResponseTests.validate
    codes = staticmethod(tir.ImportResponseTests.codes)

    def fire(self, min_sell=250.0, kind="HARD_CAP", regime_required=False):
        path = self.briefings / "briefing_latest.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["rule_triggers"] = [] if kind is None else [{
            "rule_id": "R1", "kind": kind, "scope": "AAA", "symbols": ["AAA"],
            "min_sell": min_sell, "detail": "", "binding": kind in tr.BINDING_KINDS,
        }]
        meta["regime_required"] = regime_required
        path.write_text(json.dumps(meta), encoding="utf-8")

    def reply(self, trades=(), responses=None, web_research=True, regime=None):
        text = tir.reply_text(list(trades), web_research=web_research)
        if responses is None and regime is None:
            return text
        payload = json.loads(tir.imp.extract_json_block(text))
        payload["rule_responses"] = responses or []
        if regime is not None:
            payload["market_regime"] = regime
        return "```json\n" + json.dumps(payload) + "\n```\n"

    def test_no_triggers_keeps_old_behavior(self):
        checks, _parsed, _computed = self.validate(self.reply())
        self.assertEqual(self.codes(checks), [])

    def test_keep_without_a_response_is_rejected(self):
        self.fire()
        checks, _parsed, _computed = self.validate(self.reply())
        self.assertIn("rule_response_missing", self.codes(checks, "ERROR"))

    def test_comply_must_sell_the_minimum(self):
        self.fire()
        comply = [{"rule": "R1", "response": "COMPLY"}]
        checks, _p, _c = self.validate(self.reply([tir.sell(quantity=2)], comply))
        self.assertIn("rule_trim_too_small", self.codes(checks, "ERROR"))
        checks, _p, _c = self.validate(self.reply([tir.sell(quantity=3)], comply))
        self.assertNotIn("rule_trim_too_small", self.codes(checks))

    def test_review_answers_are_trim_or_dated_hold(self):
        self.fire(min_sell=0.0, kind="STRETCHED")
        trim = [{"rule": "R1", "response": "TRIM"}]
        checks, _p, _c = self.validate(self.reply([], trim))
        self.assertIn("rule_trim_too_small", self.codes(checks, "ERROR"))
        checks, _p, _c = self.validate(self.reply([tir.sell(quantity=1)], trim))
        self.assertEqual(self.codes(checks, "ERROR"), [])
        hold = [{"rule": "R1", "response": "HOLD", "reason": "momentum"}]
        checks, _p, _c = self.validate(self.reply([], hold))
        self.assertIn("rule_override_undated", self.codes(checks, "ERROR"))
        hold[0]["reason"] = "2026-09-17 raised guidance, company release"
        checks, _p, _c = self.validate(self.reply([], hold))
        self.assertEqual(self.codes(checks, "ERROR"), [])
        comply = [{"rule": "R1", "response": "COMPLY"}]
        checks, _p, _c = self.validate(self.reply([tir.sell(quantity=1)], comply))
        self.assertIn("rule_response_invalid", self.codes(checks, "ERROR"))

    def test_market_regime_required_when_asked(self):
        self.fire(kind=None, regime_required=True)
        checks, _p, _c = self.validate(self.reply())
        self.assertIn("market_regime_missing", self.codes(checks, "ERROR"))
        checks, _p, _c = self.validate(self.reply(regime={"regime": "SIDEWAYS"}))
        self.assertIn("market_regime_missing", self.codes(checks, "ERROR"))
        checks, _p, _c = self.validate(self.reply(regime={"regime": "neutral"}))
        self.assertEqual(self.codes(checks), [])
        checks, _p, _c = self.validate(self.reply(regime={"regime": "RISK_OFF"}))
        self.assertIn("risk_off_without_sleeve_sale", self.codes(checks, "WARN"))

    def test_override_needs_a_dated_reason(self):
        self.fire()
        undated = [{"rule": "R1", "response": "OVERRIDE", "reason": "thesis intact"}]
        checks, _p, _c = self.validate(self.reply([], undated))
        self.assertIn("rule_override_undated", self.codes(checks, "ERROR"))
        dated = [{"rule": "r1", "response": "override",
                  "reason": "2026-09-17 guidance raise, company release"}]
        checks, _p, _c = self.validate(self.reply([], dated))
        self.assertEqual(self.codes(checks, "ERROR"), [])

    def test_invalid_and_unknown_responses(self):
        self.fire()
        bad = [{"rule": "R1", "response": "HOLD"}, {"rule": "R9", "response": "COMPLY"}]
        checks, _p, _c = self.validate(self.reply([], bad))
        self.assertIn("rule_response_invalid", self.codes(checks, "ERROR"))
        self.assertIn("rule_response_unknown", self.codes(checks, "WARN"))

    def test_no_research_defers_rules_with_a_warning(self):
        self.fire()
        no_names = [{"ticker": t, "view": "INSUFFICIENT_EVIDENCE", "action": "NO_ACTION"}
                    for t in ("AAA", "BBB")]
        text = tir.reply_text([], web_research=False, names_list=no_names)
        checks, _p, _c = self.validate(text)
        self.assertEqual(self.codes(checks, "ERROR"), [])
        self.assertIn("rule_responses_deferred", self.codes(checks, "WARN"))

    def test_override_reason_reaches_the_decision_history(self):
        parsed = {"names": [], "rule_responses": [
            {"rule": "R1", "response": "OVERRIDE", "reason": "2026-09-17 contract signed"}
        ]}
        tir.imp.record_accepted_response(
            self.context, parsed, {"briefing_id": "b", "as_of": "2026-09-18"}, "claude", "h"
        )
        entry = json.loads(self.context.read_text(encoding="utf-8"))["decision_history"][-1]
        self.assertIn("R1 OVERRIDE", entry["portfolio_action"])
        self.assertIn("2026-09-17 contract signed", entry["rationale"])


if __name__ == "__main__":
    unittest.main()
