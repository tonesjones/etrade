"""Owner trim rules and reviews for the daily briefing.

Two hard limits are binding: the reply must COMPLY (sell at least the minimum)
or OVERRIDE with a dated reason. Concentration above the rebalance target and
stretched technicals are required reviews: the reply must TRIM or HOLD with a
dated reason, and the model sizes any trim. import_response.py checks the answers.
Portfolio history comes from the saved ``briefings/weights_YYYY-MM-DD.json`` snapshots.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from briefing_formatting import fmt_money, fmt_qty, fmt_weight
from get_portfolio import account_tail, fmt_signed_money, is_cash_symbol, term_from_lot
from market_data import MARKET_SYMBOLS, Technicals
from portfolio_policy import TrimRules, active_policy

_DATED_WEIGHTS = re.compile(r"^weights_(\d{4}-\d{2}-\d{2})\.json$")
_DATED_BRIEFING = re.compile(r"^briefing_(\d{4}-\d{2}-\d{2})\.json$")
# A one-step price move past 60% (or 1/0.6) with market value within 80% (or 1/0.8)
# is a split or reverse split, not a crash or a rally.
SPLIT_PRICE_RATIO = 0.6
SPLIT_VALUE_RATIO = 0.8
SLEEVE = "direct_ai_semi"
BINDING_KINDS = frozenset({"SLEEVE", "HARD_CAP"})


@dataclass(frozen=True)
class PriceSignal:
    symbol: str
    price: float
    high: float
    high_date: str
    from_high_pct: float
    change_5: float | None
    change_20: float | None


@dataclass(frozen=True)
class PortfolioSignal:
    total: float
    peak: float
    peak_date: str
    drawdown_pct: float
    change_5: float | None
    change_20: float | None
    snapshots: int
    first_date: str


@dataclass(frozen=True)
class Leg:
    account: str
    tax_bucket: str
    symbol: str
    lot_acquired: str | None
    quantity: float
    value: float
    gain: float | None
    term: str | None
    tax: float | None


@dataclass(frozen=True)
class RuleTrigger:
    rule_id: str
    kind: str
    scope: str
    symbols: tuple[str, ...]
    min_sell: float
    detail: str

    @property
    def binding(self) -> bool:
        return self.kind in BINDING_KINDS

    def to_meta(self) -> dict:
        row = asdict(self)
        row["symbols"] = list(self.symbols)
        row["binding"] = self.binding
        return row


def load_snapshot_history(briefings_dir: Path, today_snap: dict) -> list[dict]:
    """Dated weights snapshots before today's date, oldest first, then today's."""
    today = str(today_snap.get("date") or "")
    found = []
    for path in briefings_dir.glob("weights_20*.json"):
        match = _DATED_WEIGHTS.match(path.name)
        if not match or match.group(1) >= today:
            continue
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(snap, dict):
            found.append((match.group(1), snap))
    return [snap for _date, snap in sorted(found, key=lambda item: item[0])] + [today_snap]


def _change(series: list[float], steps: int) -> float | None:
    if len(series) <= steps or not series[-1 - steps]:
        return None
    return (series[-1] / series[-1 - steps] - 1) * 100


def _looks_like_split(before: dict, after: dict) -> bool:
    """Price jumped or fell by the split ratio in one step while market value held."""
    price = after["price"] / before["price"]
    old_value = before.get("market_value") or 0
    value = (after.get("market_value") or 0) / old_value if old_value else 0
    moved = price < SPLIT_PRICE_RATIO or price > 1 / SPLIT_PRICE_RATIO
    held = SPLIT_VALUE_RATIO < value < 1 / SPLIT_VALUE_RATIO
    return moved and held


def price_series(history: list[dict], symbol: str) -> list[tuple[str, float]]:
    """(date, price) for a symbol, restarted after an apparent stock split."""
    series: list[tuple[str, float]] = []
    previous = None
    for snap in history:
        row = next((h for h in snap.get("holdings") or [] if h.get("symbol") == symbol), None)
        if not row or not row.get("price"):
            continue
        if previous and _looks_like_split(previous, row):
            series = []
        series.append((str(snap.get("date") or ""), float(row["price"])))
        previous = row
    return series


def position_signals(history: list[dict]) -> dict[str, PriceSignal]:
    today = history[-1]
    out = {}
    for row in today.get("holdings") or []:
        symbol = row.get("symbol") or ""
        if not symbol or " " in symbol or is_cash_symbol(symbol):
            continue
        series = price_series(history, symbol)
        if not series:
            continue
        prices = [price for _date, price in series]
        high_date, high = max(series, key=lambda item: item[1])
        out[symbol] = PriceSignal(
            symbol=symbol,
            price=prices[-1],
            high=high,
            high_date=high_date,
            from_high_pct=(prices[-1] / high - 1) * 100,
            change_5=_change(prices, 5),
            change_20=_change(prices, 20),
        )
    return out


def portfolio_signal(history: list[dict]) -> PortfolioSignal | None:
    series = [
        (str(snap.get("date") or ""), float(snap["grand_total"]))
        for snap in history
        if snap.get("grand_total")
    ]
    if not series:
        return None
    totals = [total for _date, total in series]
    peak_date, peak = max(series, key=lambda item: item[1])
    return PortfolioSignal(
        total=totals[-1],
        peak=peak,
        peak_date=peak_date,
        drawdown_pct=(totals[-1] / peak - 1) * 100,
        change_5=_change(totals, 5),
        change_20=_change(totals, 20),
        snapshots=len(series),
        first_date=series[0][0],
    )


def sleeve_breach_streak(history: list[dict], cap_pct: float) -> int:
    """Consecutive snapshots, ending today, with the direct sleeve above cap_pct."""
    streak = 0
    for snap in reversed(history):
        weight = snap.get("cluster_w")
        if weight is None or weight <= cap_pct:
            break
        streak += 1
    return streak


def _holding_lots(holding: dict) -> list[dict]:
    if holding.get("lots"):
        return holding["lots"]
    if holding.get("date_acquired"):
        return [{
            "qty": holding.get("quantity") or 0.0,
            "total_cost": holding.get("total_cost") or 0.0,
            "acquired": holding["date_acquired"],
        }]
    return []


def candidate_legs(results, symbols, as_of: datetime, rules: TrimRules) -> list[Leg]:
    """Every sellable piece of the symbols, cheapest tax per dollar first.

    IRA shares come first, then Roth (both untaxed; Roth space is kept for growth),
    then taxable lots ordered by estimated tax per dollar sold, so losses lead.
    Lots without a cost basis go last because their tax is unknown.
    """
    wanted = set(symbols)
    sheltered: list[tuple[int, Leg]] = []
    taxable: list[tuple[float, Leg]] = []
    unknown: list[Leg] = []
    for result in results or []:
        if result.get("error"):
            continue
        bucket = result.get("tax_bucket") or "taxable"
        account = account_tail(result.get("label") or "") or "account"
        for holding in result.get("holdings") or []:
            symbol = holding.get("symbol") or ""
            qty = holding.get("quantity") or 0.0
            value = holding.get("market_value") or 0.0
            if symbol not in wanted or qty <= 0 or value <= 0:
                continue
            unit = value / qty
            if bucket != "taxable":
                sheltered.append((0 if bucket == "traditional" else 1, Leg(
                    account, bucket, symbol, None, qty, value, None, None, 0.0
                )))
                continue
            for lot in _holding_lots(holding):
                lot_qty = lot.get("qty") or 0.0
                if lot_qty <= 0:
                    continue
                acquired = lot.get("acquired")
                acquired_text = acquired.date().isoformat() if acquired else None
                lot_value = lot_qty * unit
                cost = lot.get("total_cost") or 0.0
                if cost <= 0:
                    unknown.append(Leg(
                        account, bucket, symbol, acquired_text, lot_qty, lot_value, None, None, None
                    ))
                    continue
                term = term_from_lot(lot, as_of)
                gain = lot_value - cost
                rate = rules.long_term_tax_rate if term == "LT" else rules.short_term_tax_rate
                leg = Leg(
                    account, bucket, symbol, acquired_text, lot_qty, lot_value,
                    gain, term, gain * rate,
                )
                taxable.append((leg.tax / lot_value, leg))
    sheltered.sort(key=lambda item: (item[0], -item[1].value, item[1].symbol))
    taxable.sort(key=lambda item: (item[0], item[1].symbol, item[1].lot_acquired or ""))
    return [leg for _key, leg in sheltered] + [leg for _key, leg in taxable] + unknown


def _partial(leg: Leg, dollars: float) -> Leg:
    unit = leg.value / leg.quantity
    qty = min(leg.quantity, math.ceil(dollars / unit - 1e-9))
    share = qty / leg.quantity
    return Leg(
        leg.account, leg.tax_bucket, leg.symbol, leg.lot_acquired, qty, qty * unit,
        None if leg.gain is None else leg.gain * share, leg.term,
        None if leg.tax is None else leg.tax * share,
    )


def sell_plan(legs: list[Leg], min_sell: float) -> list[Leg]:
    """The first legs that cover min_sell, the last one cut to whole shares."""
    plan, covered = [], 0.0
    for leg in legs:
        if covered >= min_sell:
            break
        need = min_sell - covered
        piece = leg if leg.value <= need else _partial(leg, need)
        plan.append(piece)
        covered += piece.value
    return plan


def _prior_firings(briefings_dir: Path, today: str) -> dict[tuple[str, str], str]:
    """Latest earlier date each (kind, scope) fired, from saved briefing sidecars."""
    found: dict[tuple[str, str], str] = {}
    for path in briefings_dir.glob("briefing_20*.json"):
        match = _DATED_BRIEFING.match(path.name)
        if not match or match.group(1) >= today:
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in (meta.get("rule_triggers") or []) if isinstance(meta, dict) else []:
            if not isinstance(row, dict):
                continue
            key = (str(row.get("kind")), str(row.get("scope")))
            found[key] = max(found.get(key, ""), match.group(1))
    return found


def _recent(fired_on: str | None, today: str, repeat_days: int) -> bool:
    if not fired_on:
        return False
    return (date.fromisoformat(today) - date.fromisoformat(fired_on)).days < repeat_days


def stretch_hits(tech: Technicals, rules: TrimRules) -> list[str]:
    hits = []
    if tech.rsi14 is not None and tech.rsi14 >= rules.stretch_rsi:
        hits.append(f"RSI {tech.rsi14:.0f} (≥ {rules.stretch_rsi:g})")
    if tech.vs_sma50 is not None and tech.vs_sma50 >= rules.stretch_above_sma50_pct:
        hits.append(f"{tech.vs_sma50:+.0f}% vs 50-day (≥ {rules.stretch_above_sma50_pct:g}%)")
    if tech.vs_sma200 is not None and tech.vs_sma200 >= rules.stretch_above_sma200_pct:
        hits.append(f"{tech.vs_sma200:+.0f}% vs 200-day (≥ {rules.stretch_above_sma200_pct:g}%)")
    return hits


@dataclass(frozen=True)
class TrimReview:
    signals: dict[str, PriceSignal]
    portfolio: PortfolioSignal | None
    sleeve_streak: int
    technicals: dict[str, Technicals]
    technicals_problems: tuple[str, ...]
    triggers: tuple[RuleTrigger, ...]
    plans: dict[str, tuple[Leg, ...]]
    tax_free: dict[str, tuple[Leg, ...]]


def review(
    holdings: list[dict],
    results,
    grand_total: float,
    cluster_mv: float,
    history: list[dict],
    as_of: datetime,
    briefings_dir: Path,
    technicals: dict[str, Technicals] | None = None,
    technicals_problems=(),
) -> TrimReview:
    policy = active_policy()
    rules = policy.trim_rules
    technicals = technicals or {}
    base = dict(
        signals=position_signals(history),
        portfolio=portfolio_signal(history),
        sleeve_streak=sleeve_breach_streak(history, policy.cluster_soft_cap_pct),
        technicals=technicals,
        technicals_problems=tuple(technicals_problems),
    )
    if rules is None or grand_total <= 0:
        return TrimReview(**base, triggers=(), plans={}, tax_free={})

    today = as_of.strftime("%Y-%m-%d")
    prior = _prior_firings(briefings_dir, today)
    found: list[tuple[str, str, tuple[str, ...], float, float, str]] = []

    streak = base["sleeve_streak"]
    cut = max(0.0, cluster_mv - grand_total * policy.cluster_soft_cap_pct / 100)
    if streak >= rules.sleeve_breach_enforce_days and cut > 0:
        members = tuple(policy.sleeves[SLEEVE])
        found.append((
            "SLEEVE", SLEEVE, members, cut, cut,
            f"direct AI/semi {fmt_weight(cluster_mv / grand_total * 100)} has been above the "
            f"{policy.cluster_soft_cap_pct:g}% ceiling for {streak} snapshots in a row "
            f"(rule: {rules.sleeve_breach_enforce_days}). Sell at least **{fmt_money(cut)}** "
            f"across {'+'.join(members)} to reach {policy.cluster_soft_cap_pct:g}%.",
        ))

    index_funds = set(policy.sleeves["broad_index"])
    target = rules.rebalance_target_pct
    for h in holdings:
        symbol = h["symbol"]
        if is_cash_symbol(symbol) or " " in symbol or symbol in index_funds:
            continue
        excess = h["market_value"] - grand_total * target / 100
        if h["weight"] > rules.single_name_hard_cap_pct:
            found.append((
                "HARD_CAP", symbol, (symbol,), excess, excess,
                f"{symbol} is {fmt_weight(h['weight'])}, above the "
                f"{rules.single_name_hard_cap_pct:g}% hard cap. Sell at least "
                f"**{fmt_money(excess)}** to return to {target:g}%.",
            ))
        elif excess > 0 and not _recent(prior.get(("REBALANCE", symbol)), today,
                                        rules.repeat_days):
            found.append((
                "REBALANCE", symbol, (symbol,), 0.0, excess,
                f"{symbol} is {fmt_weight(h['weight'])}, above the {target:g}% target. "
                f"Selling {fmt_money(excess)} would bring it back to {target:g}%. "
                "TRIM (you size it) or HOLD.",
            ))
        tech = technicals.get(symbol)
        hits = stretch_hits(tech, rules) if tech else []
        if (
            hits
            and (h.get("total_gain") or 0) > 0
            and h["market_value"] >= policy.analyze_market_value_floor
            and not _recent(prior.get(("STRETCHED", symbol)), today, rules.repeat_days)
        ):
            found.append((
                "STRETCHED", symbol, (symbol,), 0.0, 0.0,
                f"{symbol} looks stretched: {'; '.join(hits)}, with an unrealized gain of "
                f"{fmt_signed_money(h['total_gain'])}. TRIM to take some profit, or HOLD.",
            ))

    triggers, plans, tax_free = [], {}, {}
    for index, (kind, scope, symbols, minimum, amount, detail) in enumerate(found, start=1):
        trigger = RuleTrigger(f"R{index}", kind, scope, symbols, round(minimum, 2), detail)
        triggers.append(trigger)
        legs = candidate_legs(results, symbols, as_of, rules)
        plans[trigger.rule_id] = tuple(sell_plan(legs, amount) if amount > 0 else legs)
        tax_free[trigger.rule_id] = tuple(leg for leg in legs if leg.tax_bucket != "taxable")
    return TrimReview(**base, triggers=tuple(triggers), plans=plans, tax_free=tax_free)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f}%"


def _tech_text(t: Technicals) -> str:
    rsi_text = "—" if t.rsi14 is None else f"{t.rsi14:.0f}"
    return (
        f"{_pct(t.vs_sma50)} vs 50-day, {_pct(t.vs_sma200)} vs 200-day, RSI {rsi_text}, "
        f"{_pct(t.from_52w_high)} from 52-week high, 1-month {_pct(t.ret_1m)}, "
        f"3-month {_pct(t.ret_3m)} (close {t.last_date})"
    )


def format_risk_signals(trim: TrimReview, holdings: list[dict]) -> str:
    policy = active_policy()
    lines = []
    p = trim.portfolio
    if p is not None:
        lines.append(
            f"- Portfolio {fmt_money(p.total)}: {_pct(p.drawdown_pct)} "
            f"({fmt_signed_money(p.total - p.peak)}) from the tracked peak {fmt_money(p.peak)} "
            f"on {p.peak_date}. 5-snapshot {_pct(p.change_5)}, "
            f"20-snapshot {_pct(p.change_20)}. From {p.snapshots} snapshots since "
            f"{p.first_date}; totals include deposits and withdrawals."
        )
    if trim.sleeve_streak:
        lines.append(
            f"- Direct AI/semi has been above {policy.cluster_soft_cap_pct:g}% for "
            f"{trim.sleeve_streak} snapshots in a row."
        )
    for symbol in MARKET_SYMBOLS:
        tech = trim.technicals.get(symbol)
        if tech:
            lines.append(f"- **{symbol}:** {_tech_text(tech)}.")
    if trim.technicals_problems:
        lines.append("- Technicals missing: " + "; ".join(trim.technicals_problems) + ".")

    sized = [
        h["symbol"] for h in holdings
        if h["market_value"] >= policy.analyze_market_value_floor
        and not is_cash_symbol(h["symbol"])
    ]
    techs = [trim.technicals[s] for s in sized if s in trim.technicals]
    if techs:
        techs.sort(key=lambda t: (-(t.vs_sma50 or -999), t.symbol))
        lines += [
            "",
            "| Ticker | vs 50-day | vs 200-day | RSI(14) | From 52w high | 1-month | 3-month |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        lines.extend(
            f"| {t.symbol} | {_pct(t.vs_sma50)} | {_pct(t.vs_sma200)} | "
            f"{'—' if t.rsi14 is None else f'{t.rsi14:.0f}'} | {_pct(t.from_52w_high)} | "
            f"{_pct(t.ret_1m)} | {_pct(t.ret_3m)} |"
            for t in techs
        )
        return "\n".join(lines)
    rows = sorted(
        (s for s in trim.signals.values() if s.symbol in sized),
        key=lambda s: (s.from_high_pct, s.symbol),
    )
    if rows:
        lines += [
            "",
            "No end-of-day technicals; distance from the high tracked in our snapshots instead:",
            "",
            "| Ticker | Price | Tracked high | From high | 5-snap | 20-snap |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        lines.extend(
            f"| {s.symbol} | ${s.price:,.2f} | ${s.high:,.2f} ({s.high_date}) | "
            f"{_pct(s.from_high_pct)} | {_pct(s.change_5)} | {_pct(s.change_20)} |"
            for s in rows
        )
    return "\n".join(lines) or "- No snapshot history yet."


def _leg_text(leg: Leg) -> str:
    where = {"traditional": "IRA", "roth": "Roth"}.get(leg.tax_bucket, "taxable")
    lot = f" lot {leg.lot_acquired}" if leg.lot_acquired else ""
    if leg.tax_bucket != "taxable":
        tax = "no tax"
    elif leg.tax is None:
        tax = "basis unknown"
    else:
        tax = f"{leg.term} {fmt_signed_money(leg.gain)}, est. tax {fmt_signed_money(leg.tax)}"
    return (
        f"{where} {leg.account} {leg.symbol}{lot}: {fmt_qty(leg.quantity)} sh "
        f"{fmt_money(leg.value)} ({tax})"
    )


def _tax_text(legs: tuple[Leg, ...]) -> str:
    sold = sum(leg.value for leg in legs)
    tax = sum(leg.tax or 0.0 for leg in legs)
    breakeven = f" (break-even drop {tax / sold * 100:.1f}%)" if sold and tax > 0 else ""
    return f"est. tax {fmt_signed_money(tax)}{breakeven}"


def format_rule_triggers(trim: TrimReview) -> str:
    rules = active_policy().trim_rules
    if rules is None:
        return "- Trim rules are off (no `trim_rules` in portfolio_policy.json)."
    if not trim.triggers:
        return "- No rule or review fired today."
    lines = []
    for trigger in trim.triggers:
        answer = "binding: COMPLY or OVERRIDE" if trigger.binding else "review: TRIM or HOLD"
        lines.append(f"- **{trigger.rule_id} {trigger.kind}** ({answer}): {trigger.detail}")
        plan = trim.plans.get(trigger.rule_id) or ()
        if not plan:
            continue
        if trigger.kind == "STRETCHED":
            free = sum(leg.value for leg in trim.tax_free.get(trigger.rule_id) or ())
            lines.append(
                f"  Untaxed IRA/Roth shares: {fmt_money(free)}. "
                f"Selling the whole position: {_tax_text(plan)}."
            )
            continue
        lines.append("  Lowest-tax order: " + "; ".join(_leg_text(leg) for leg in plan) + ".")
        sold = sum(leg.value for leg in plan)
        lines.append(f"  This order sells {fmt_money(sold)}, {_tax_text(plan)}.")
    lines.append(
        "Break-even drop: the fall in the sold shares that would cost as much as the "
        "estimated tax. Loss lots show a negative tax (a saving, subject to wash-sale rules)."
    )
    return "\n".join(lines)
