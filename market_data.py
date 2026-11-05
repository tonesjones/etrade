"""End-of-day prices from Tiingo and the technicals the briefing shows.

READ-ONLY market data. The key comes from TIINGO_API_KEY in .env and travels in
the Authorization header, never in the URL. Prices are cached per ticker for the
day under briefings/prices/ (gitignored), so same-day rebuilds make no requests.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from get_portfolio import atomic_write_text, is_cash_symbol

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
MARKET_SYMBOLS = ("SPY", "QQQ", "SMH")
HISTORY_DAYS = 400
TIMEOUT_SECONDS = 15
TRADING_DAYS_YEAR = 252
# Covers a weekend plus a market holiday; an older close is stale.
MAX_CLOSE_AGE_DAYS = 4


@dataclass(frozen=True)
class Technicals:
    symbol: str
    last_close: float
    last_date: str
    vs_sma50: float | None
    vs_sma200: float | None
    rsi14: float | None
    from_52w_high: float
    ret_1m: float | None
    ret_3m: float | None


def is_stale(t: Technicals, today: date) -> bool:
    """True when the last close is too old to trigger a review today."""
    try:
        closed = date.fromisoformat(t.last_date)
    except ValueError:
        return True
    return (today - closed).days > MAX_CLOSE_AGE_DAYS


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI over the whole series."""
    if len(closes) <= n:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain = sum(max(c, 0.0) for c in changes[:n]) / n
    loss = sum(max(-c, 0.0) for c in changes[:n]) / n
    for c in changes[n:]:
        gain = (gain * (n - 1) + max(c, 0.0)) / n
        loss = (loss * (n - 1) + max(-c, 0.0)) / n
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def technicals(symbol: str, series: list[tuple[str, float]]) -> Technicals | None:
    if not series:
        return None
    closes = [close for _date, close in series]
    last = closes[-1]
    sma50, sma200 = sma(closes, 50), sma(closes, 200)
    return Technicals(
        symbol=symbol,
        last_close=last,
        last_date=series[-1][0],
        vs_sma50=_pct(last, sma50) if sma50 else None,
        vs_sma200=_pct(last, sma200) if sma200 else None,
        rsi14=rsi(closes),
        from_52w_high=_pct(last, max(closes[-TRADING_DAYS_YEAR:])),
        ret_1m=_pct(last, closes[-22]) if len(closes) >= 22 else None,
        ret_3m=_pct(last, closes[-64]) if len(closes) >= 64 else None,
    )


def fetch_series(symbol: str, key: str, start: date) -> list[tuple[str, float]]:
    ticker = urllib.parse.quote(symbol.replace(".", "-").lower())
    url = TIINGO_URL.format(ticker=ticker) + "?" + urllib.parse.urlencode(
        {"startDate": start.isoformat()}
    )
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        rows = json.loads(response.read().decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("unexpected Tiingo response")
    return [
        (str(row["date"])[:10], float(row.get("adjClose") or row["close"]))
        for row in rows
        if isinstance(row, dict) and (row.get("adjClose") or row.get("close"))
    ]


def _cache_path(prices_dir: Path, symbol: str) -> Path:
    return prices_dir / f"{symbol.replace('.', '-').upper()}.json"


def _read_cache(path: Path) -> dict | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cached if isinstance(cached, dict) and cached.get("closes") else None


def load_technicals(
    symbols, today: date, prices_dir: Path, fetch: bool
) -> tuple[dict[str, Technicals], list[str]]:
    """Technicals per symbol, plus notes on what could not be loaded.

    With fetch=False (offline rebuilds, tests) only the cache is read, whatever its date.
    """
    key = (os.getenv("TIINGO_API_KEY") or "").strip()
    out: dict[str, Technicals] = {}
    problems: list[str] = []
    if fetch and not key:
        problems.append("TIINGO_API_KEY is not set in .env")
        fetch = False
    wanted = sorted({
        s for s in symbols if s and " " not in s and not is_cash_symbol(s)
    } | set(MARKET_SYMBOLS))
    for symbol in wanted:
        path = _cache_path(prices_dir, symbol)
        cached = _read_cache(path)
        fresh = cached is not None and cached.get("fetched") == today.isoformat()
        if fetch and not fresh:
            try:
                series = fetch_series(symbol, key, today - timedelta(days=HISTORY_DAYS))
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                problems.append(f"{symbol}: {getattr(exc, 'code', '') or exc}")
                series = None
            if series:
                cached = {"fetched": today.isoformat(), "closes": series}
                prices_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, json.dumps(cached))
        if cached is None:
            continue
        out[symbol] = technicals(symbol, [tuple(row) for row in cached["closes"]])
    if not out and not problems:
        problems.append("no cached prices (offline build)")
    return out, problems
