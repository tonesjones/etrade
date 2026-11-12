import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import execution_tracking as ex
from get_portfolio import ET

ACCOUNT = "Brokerage (…1111)"
OTHER = "IRA (…2222)"
DAY = "2026-01-05"
NEXT = datetime(2026, 1, 6, 9, 30, tzinfo=ET)


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(hour=12, tzinfo=ET).timestamp() * 1000)


def tx(kind="Sold", symbol="AAA", qty=-10, price=50.0, day=DAY, tid="t-1"):
    return {
        "transactionId": tid,
        "transactionDate": ms(day),
        "transactionType": kind,
        "brokerage": {"product": {"symbol": symbol}, "quantity": qty, "price": price},
    }


def tx_page(*items, more=False, marker=None):
    body = {"Transaction": list(items), "moreTransactions": "true" if more else "false"}
    if marker:
        body["marker"] = marker
    return {"TransactionListResponse": body}


def result(label=ACCOUNT, holdings=(("AAA", 10),), error=None):
    out = {
        "label": label,
        "account_id_key": f"key-{label[-5:-1]}",
        "holdings": [
            {"symbol": s, "quantity": q, "price": 1.0, "market_value": float(q), "lots": []}
            for s, q in holdings
        ],
    }
    if error:
        out["error"] = error
    return out


def observation(*rows):
    return {"positions": [
        {"account": f"…{tail}", "symbol": s, "quantity": q} for tail, s, q in rows
    ]}


def record(as_of=DAY, briefing="b1", trades=None, state=ex.OPEN_STATE, engine="chatgpt"):
    return {
        "record_id": f"{briefing}:{engine}",
        "source_briefing_id": briefing,
        "as_of": as_of,
        "engine": engine,
        "approval_state": state,
        "proposed_trades": trades if trades is not None else [
            {"id": "t1", "side": "SELL", "ticker": "AAA", "quantity": 10, "account": "…1111"}
        ],
    }


class FakeApi:
    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error
        self.calls = []

    def list_transactions(self, key, **kwargs):
        self.calls.append((key, kwargs))
        if self.error:
            raise self.error
        return self.pages.pop(0)


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.context = self.dir / "portfolio_context.json"

    def baseline(self, *rows, briefing="b1"):
        (self.dir / f"observations_{briefing}.json").write_text(
            json.dumps(observation(*rows)), encoding="utf-8"
        )

    def write_history(self, *records):
        self.context.write_text(
            json.dumps({"decision_history": list(records)}), encoding="utf-8"
        )

    def history(self):
        return json.loads(self.context.read_text(encoding="utf-8"))["decision_history"]

    def run_check(self, results, api=None, when=NEXT):
        return ex.reconcile_context_file(self.context, results, when, self.dir, api)


class ParseTransactionsTests(unittest.TestCase):
    def test_keeps_buys_and_sells_only(self):
        page = tx_page(
            tx(), tx(kind="Bought", symbol="BBB", qty=5, tid="t-2"),
            {"transactionType": "Dividend", "transactionDate": ms(DAY), "brokerage": {}},
        )
        trades, marker = ex.parse_transactions(page)
        self.assertIsNone(marker)
        self.assertEqual(
            [(t["type"], t["symbol"], t["quantity"], t["date"]) for t in trades],
            [("SOLD", "AAA", 10.0, DAY), ("BOUGHT", "BBB", 5.0, DAY)],
        )

    def test_empty_body_and_single_object(self):
        self.assertEqual(ex.parse_transactions({}), ([], None))
        trades, _ = ex.parse_transactions({"TransactionListResponse": {"Transaction": tx()}})
        self.assertEqual(len(trades), 1)

    def test_follows_marker_pages(self):
        api = FakeApi([tx_page(tx(), more=True, marker="m2"), tx_page(tx(tid="t-2"))])
        trades = ex.fetch_transactions(api, "k", date(2026, 1, 5), date(2026, 1, 6))
        self.assertEqual(len(trades), 2)
        self.assertEqual(api.calls[1][1]["marker"], "m2")
        self.assertEqual(api.calls[0][1]["resp_format"], "json")


