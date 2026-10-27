"""Focus limits, material snapshot changes, and candidate cadence."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
from portfolio_policy import active_policy, load_policy, set_active_policy

AS_OF = datetime(2026, 10, 27, 9, 30, tzinfo=ZoneInfo("America/New_York"))
ROOT = Path(__file__).resolve().parents[1]


def holding(symbol, market_value, weight=3.0, **overrides):
    row = {
        "symbol": symbol, "market_value": market_value, "weight": weight,
        "price": 100.0, "quantity": market_value / 100.0, "lots": [],
        "total_gain": None, "total_gain_pct": None, "total_cost": None,
        "cost_per_share": None, "price_paid": None, "alias": None,
        "tax_buckets": {"taxable"}, "taxable_mv": market_value,
        "ira_mv": 0.0, "roth_mv": 0.0,
    }
    row.update(overrides)
    return row


class FocusCapTests(unittest.TestCase):
    def setUp(self):
        previous = set_active_policy(load_policy())
        self.addCleanup(set_active_policy, previous)
        directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = patch.object(briefing, "BRIEFINGS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "candidate_refresh_state.json").write_text(
            json.dumps({"last_requested": "2026-10-26"}), encoding="utf-8"
        )

    def policy(self, today, prior, observation=None, prior_observation=None,
               holdings=(), harvest=(), breaches=()):
        return briefing.briefing_output_policy(
            list(holdings), list(harvest), list(breaches), today, prior,
            observation or {"positions": []}, prior_observation or {"positions": []},
            AS_OF,
        )

    def test_appear_and_disappear_use_market_value_floor(self):
        for value in (300.0, 4999.99, 5000.0, 6000.0):
            for appearing in (True, False):
                with self.subTest(value=value, appearing=appearing):
                    row = holding("SPCX", value, weight=0.1)
                    empty = {"holdings": [], "date": "2026-10-26"}
                    populated = {"holdings": [row], "date": "2026-10-27"}
                    today, prior = (populated, empty) if appearing else (empty, populated)
                    observation = {"positions": [row]}
                    empty_observation = {"positions": []}
                    current_obs, prior_obs = (
                        (observation, empty_observation) if appearing
                        else (empty_observation, observation)
                    )
                    expected = {"SPCX"} if value >= 5000 else set()
                    self.assertEqual(briefing._changed_weight_symbols(today, prior), expected)
                    self.assertEqual(
                        briefing._changed_observation_symbols(current_obs, prior_obs), expected
                    )
                    policy = self.policy(today, prior, current_obs, prior_obs)
                    self.assertEqual(policy.mode, "DECISION DAY" if expected else "ROUTINE DAY")
                    delta = briefing.format_daily_delta(today, prior, "prior")
                    self.assertEqual("**SPCX**" in delta, bool(expected))
                    audit = briefing.format_observed_delta(current_obs, prior_obs, "prior")
                    event = "POSITION_APPEARED" if appearing else "POSITION_DISAPPEARED"
                    self.assertIn(event, audit)
                    self.assertIn("SPCX", audit)

    def test_existing_position_changes_use_either_snapshot_value(self):
        for old_value, new_value in ((300, 400), (5000, 400), (300, 5000)):
            for lot_change in (False, True):
                with self.subTest(old_value=old_value, new_value=new_value, lots=lot_change):
                    old = holding("AAA", old_value, weight=1.0, quantity=10)
                    new = holding("AAA", new_value, weight=2.0, quantity=10 if lot_change else 11)
                    if lot_change:
                        new["lots"] = [{"acquired": "2026-10-26", "quantity": 10}]
                    today, prior = {"holdings": [new]}, {"holdings": [old]}
                    current_obs, prior_obs = {"positions": [new]}, {"positions": [old]}
                    expected = {"AAA"} if max(old_value, new_value) >= 5000 else set()
                    self.assertEqual(briefing._changed_weight_symbols(today, prior), expected)
                    self.assertEqual(
                        briefing._changed_observation_symbols(current_obs, prior_obs), expected
                    )
                    self.assertEqual(
                        "**AAA**" in briefing.format_daily_delta(today, prior, "prior"),
                        bool(expected),
                    )

    def test_observation_floor_combines_accounts_per_snapshot(self):
        old = [{"symbol": "AAA", "account_ref": account, "market_value": 2500,
                "quantity": 10} for account in ("one", "two")]
        new = [old[0] | {"quantity": 11}, old[1]]
        self.assertEqual(
            briefing._changed_observation_symbols({"positions": new}, {"positions": old}),
            {"AAA"},
        )
        self.assertEqual(
            briefing._changed_observation_symbols(
                {"positions": [new[0]]}, {"positions": [old[0]]}
            ), set(),
        )

    def test_decision_day_does_not_advance_weekly_candidates(self):
        today = {"holdings": [holding("AAA", 5000)]}
        prior = {"holdings": []}
        for days_ago, expected in ((0, True), (1, False), (6, False), (7, True)):
            with self.subTest(days_ago=days_ago):
                (self.root / "candidate_refresh_state.json").write_text(
                    json.dumps({"last_requested": f"2026-10-{27 - days_ago}"}), encoding="utf-8"
                )
                policy = self.policy(today, prior)
                self.assertEqual(policy.mode, "DECISION DAY")
                self.assertEqual(policy.refresh_candidates, expected)

    def test_focus_cap_ranks_reasons_then_value_then_symbol(self):
        holdings = [
            holding("SMALL", 70_000, 7), holding("ZSIZE", 80_000, 8),
            holding("ASIZE", 80_000, 8), holding("LARGE", 90_000, 9),
            holding("WEIGHT", 40_000, 4), holding("QTY", 30_000, 3),
            holding("LOSS", 20_000, 2, total_gain=-6000),
            holding("TAX", 10_000, 1, total_gain=-6000),
            holding("NEAR", 140_000, 14), holding("ABOVE", 160_000, 16),
        ]
        prior_holdings = [h | {"weight": 3.5} if h["symbol"] == "WEIGHT" else h
                          for h in holdings]
        prior_positions = [h | {"quantity": 1} for h in holdings]
        positions = [h | {"quantity": 2} if h["symbol"] == "QTY" else h
                     for h in prior_positions]
        ranked = ["ABOVE", "NEAR", "TAX", "LOSS", "QTY", "WEIGHT",
                  "LARGE", "ASIZE", "ZSIZE", "SMALL"]
        for limit in range(1, 9):
            with self.subTest(limit=limit):
                set_active_policy(replace(active_policy(), max_focus_names=limit))
                policy = self.policy(
                    {"holdings": holdings}, {"holdings": prior_holdings},
                    {"positions": positions}, {"positions": prior_positions}, holdings,
                    [{"symbol": "TAX"}], [{"symbol": "ABOVE"}],
                )
                self.assertEqual(set(policy.material_symbols), set(ranked[:limit]))
                self.assertEqual(len(policy.focus_reasons), limit)
                self.assertEqual(policy.monitor_symbols, tuple(ranked[limit:]))
        self.assertEqual(
            [symbol for symbol, _ in policy.focus_reasons],
            ["ABOVE", "NEAR", "LARGE", "ASIZE", "WEIGHT", "QTY", "LOSS", "TAX"],
        )
        block = briefing.format_focus_block(policy)
        self.assertIn(
            "deferred from focus today: ZSIZE (weight ≥ 2.5%), SMALL (weight ≥ 2.5%)", block
        )
        self.assertNotIn("**ZSIZE**", block)

    def test_plain_monitor_names_are_separate_from_deferred_reasons(self):
        holdings = [holding("AAA", 20_000, 20), holding("BBB", 18_000, 18),
                    holding("VOO", 10_000, 10)]
        set_active_policy(replace(active_policy(), max_focus_names=1))
        snap = {"holdings": holdings, "breaches": ["AAA", "BBB"]}
        policy = self.policy(snap, snap, holdings=holdings, breaches=holdings[:2])
        self.assertEqual(policy.monitor_symbols, ("VOO", "BBB"))
        self.assertIn(
            "): VOO; deferred from focus today: BBB (above 15% single-name limit)",
            briefing.format_focus_block(policy),
        )

    def test_build_metadata_only_requires_kept_focus_plus_manual_snps(self):
        holdings = [holding(f"NAME{i}", 30_000 - i * 1000) for i in range(10)]
        manual = {"total_shares": 1, "lots": [{
            "symbol": "SNPS", "acquired": "2023-01-01", "benefit_type": "ESPP",
            "shares": 1, "estimated_basis_per_share": 100,
        }]}
        with (patch.object(briefing, "load_portfolio_context", return_value=({}, "")),
              patch.object(briefing, "load_manual_holdings", return_value=manual),
              patch.object(briefing, "load_prior_snapshot", return_value=(None, "")),
              patch.object(briefing, "load_prior_observation", return_value=(None, ""))):
            result = briefing.build_briefing(holdings, [], 1_000_000, AS_OF, "")
        expected = [f"NAME{i}" for i in range(8)] + ["SNPS"]
        self.assertEqual(result.meta["focus_symbols"], expected)
        self.assertEqual(result.meta["monitor_symbols"], ["NAME8", "NAME9"])
        focus = result.prompt.split("**Focus names** (analyze every one):")[1].split(
            "**Changes since last snapshot:**"
        )[0]
        for symbol in expected:
            self.assertIn(f"- **{symbol}**", focus)
        self.assertIn("deferred from focus today: NAME8", focus)

    def test_old_policy_defaults_to_eight_and_rejects_invalid_caps(self):
        raw = json.loads((ROOT / "portfolio_policy.json").read_text(encoding="utf-8"))
        raw.pop("max_focus_names")
        path = self.root / "policy.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(load_policy(path).max_focus_names, 8)
        self.assertEqual(load_policy(path).schema_version, 1)
        path.write_text(json.dumps(raw | {"max_focus_names": 1}), encoding="utf-8")
        self.assertEqual(load_policy(path).max_focus_names, 1)
        for value in (0, -1, 1.5, "8", True, None):
            with self.subTest(value=value):
                path.write_text(json.dumps(raw | {"max_focus_names": value}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "max_focus_names"):
                    load_policy(path)


if __name__ == "__main__":
    unittest.main()
