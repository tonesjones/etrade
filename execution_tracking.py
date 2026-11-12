"""Match recorded proposals against what the owner actually traded.

An accepted import appends the model's trades to ``decision_history`` as
``NOT APPROVED / NOT EXECUTED``. Each live build checks the open ones against
two kinds of evidence and updates them in place:

* E*TRADE transactions: a ``Sold`` or ``Bought`` in the proposal's account and
  symbol, dated on or after the proposal. This confirms an execution.
* Position changes: the share count in that account against the observation
  snapshot saved with the proposal's briefing. A transfer or split looks the
  same, so this only infers an execution.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from briefing_snapshots import observation_snapshot_dict
from get_portfolio import (
    PortfolioDataError,
    _first,
    _retry_read,
    account_tail,
    atomic_write_text,
    parse_etrade_date,
)

OPEN_STATE = "NOT APPROVED / NOT EXECUTED"
CONFIRMED = "EXECUTED CONFIRMED"
INFERRED = "EXECUTED (INFERRED FROM POSITIONS)"
PARTIAL = "PARTIALLY EXECUTED"
SUPERSEDED = "SUPERSEDED"
RECHECK_STATES = {OPEN_STATE, INFERRED, PARTIAL}
CHECK_WINDOW_DAYS = 7
QTY_TOLERANCE = 1e-4
TRANSACTION_PAGE_SIZE = 50
TRANSACTION_MAX_PAGES = 20
SIDE_TYPES = {"SELL": "SOLD", "BUY": "BOUGHT"}


def tail4(raw) -> str:
    return re.sub(r"\D", "", str(raw or ""))[-4:]


def parse_transactions(response) -> tuple[list[dict], str | None]:
    """Normalize one transactions page; return (trades, next marker)."""
    if not response:
        return [], None
    try:
        body = response["TransactionListResponse"]
    except (KeyError, TypeError) as exc:
        raise PortfolioDataError("Unrecognized transactions response") from exc
    raw = body.get("Transaction") or []
    if isinstance(raw, dict):
        raw = [raw]
    trades = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("transactionType") or "").strip().upper()
        if kind not in SIDE_TYPES.values():
            continue
        brokerage = item.get("brokerage") or {}
        product = brokerage.get("product") or {}
        symbol = str(
            _first(product, "symbol") or _first(brokerage, "displaySymbol") or ""
        ).strip().upper()
        when = parse_etrade_date(item.get("transactionDate"))
        try:
            quantity = abs(float(brokerage.get("quantity")))
        except (TypeError, ValueError):
            continue
        if not symbol or when is None or not quantity:
            continue
        try:
            price = float(brokerage.get("price"))
        except (TypeError, ValueError):
            price = None
        trades.append({
            "id": str(item.get("transactionId") or ""),
            "type": kind,
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "date": when.date().isoformat(),
        })
    more = str(body.get("moreTransactions") or "").lower() == "true"
    marker = body.get("marker") or body.get("next")
    return trades, (str(marker) if more and marker else None)


def fetch_transactions(accounts_api, account_id_key: str, start: date, end: date) -> list[dict]:
    """READ: buy and sell transactions for one account between two dates."""
    trades: list[dict] = []
    marker = None
    for _page in range(TRANSACTION_MAX_PAGES):
        response = _retry_read(lambda m=marker: accounts_api.list_transactions(
            account_id_key, start_date=start, end_date=end, marker=m,
            count=TRANSACTION_PAGE_SIZE, resp_format="json",
        ))
        page, marker = parse_transactions(response)
        trades.extend(page)
        if not marker:
            return trades
    raise PortfolioDataError("Transactions paging did not terminate")


def open_records(history: list, today: date) -> list[dict]:
    """Records still worth checking: open state, trades, inside the window."""
    found = []
    for item in history:
        if not isinstance(item, dict) or item.get("approval_state") not in RECHECK_STATES:
            continue
        if not item.get("proposed_trades"):
            continue
        try:
            as_of = date.fromisoformat(str(item.get("as_of") or "")[:10])
        except ValueError:
            continue
        if as_of <= today <= as_of + timedelta(days=CHECK_WINDOW_DAYS):
            found.append(item)
    return found


def transactions_by_account(
    accounts_api, results: list[dict], history: list, today: date
) -> tuple[dict[str, list[dict]], list[str]]:
    """Fetch transactions for each account an open proposal trades in.

    Returns ({account tail: trades}, problems). An account whose fetch fails is
    left out, so its proposals fall back to position evidence.
    """
    records = open_records(history, today)
    wanted = {
        tail4(trade.get("account"))
        for record in records
        for trade in record.get("proposed_trades") or []
        if isinstance(trade, dict)
    }
    if not wanted:
        return {}, []
    start = min(date.fromisoformat(str(r["as_of"])[:10]) for r in records)
    found: dict[str, list[dict]] = {}
    problems = []
    for result in results:
        tail = tail4(account_tail(result.get("label") or ""))
        if tail not in wanted or result.get("error") or not result.get("account_id_key"):
            continue
        try:
            found[tail] = fetch_transactions(
                accounts_api, result["account_id_key"], start, today
            )
        except Exception as exc:  # noqa: BLE001 - any failure falls back to positions
            problems.append(f"{account_tail(result.get('label') or '')}: {exc}")
    return found, problems


def load_baseline(briefings_dir: Path, briefing_id) -> dict | None:
    if not briefing_id:
        return None
    path = briefings_dir / f"observations_{briefing_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _position_qty(observation: dict, tail: str, symbol: str) -> float:
    return sum(
        float(row.get("quantity") or 0.0)
        for row in observation.get("positions") or []
        if tail4(row.get("account")) == tail and str(row.get("symbol") or "").upper() == symbol
    )


def _account_known(observation: dict | None, tail: str) -> bool:
    return bool(observation) and any(
        tail4(row.get("account")) == tail for row in observation.get("positions") or []
    )


def _fill(done: float, proposed: float) -> str:
    if done >= proposed - QTY_TOLERANCE:
        return "FULL"
    return "PARTIAL" if done > QTY_TOLERANCE else "NONE"


def match_trade(
    trade: dict,
    since: str,
    transactions: dict[str, list[dict]],
    baseline: dict | None,
    today_observation: dict,
    live_tails: set[str],
) -> dict:
    """Evidence that one proposed trade was placed."""
    side = str(trade.get("side") or "").upper()
    symbol = str(trade.get("ticker") or "").upper().strip()
    tail = tail4(trade.get("account"))
    try:
        proposed = float(trade.get("quantity"))
    except (TypeError, ValueError):
        proposed = 0.0
    row = {
        "id": trade.get("id"),
        "side": side,
        "ticker": symbol,
        "account": f"…{tail}" if tail else str(trade.get("account") or ""),
        "proposed_quantity": proposed,
        "status": "NONE",
        "method": None,
    }
    if side not in SIDE_TYPES or not symbol or not tail or proposed <= 0:
        row["status"] = "UNCHECKED"
        return row

    if tail in transactions:
        hits = [
            t for t in transactions[tail]
            if t["symbol"] == symbol and t["type"] == SIDE_TYPES[side] and t["date"] >= since
        ]
        done = sum(t["quantity"] for t in hits)
        if done > QTY_TOLERANCE:
            priced = [t for t in hits if t["price"] is not None]
            row.update({
                "status": _fill(done, proposed),
                "method": "transactions",
                "executed_quantity": done,
                "dates": sorted({t["date"] for t in hits}),
                "transaction_ids": [t["id"] for t in hits if t["id"]],
            })
            if priced:
                shares = sum(t["quantity"] for t in priced)
                row["average_price"] = round(
                    sum(t["quantity"] * t["price"] for t in priced) / shares, 4
                )
            return row

    if tail not in live_tails or not _account_known(baseline, tail):
        row["status"] = "UNCHECKED"
        return row
    before = _position_qty(baseline, tail, symbol)
    after = _position_qty(today_observation, tail, symbol)
    change = (before - after) if side == "SELL" else (after - before)
    if change > QTY_TOLERANCE:
        row.update({
            "status": _fill(change, proposed),
            "method": "positions",
            "executed_quantity": change,
            "shares_before": before,
            "shares_after": after,
        })
    return row


def _record_state(rows: list[dict]) -> str | None:
    """New approval state for a record, or None to leave it open."""
    checked = [r for r in rows if r["status"] != "UNCHECKED"]
    if not checked or all(r["status"] == "NONE" for r in checked):
        return None
    if len(checked) == len(rows) and all(r["status"] == "FULL" for r in rows):
        if all(r["method"] == "transactions" for r in rows):
            return CONFIRMED
        return INFERRED
    return PARTIAL


def _trade_keys(record: dict) -> set[tuple[str, str, str]]:
    return {
        (
            tail4(t.get("account")),
            str(t.get("ticker") or "").upper(),
            str(t.get("side") or "").upper(),
        )
        for t in record.get("proposed_trades") or []
        if isinstance(t, dict)
    }


def reconcile(
    history: list,
    today_observation: dict,
    transactions: dict[str, list[dict]],
    briefings_dir: Path,
    live_tails: set[str],
    today: date,
) -> list[dict]:
    """Update open records in place; return the ones whose state changed.

    ``live_tails`` are the accounts loaded without error today; position
    evidence is used only for those.
    """
    records = sorted(open_records(history, today), key=lambda r: str(r.get("as_of")))
    changed = []
    claimed: set[tuple[str, str, str]] = set()
    for record in reversed(records):
        keys = _trade_keys(record)
        if keys and keys <= claimed:
            if record.get("approval_state") == OPEN_STATE:
                record["approval_state"] = SUPERSEDED
                changed.append(record)
            continue
        claimed |= keys
        since = str(record.get("as_of"))[:10]
        baseline = load_baseline(briefings_dir, record.get("source_briefing_id"))
        rows = [
            match_trade(t, since, transactions, baseline, today_observation, live_tails)
            for t in record.get("proposed_trades") or []
            if isinstance(t, dict)
        ]
        state = _record_state(rows)
        if state is None:
            continue
        previous = (record.get("approval_state"), record.get("execution", {}).get("trades"))
        record["approval_state"] = state
        record["execution"] = {"checked_on": today.isoformat(), "trades": rows}
        if previous != (state, rows):
            changed.append(record)
    return changed


def execution_detail(record: dict) -> str:
    """What was traded against a record, e.g. "SELL 10 of 10 AAA in …1111 on 2026-01-05"."""
    if record.get("approval_state") == SUPERSEDED:
        return "a newer proposal covers the same trades"
    parts = []
    for row in (record.get("execution") or {}).get("trades") or []:
        if row.get("status") in (None, "NONE", "UNCHECKED"):
            continue
        text = (
            f"{row['side']} {row['executed_quantity']:g} of {row['proposed_quantity']:g} "
            f"{row['ticker']} in {row['account']}"
        )
        if row.get("method") == "transactions":
            text += f" on {', '.join(row['dates'])}"
            if row.get("average_price") is not None:
                text += f" @ ${row['average_price']:,.2f}"
        else:
            text += " (position change only)"
        parts.append(text)
    return "; ".join(parts)


def describe(record: dict) -> str:
    return (
        f"{record.get('as_of')} {record.get('engine')}: "
        f"{record['approval_state']}: {execution_detail(record)}"
    )


def reconcile_context_file(
    path: Path,
    results: list[dict],
    as_of: datetime,
    briefings_dir: Path,
    accounts_api=None,
) -> tuple[list[str], list[str]]:
    """Reconcile portfolio_context.json in place.

    Returns (one line per changed record, transaction fetch problems). Without
    an accounts client, only position evidence is used.
    """
    if not path.exists():
        return [], []
    context = json.loads(path.read_text(encoding="utf-8"))
    history = context.get("decision_history") if isinstance(context, dict) else None
    if not isinstance(history, list):
        return [], []
    today = as_of.date()
    transactions, problems = (
        transactions_by_account(accounts_api, results, history, today)
        if accounts_api is not None
        else ({}, [])
    )
    live_tails = {
        tail4(account_tail(r.get("label") or "")) for r in results if not r.get("error")
    }
    changed = reconcile(
        history,
        observation_snapshot_dict(results, [], [], as_of),
        transactions,
        briefings_dir,
        live_tails,
        today,
    )
    if changed:
        atomic_write_text(path, json.dumps(context, indent=2, ensure_ascii=False) + "\n")
    return [describe(record) for record in changed], problems