class ReconcileTests(Fixture):
    def test_transaction_confirms_full_sale(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        api = FakeApi([tx_page(tx(qty=-10, price=50.0))])
        lines, problems = self.run_check([result(holdings=())], api)
        saved = self.history()[0]
        self.assertEqual(saved["approval_state"], ex.CONFIRMED)
        trade = saved["execution"]["trades"][0]
        self.assertEqual((trade["method"], trade["executed_quantity"]), ("transactions", 10.0))
        self.assertEqual(trade["average_price"], 50.0)
        self.assertEqual(problems, [])
        self.assertIn("SELL 10 of 10 AAA in …1111 on 2026-01-05 @ $50.00", lines[0])
        self.assertEqual(api.calls[0][1]["start_date"], date(2026, 1, 5))

    def test_partial_fill(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        self.run_check([result(holdings=(("AAA", 6),))], FakeApi([tx_page(tx(qty=-4))]))
        saved = self.history()[0]
        self.assertEqual(saved["approval_state"], ex.PARTIAL)
        self.assertEqual(saved["execution"]["trades"][0]["executed_quantity"], 4.0)

    def test_transaction_before_proposal_is_ignored(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        self.run_check([result()], FakeApi([tx_page(tx(day="2026-01-02"))]))
        self.assertEqual(self.history()[0]["approval_state"], ex.OPEN_STATE)

    def test_position_change_infers_sale(self):
        self.baseline(("1111", "AAA", 10), ("1111", "CASH", 1))
        self.write_history(record())
        lines, _ = self.run_check([result(holdings=(("CASH", 1),))])
        saved = self.history()[0]
        self.assertEqual(saved["approval_state"], ex.INFERRED)
        self.assertEqual(saved["execution"]["trades"][0]["shares_after"], 0.0)
        self.assertIn("position change only", lines[0])

    def test_transactions_fetch_failure_falls_back_to_positions(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        lines, problems = self.run_check(
            [result(holdings=(("CASH", 1),))], FakeApi(error=RuntimeError("boom"))
        )
        self.assertEqual(self.history()[0]["approval_state"], ex.INFERRED)
        self.assertEqual(len(problems), 1)

    def test_no_change_leaves_record_open_and_file_untouched(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        before = self.context.read_text(encoding="utf-8")
        lines, _ = self.run_check([result()], FakeApi([tx_page()]))
        self.assertEqual(lines, [])
        self.assertEqual(self.context.read_text(encoding="utf-8"), before)

    def test_failed_account_is_never_marked_executed(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        api = FakeApi([tx_page()])
        self.run_check([result(holdings=(), error="timeout")], api)
        self.assertEqual(self.history()[0]["approval_state"], ex.OPEN_STATE)
        self.assertEqual(api.calls, [])

    def test_missing_baseline_needs_a_transaction(self):
        self.write_history(record())
        self.run_check([result(holdings=())])
        self.assertEqual(self.history()[0]["approval_state"], ex.OPEN_STATE)

    def test_buy_matches_bought(self):
        self.baseline(("1111", "CASH", 100))
        trades = [{"id": "t1", "side": "BUY", "ticker": "BBB", "quantity": 5, "account": "1111"}]
        self.write_history(record(trades=trades))
        self.run_check([result()], FakeApi([tx_page(tx(kind="Bought", symbol="BBB", qty=5))]))
        self.assertEqual(self.history()[0]["approval_state"], ex.CONFIRMED)

    def test_other_account_does_not_count(self):
        self.baseline(("1111", "AAA", 10), ("2222", "AAA", 10))
        self.write_history(record())
        self.run_check([result(), result(label=OTHER, holdings=())])
        self.assertEqual(self.history()[0]["approval_state"], ex.OPEN_STATE)

    def test_newer_proposal_supersedes_older(self):
        self.baseline(("1111", "AAA", 10), briefing="b1")
        self.baseline(("1111", "AAA", 10), briefing="b2")
        self.write_history(record(briefing="b1"), record(briefing="b2", engine="codex"))
        self.run_check([result(holdings=())], FakeApi([tx_page(tx())]))
        older, newer = self.history()
        self.assertEqual(older["approval_state"], ex.SUPERSEDED)
        self.assertEqual(newer["approval_state"], ex.CONFIRMED)

    def test_window_closes_after_seven_days(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        late = datetime(2026, 1, 13, 9, 30, tzinfo=ET)
        self.run_check([result(holdings=())], when=late)
        self.assertEqual(self.history()[0]["approval_state"], ex.OPEN_STATE)

    def test_inferred_upgrades_when_transaction_appears(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record())
        self.run_check([result(holdings=())])
        self.assertEqual(self.history()[0]["approval_state"], ex.INFERRED)
        self.run_check([result(holdings=())], FakeApi([tx_page(tx())]))
        self.assertEqual(self.history()[0]["approval_state"], ex.CONFIRMED)

    def test_confirmed_record_is_not_rechecked(self):
        self.baseline(("1111", "AAA", 10))
        self.write_history(record(state=ex.CONFIRMED))
        api = FakeApi([tx_page()])
        lines, _ = self.run_check([result()], api)
        self.assertEqual((lines, api.calls), ([], []))

    def test_no_context_file(self):
        self.assertEqual(self.run_check([result()]), ([], []))


class HistoryPromptTests(unittest.TestCase):
    def test_decision_history_shows_execution(self):
        import build_briefing_prompt as bbp

        item = record(state=ex.CONFIRMED)
        item["execution"] = {"trades": [{
            "side": "SELL", "ticker": "AAA", "account": "…1111", "status": "FULL",
            "method": "transactions", "executed_quantity": 10.0, "proposed_quantity": 10.0,
            "dates": [DAY], "average_price": 50.0,
        }]}
        table = bbp.format_decision_history({"decision_history": [item]})
        self.assertIn("EXECUTED CONFIRMED: SELL 10 of 10 AAA in …1111 on 2026-01-05", table)


if __name__ == "__main__":
    unittest.main()
