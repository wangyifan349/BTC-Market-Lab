#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Market Lab v2.2 — PyQt6 + pyqtgraph + NumPy
=================================================================
v2.2 fixes:
  * [Critical] QThread worker objects were not strongly referenced by Python and
    got garbage-collected, so thread.started → worker.run never fired: the UI
    stayed on "Network: Not connected" and Binance data looked like it never
    loaded. Now _run_worker keeps strong references until the thread finishes.
  * [Critical] The Windows console defaults to GBK; printing "R²"/"σ" raised a
    UnicodeEncodeError and aborted the fit callback halfway. On startup stdout /
    stderr are reconfigured with errors="replace" so unsupported glyphs become
    "?" instead of crashing.
  * Slimmed dependency imports: PyQt6 / pyqtgraph / NumPy / PySocks are all
    installed and imported directly — no more try/except swallowing import errors.
  * Removed the version-compat wrappers (_safe_downsampling / _safe_clip /
    _make_fill_between ...) and call the stable pyqtgraph API directly.
  * More accurate network status label: distinguishes "Connecting / Connected /
    All strategies failed".

Dependencies:
    pip install PyQt6 pyqtgraph numpy pysocks

Run from the command line (not IDLE):
    python BTC_Market_Lab.py
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
import socks
import sockshandler

from PyQt6.QtCore import (
    QObject, QPointF, QRectF, QThread, Qt, QTimer, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPicture
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSpinBox, QStatusBar, QTabWidget,
    QVBoxLayout, QWidget,
)


# ============================================================================
# 0. Console logging
# ============================================================================
def log(*args) -> None:
    print(f"[{datetime.now():%H:%M:%S}]", *args, flush=True)


def log_rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * 4} {title} {'─' * max(4, 58 - len(title))}", flush=True)
    else:
        print("─" * 70, flush=True)


# ============================================================================
# 1. Theme colors (green up / red down) & plot config
# ============================================================================
C_BG      = "#0b0e13"       # window / chart background
C_PANEL   = "#141a24"       # main panel background
C_PANEL_2 = "#111722"       # side sub-panel background
C_INPUT   = "#0f151e"       # input / card background
C_BORDER  = "#232e3e"       # border color
C_TEXT    = "#e8eef6"       # primary text
C_MUTED   = "#93a1b1"       # secondary text
C_ACCENT  = "#f7931a"      # bitcoin orange
C_UP      = "#16c784"      # up — green
C_DOWN    = "#ea3943"      # down — red
C_FIT     = "#4fc3f7"      # fitted curve
C_PRED    = "#ffb347"      # forecast curve
C_BAND    = (255, 179, 71, 30)  # forecast confidence band (RGBA)

pg.setConfigOptions(antialias=True, background=C_BG, foreground=C_MUTED)  # global antialiasing + theme

FONT_STACK = ["Inter", "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC"]  # UI font stack


# ============================================================================
# 2. Network layer: proxy 127.0.0.1:10808 → auto fallback to direct
# ============================================================================
PROXY_HOST = "127.0.0.1"   # local proxy address (v2rayN / clash etc.)
PROXY_PORT = 10808         # local proxy port (HTTP/SOCKS mixed mode works too)

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
}


class Http:
    """
    Tries several network strategies in order and degrades automatically:
        HTTP proxy 10808 → HTTP proxy 10809 → SOCKS5 proxy 10808 → direct (no proxy)
    Once a strategy succeeds, later requests reuse it.
    """

    STRATEGIES = (
        # (display name, key, proxy target, timeout seconds)
        (f"HTTP proxy {PROXY_HOST}:{PROXY_PORT}", "http8", f"http://{PROXY_HOST}:{PROXY_PORT}", 6.0),
        (f"HTTP proxy {PROXY_HOST}:10809", "http9", f"http://{PROXY_HOST}:10809", 6.0),
        (f"SOCKS5 proxy {PROXY_HOST}:{PROXY_PORT}", "socks8", f"socks5h://{PROXY_HOST}:{PROXY_PORT}", 6.0),
        ("Direct (no proxy)", "direct", "", 20.0),
    )

    def __init__(self):
        self._openers: Dict[str, urllib.request.OpenerDirector] = {}  # key -> built opener cache
        self._preferred: str | None = None                            # key of the currently preferred strategy
        self.last_error: str = ""                                     # summary of the last all-failed attempt
        self._lock = threading.Lock()                                 # keeps cache / preference thread-safe

    @staticmethod
    def _make_opener(key: str, target: str):
        if key == "direct":
            # explicitly clear the proxy so this really is a "direct" connection
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if key.startswith("http"):
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": target, "https": target}))
        # SOCKS5 — PySocks is installed, build the handler directly
        handler = sockshandler.SocksiPyHandler(
            socks.SOCKS5, PROXY_HOST, PROXY_PORT, rdns=True)
        return urllib.request.build_opener(handler)

    def _opener(self, key: str, target: str):
        with self._lock:
            if key not in self._openers:
                self._openers[key] = self._make_opener(key, target)   # build lazily on first use
            return self._openers[key]

    def get_json(self, url: str, timeout: float | None = None):
        order: List[str] = []
        if self._preferred:
            order.append(self._preferred)                             # keep the previously successful strategy first
        order += [s[1] for s in self.STRATEGIES if s[1] != self._preferred]  # the rest fall back in fixed order

        errors: List[str] = []
        for key in order:
            name, _, target, default_to = next(
                (s for s in self.STRATEGIES if s[1] == key), (key, key, "", 15.0))
            try:
                opener = self._opener(key, target)                    # try the current strategy
                req = urllib.request.Request(url, headers=UA_HEADERS)
                with opener.open(req, timeout=timeout or default_to) as resp:
                    raw = resp.read().decode("utf-8", "replace")      # tolerant decode, replace bad bytes
                data = json.loads(raw)                                # parse JSON
                if self._preferred != key:
                    with self._lock:
                        self._preferred = key                         # remember the working strategy
                    log(f"Network strategy → {name} (remembered, will be preferred)")
                self.last_error = ""
                return data
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name} ✗ {exc}")                      # record the failure reason
                log(f"Network strategy {name} failed: {exc}")         # continue to the next strategy
        self.last_error = " | ".join(errors)
        raise RuntimeError("All network strategies failed | " + self.last_error)

    def preferred_name(self) -> str:
        for s in self.STRATEGIES:
            if s[1] == self._preferred:
                return s[0]                                           # display name for the status bar
        return ""

    def reset(self) -> None:
        with self._lock:
            self._preferred = None                                    # forget the remembered strategy, re-probe
        self.last_error = ""
        log("Network strategies reset")


HTTP = Http()


# ============================================================================
# 3. Data model & timeframes
# ============================================================================
@dataclass(frozen=True)
class Candle:
    """One candle (K-line); field order matches the arrays returned by the exchanges."""
    ts: float            # millisecond timestamp (UTC)
    open: float          # open
    high: float          # high
    low: float           # low
    close: float         # close
    volume: float = 0.0  # volume (quote-currency amount on Binance)


@dataclass(frozen=True)
class Timeframe:
    """Timeframe config: per-exchange interval params + local conversion params."""
    key: str                # unique internal id, e.g. "1d"
    label: str              # UI display name, e.g. "Daily"
    binance: str            # Binance interval, e.g. "1d"
    okx: str                # OKX interval, e.g. "1D"
    cg_days: str            # CoinGecko market_chart days parameter
    target: int             # target number of candles to load (paged until full)
    periods_per_year: float # annualization factor: how many bars per year
    resample: str = ""      # if set, original candles are resampled to this period (e.g. annual "Y")


TIMEFRAMES: List[Timeframe] = [
    Timeframe("1h", "1 Hour",   "1h", "1H", "90", 1000, 24 * 365),
    Timeframe("4h", "4 Hours",  "4h", "4H", "90", 1000, 6 * 365),
    Timeframe("1d", "Daily",    "1d", "1D", "max", 2000, 365),
    Timeframe("1w", "Weekly",   "1w", "1W", "max", 1000, 52),
    Timeframe("1M", "Monthly",  "1M", "1M", "max", 500, 12),
    Timeframe("1Y", "Yearly",   "1M", "1M", "max", 500, 1, resample="Y"),  # monthly data resampled to yearly
]
TF_BY_KEY = {tf.key: tf for tf in TIMEFRAMES}  # quick lookup of a timeframe by key


# ============================================================================
# 4. Market data fetching
# ============================================================================
BINANCE_HOSTS = (   # Binance official endpoints, tried in turn
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api1.binance.com",
)


def _dedup(candles: Sequence[Candle]) -> List[Candle]:
    bucket = {int(c.ts): c for c in candles}   # dedupe by millisecond timestamp (later wins)
    return [bucket[k] for k in sorted(bucket)] # then return in ascending time order


def _binance_paged(host: str, symbol: str, interval: str, target: int) -> List[Candle]:
    """Page backwards through Binance klines (via endTime) until we have `target` bars."""
    out: List[Candle] = []
    end_ms = None
    while len(out) < target:
        limit = int(min(1000, max(50, target - len(out))))  # max 1000 per page
        query = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_ms is not None:
            query["endTime"] = int(end_ms)                  # limit to before the earliest bar of the previous page
        rows = HTTP.get_json(f"{host}/api/v3/klines?" + urllib.parse.urlencode(query))
        if not isinstance(rows, list) or not rows:
            break                                            # no more data
        out = [Candle(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5])) for r in rows] + out      # prepend the new page
        end_ms = int(rows[0][0]) - 1                         # continue before the earliest bar
        if len(rows) < limit:
            break                                            # short page means we reached the beginning
        time.sleep(0.1)                                      # light rate limiting to avoid bans
    return _dedup(out)


def fetch_binance(symbol: str, tf: Timeframe) -> List[Candle]:
    symbol = symbol.upper().replace("-", "").replace("/", "")  # normalize to BTCUSDT form
    err = None
    for host in BINANCE_HOSTS:                                  # retry across endpoints
        try:
            data = _binance_paged(host, symbol, tf.binance, tf.target)
            if data:
                log(f"Binance {host} returned {len(data)} candles")
                return data
        except Exception as exc:  # noqa: BLE001
            err = exc
            log(f"Binance {host} failed: {exc}")
    raise RuntimeError(f"Binance fetch failed: {err}")


def _to_okx_inst(symbol: str) -> str:
    """Normalize BTCUSDT / BTC-USDT into the BTC-USDT form that OKX expects."""
    s = symbol.upper().replace("/", "-")
    if "-" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):   # find the quote currency and insert the dash
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}-{quote}"
    return s


def fetch_okx(symbol: str, tf: Timeframe) -> List[Candle]:
    inst = _to_okx_inst(symbol)
    out: List[Candle] = []
    after = None
    while len(out) < tf.target:
        query = {"instId": inst, "bar": tf.okx, "limit": 100}
        if after:
            query["after"] = after                            # cursor: page towards history
        js = HTTP.get_json("https://www.okx.com/api/v5/market/history-candles?"
                           + urllib.parse.urlencode(query))
        rows = js.get("data") or []
        if not rows:
            break
        out = [Candle(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5])) for r in rows] + out
        after = rows[-1][0]                                   # continue from the earliest bar
        if len(rows) < 100:
            break
        time.sleep(0.12)
    data = _dedup(out)
    if not data:
        raise RuntimeError("OKX returned empty data")
    log(f"OKX {inst} returned {len(data)} candles")
    return data


def fetch_coingecko(tf: Timeframe) -> List[Candle]:
    js = HTTP.get_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                       f"?vs_currency=usd&days={tf.cg_days}", timeout=25.0)
    prices = js.get("prices") or []   # [[millisecond timestamp, price], ...]
    vols = {int(t): float(v) for t, v in (js.get("total_volumes") or [])}  # timestamp -> volume
    if not prices:
        raise RuntimeError("CoinGecko returned empty data")
    candles = [Candle(float(t), float(p), float(p), float(p), float(p), vols.get(int(t), 0.0))
               for t, p in prices]   # CoinGecko has price points only; OHLC use the same price
    log(f"CoinGecko returned {len(candles)} price points")
    return candles[-tf.target:] if len(candles) > tf.target else candles  # keep only the trailing slice


def resample(candles: Sequence[Candle], rule: str) -> List[Candle]:
    """Aggregate finer candles into a coarser period (year / month / week / day)."""
    if not candles:
        return []
    groups: Dict[tuple, List[Tuple[int, Candle]]] = {}
    order: List[tuple] = []
    for c in candles:
        dt = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        if rule == "Y":
            key = (dt.year,)                                    # bucket key: year
            start = datetime(dt.year, 1, 1, tzinfo=timezone.utc)
        elif rule == "M":
            key = (dt.year, dt.month)                           # month
            start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
        elif rule == "W":
            monday = dt - timedelta(days=dt.weekday())          # Monday as the week start
            key = (monday.year, monday.month, monday.day)
            start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        else:
            key = (dt.year, dt.month, dt.day)                   # natural day
            start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        if key not in groups:
            groups[key] = []
            order.append(key)                                   # remember first-seen order
        groups[key].append((int(start.timestamp() * 1000), c))

    result: List[Candle] = []
    for key in order:
        items = groups[key]
        cs = [c for _, c in items]
        result.append(Candle(
            ts=float(items[0][0]),
            open=cs[0].open,             # period open = first bar's open
            high=max(c.high for c in cs),   # period high = max of the window
            low=min(c.low for c in cs),     # period low = min of the window
            close=cs[-1].close,          # period close = last bar's close
            volume=float(sum(c.volume for c in cs)),  # summed volume
        ))
    result.sort(key=lambda c: c.ts)
    return result


def load_market(source: str, symbol: str, tf: Timeframe) -> List[Candle]:
    """Unified entry: fetch by source → (optionally) resample → dedupe, return clean candles."""
    if source == "Binance":
        candles = fetch_binance(symbol, tf)
    elif source == "OKX":
        candles = fetch_okx(symbol, tf)
    else:
        candles = fetch_coingecko(tf)

    if tf.resample:                      # resample raw data when a coarser period (e.g. yearly) is requested
        before = len(candles)
        candles = resample(candles, tf.resample)
        log(f"Resampled {before} → {len(candles)} bars ({tf.label})")

    candles = _dedup(candles)
    if len(candles) < 10:                # too few bars make stats / fitting meaningless
        raise RuntimeError(f"Too few valid bars ({len(candles)}). Switch data source or timeframe.")
    return candles


# ============================================================================
# 5. NumPy statistics report
# ============================================================================
def _skew(a: np.ndarray) -> float:
    if a.size < 2:
        return 0.0
    m, s = float(a.mean()), float(a.std(ddof=0))
    return 0.0 if s == 0 else float(np.mean(((a - m) / s) ** 3))


def _kurt(a: np.ndarray) -> float:
    """Excess kurtosis (normal distribution = 0)."""
    if a.size < 2:
        return 0.0
    m, s = float(a.mean()), float(a.std(ddof=0))
    return 0.0 if s == 0 else float(np.mean(((a - m) / s) ** 4) - 3.0)  # E[z^4]-3


def _max_streak(flags: np.ndarray) -> int:
    """Longest consecutive run of True (up / down streak length)."""
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0     # +1 while consecutive, reset on break
        if cur > best:
            best = cur
    return int(best)


def _ema_series(a: np.ndarray, span: int) -> np.ndarray:
    """Recursive EMA series: alpha=2/(span+1)."""
    if a.size == 0:
        return np.asarray([])
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(a, dtype=float)
    out[0] = a[0]                     # seed with the first value
    for i in range(1, a.size):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(a: np.ndarray, period: int = 14) -> float:
    """Wilder RSI, computed from smoothed average gains / losses."""
    if a.size < period + 1:
        return float("nan")
    d = np.diff(a)
    gain = np.where(d > 0, d, 0.0)    # upward moves
    loss = np.where(d < 0, -d, 0.0)   # downward moves
    ag = float(gain[:period].mean())  # initial average gain
    al = float(loss[:period].mean())  # initial average loss
    for i in range(period, d.size):   # Wilder smoothing
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
    if al == 0:
        return 100.0                  # no losses => RSI maxed out
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _pct(v: float) -> str:
    return f"{v * 100:+,.2f}%"   # fraction to signed, 2-decimal percent string


def _money(v: float) -> str:
    return f"${v:,.2f}"          # thousands-separated currency format


@dataclass
class Report:
    text: str = ""
    cards: Dict[str, Tuple[str, str]] = field(default_factory=dict)


def build_report(candles: Sequence[Candle], tf: Timeframe,
                 source: str, symbol: str) -> Report:
    """Compute as much as NumPy allows: printed to the console and parsed into UI cards."""
    close = np.asarray([c.close for c in candles], dtype=float)   # close price series
    hi = np.asarray([c.high for c in candles], dtype=float)       # high price series
    lo = np.asarray([c.low for c in candles], dtype=float)        # low price series
    vol = np.asarray([c.volume for c in candles], dtype=float)    # volume series
    ts = np.asarray([c.ts for c in candles], dtype=float) / 1000.0  # timestamps in seconds
    n = close.size                                                 # sample count

    sr = np.diff(close) / np.maximum(close[:-1], 1e-9)          # simple returns
    lr = np.diff(np.log(np.maximum(close, 1e-9)))               # log returns
    ann = float(tf.periods_per_year)                              # periods per year for annualization

    lines: List[str] = []
    cards: Dict[str, Tuple[str, str]] = {}

    def add(text: str) -> None:
        lines.append(text)

    start = datetime.fromtimestamp(ts[0], tz=timezone.utc)
    end = datetime.fromtimestamp(ts[-1], tz=timezone.utc)

    # ---------------- header ----------------
    add("=" * 74)
    add(f"  BTC Statistics Report   {source} · {symbol} · {tf.label}")
    add(f"  Sample {n} bars   Period {start:%Y-%m-%d} → {end:%Y-%m-%d}   "
        f"({(end - start).days} days)")
    add("=" * 74)

    # ---------------- price ----------------
    p_mean, p_med = float(np.mean(close)), float(np.median(close))
    p_std = float(np.std(close, ddof=1)) if n > 1 else 0.0
    p_var = float(np.var(close, ddof=1)) if n > 1 else 0.0
    p_min, p_max = float(np.min(close)), float(np.max(close))
    p_ptp = float(np.ptp(close))
    q = np.percentile(close, [5, 25, 50, 75, 95])
    iqr = float(np.percentile(close, 75) - np.percentile(close, 25))

    log_rule("Price stats via NumPy")
    add("【Price · NumPy】close = np.array([...])")
    add(f"  np.mean        mean price      {_money(p_mean)}")
    add(f"  np.median      median price    {_money(p_med)}")
    add(f"  np.std(ddof=1) std dev         ${p_std:,.2f}   (CV {p_std / p_mean * 100:.2f}%)")
    add(f"  np.var(ddof=1) variance        {p_var:,.2f}")
    add(f"  np.min/np.max  low / high      {_money(p_min)} / {_money(p_max)}")
    add(f"  np.ptp         range           {_money(p_ptp)}   (amplitude {p_ptp / p_min * 100:.2f}%)")
    add(f"  np.percentile 5/25/50/75/95    "
        + " / ".join(f"{x:,.0f}" for x in q))
    add(f"  np.subtract(q75,q25) IQR       {iqr:,.2f}")
    cards["median"] = (_money(p_med), "")
    cards["mean"] = (_money(p_mean), "")
    cards["pstd"] = (f"${p_std:,.2f}", "")
    cards["range"] = (f"{_money(p_min)}\n{_money(p_max)}", "")

    # ---------------- returns ----------------
    total = float(close[-1] / close[0] - 1) if n > 1 else 0.0
    mean_sr = float(np.mean(sr)) if sr.size else 0.0
    med_sr = float(np.median(sr)) if sr.size else 0.0
    std_sr = float(np.std(sr, ddof=1)) if sr.size > 1 else 0.0
    up = int(np.sum(sr > 0))
    down = int(np.sum(sr < 0))
    flat = int(np.sum(sr == 0))
    up_ratio = up / sr.size if sr.size else 0.0
    best_i = int(np.argmax(sr)) if sr.size else -1
    worst_i = int(np.argmin(sr)) if sr.size else -1
    best, worst = (float(sr[best_i]), float(sr[worst_i])) if sr.size else (0.0, 0.0)
    ann_ret = math.exp(float(np.mean(lr)) * ann) - 1 if lr.size else 0.0
    ann_vol = float(np.std(lr, ddof=1)) * math.sqrt(ann) if lr.size > 1 else 0.0
    sharpe = (mean_sr / std_sr * math.sqrt(ann)) if std_sr > 0 else 0.0

    peak = np.maximum.accumulate(close)          # running historical peak
    dd = close / np.maximum(peak, 1e-9) - 1.0    # drawdown relative to peak (<=0)
    mdd = float(np.min(dd)) if n else 0.0        # maximum drawdown
    mdd_i = int(np.argmin(dd)) if n else 0       # position of the deepest drawdown
    calmar = (ann_ret / abs(mdd)) if mdd < 0 else float("inf")   # Calmar = annualized / |MDD|

    def _d(idx: int) -> str:
        """Return sr[i] corresponds to the close[i+1] period."""
        return datetime.fromtimestamp(ts[min(idx + 1, n - 1)],
                                      tz=timezone.utc).strftime("%Y-%m-%d")

    log_rule("Return / move stats via NumPy")
    add("【Returns · NumPy】sr = np.diff(close) / close[:-1]")
    add(f"  total return       (close[-1]/close[0]-1)   {_pct(total)}")
    add(f"  np.mean(sr)        avg per-period return    {_pct(mean_sr)}")
    add(f"  np.median(sr)      median per-period        {_pct(med_sr)}")
    add(f"  np.std(sr, ddof=1) per-period volatility    {_pct(std_sr)}")
    add(f"  np.max(sr)         best period              {_pct(best)}   @ {_d(best_i)}")
    add(f"  np.min(sr)         worst period             {_pct(worst)}   @ {_d(worst_i)}")
    add(f"  np.sum(sr>0)/np.sum(sr<0)   up {up} / down {down} / flat {flat}"
        f"   win rate {up_ratio * 100:.2f}%")
    add(f"  longest up / down streak     {_max_streak(sr > 0)} / {_max_streak(sr < 0)} bars")
    add(f"  annualized return (geometric) {_pct(ann_ret)}   (x {ann:g} periods)")
    add(f"  annualized volatility         {ann_vol * 100:,.2f}%")
    add(f"  Sharpe ratio (rf=0)           {sharpe:,.3f}")
    add(f"  max drawdown                  {_pct(mdd)}   @ {_d(mdd_i)}"
        f"   (from peak {_money(float(peak[mdd_i]))})")
    add(f"  Calmar ratio                  {calmar:,.3f}" if math.isfinite(calmar) else "  Calmar ratio                  ∞")
    add(f"  cumulative net value np.prod(1+sr) {float(np.prod(1 + sr)):,.4f}")
    add(f"  np.quantile(sr, [0.05,0.5,0.95])   "
        + " / ".join(f"{x * 100:+.2f}%" for x in np.quantile(sr, [0.05, 0.5, 0.95])))

    c_up = C_UP if total >= 0 else C_DOWN
    c_med = C_UP if med_sr >= 0 else C_DOWN
    c_ann = C_UP if ann_ret >= 0 else C_DOWN
    cards["total"] = (_pct(total), c_up)
    cards["medret"] = (_pct(med_sr), c_med)
    cards["annret"] = (_pct(ann_ret), c_ann)
    cards["annvol"] = (f"{ann_vol * 100:,.2f}%", "")
    cards["mdd"] = (_pct(mdd), C_DOWN)
    cards["sharpe"] = (f"{sharpe:,.3f}", C_UP if sharpe >= 0 else C_DOWN)
    cards["best"] = (_pct(best), C_UP)
    cards["worst"] = (_pct(worst), C_DOWN)
    cards["upratio"] = (f"{up_ratio * 100:.2f}%", C_UP if up_ratio >= 0.5 else C_DOWN)

    # ---------------- distribution shape ----------------
    sk, ku = _skew(lr), _kurt(lr)
    log_rule("Distribution shape via NumPy")
    add("【Distribution · NumPy】")
    add(f"  log return mean / std  {float(np.mean(lr)) if lr.size else 0:+.6f} / "
        f"{float(np.std(lr, ddof=1)) if lr.size > 1 else 0:.6f}")
    add(f"  skewness  Skew         {sk:+.4f}   (>0 right-skewed)")
    add(f"  kurtosis  Excess Kurt  {ku:+.4f}   (>0 fat tails)")
    if n > 1:
        add(f"  np.corrcoef(close, np.arange(n))   price vs. time   "
            f"{float(np.corrcoef(close, np.arange(n))[0, 1]):+.4f}")
    if vol.size > 1 and float(np.std(vol)) > 0:
        add(f"  np.corrcoef(close, volume)         price vs. volume "
            f"{float(np.corrcoef(close, vol)[0, 1]):+.4f}")
    if lr.size > 2:
        add(f"  np.corrcoef(lr[:-1], lr[1:])      return autocorr   "
            f"{float(np.corrcoef(lr[:-1], lr[1:])[0, 1]):+.4f}")
    cards["skew"] = (f"{sk:+.3f}", "")
    cards["kurt"] = (f"{ku:+.3f}", "")

    # ---------------- trend / indicators ----------------
    idx = np.arange(n, dtype=float)             # sample index, used for regression / correlation
    slope, _intercept = np.polyfit(idx, np.log(np.maximum(close, 1e-9)), 1)  # linear trend of log price
    trend_ann = math.exp(float(slope) * ann) - 1      # slope converted to an annualized figure
    grad = float(np.gradient(close)[-1]) if n > 2 else 0.0  # latest marginal change

    def _sma(w: int) -> float:
        return float(np.mean(close[-w:])) if n >= w else float("nan")

    ema12, ema26 = _ema_series(close, 12), _ema_series(close, 26)  # fast / slow EMA
    macd_series = ema12 - ema26                       # MACD (DIF)
    signal = _ema_series(macd_series, 9)              # DEA (signal line)
    rsi14 = _rsi(close, 14)
    boll_w = 20                                       # BOLL window
    if n >= boll_w:
        b_mid = float(np.mean(close[-boll_w:]))       # middle band = SMA20
        b_sd = float(np.std(close[-boll_w:], ddof=0)) # std dev (population σ)
    else:
        b_mid = b_sd = float("nan")

    log_rule("Trend & indicators via NumPy")
    add("【Trend · NumPy】")
    add(f"  np.polyfit(idx, log(close), 1)   trend slope / annualized  {float(slope):+.6f} / {_pct(trend_ann)}")
    add(f"  np.gradient(close)[-1]           latest marginal change   {grad:+,.2f}")
    add(f"  SMA20 / SMA50 / SMA200           "
        f"{_money(_sma(20))} / {_money(_sma(50))} / {_money(_sma(200))}")
    add(f"  EMA12 / EMA26                    {_money(float(ema12[-1]))} / {_money(float(ema26[-1]))}")
    add(f"  MACD / Signal                    {float(macd_series[-1]):+,.2f} / {float(signal[-1]):+,.2f}")
    add(f"  RSI(14)                          {rsi14:.2f}")
    add(f"  BOLL20 mid / ±2σ                 {_money(b_mid)}  ±{b_sd * 2:,.2f}")
    add(f"  avg range np.mean(high-low)      {float(np.mean(hi - lo)):,.2f}"
        f"   ({float(np.mean(hi - lo)) / float(close[-1]) * 100:.2f}% of last close)")
    add("【Volume · NumPy】")
    add(f"  np.sum / np.mean / np.median     {float(np.sum(vol)):,.0f} / "
        f"{float(np.mean(vol)):,.2f} / {float(np.median(vol)):,.2f}")
    cards["rsi"] = (f"{rsi14:.1f}", C_UP if rsi14 >= 50 else C_DOWN)

    add("")
    add(f"  last price {_money(float(close[-1]))}    last bar move "
        f"{_pct(float(close[-1] / close[-2] - 1)) if n > 1 else '—'}")
    add("=" * 74)

    text = "\n".join(lines)
    print(text, flush=True)
    return Report(text=text, cards=cards)


# ============================================================================
# 6. Polynomial regression forecast
# ============================================================================
@dataclass
class FitResult:
    ts_hist: np.ndarray
    y_fit: np.ndarray
    ts_future: np.ndarray
    y_pred: np.ndarray
    upper: np.ndarray
    lower: np.ndarray
    coef: np.ndarray
    degree: int
    log_space: bool
    r2: float
    adj_r2: float
    rmse: float
    mae: float
    mape: float
    sigma: float
    n_points: int
    window: int
    next_value: float
    next_change_pct: float


def run_poly_fit(candles: Sequence[Candle], degree: int, horizon: int,
                 window: int = 0, log_space: bool = False,
                 band_k: float = 1.96) -> FitResult:
    """
    Least-squares fit y = a_n x^n + … + a_0
      * x is normalized to [-1, 1] first, avoiding the ill-conditioned matrix of
        high-degree fits (this is what makes higher degrees usable)
      * log_space=True fits ln(y) instead, matching BTC's long-term exponential growth
    """
    if len(candles) < 4:
        raise ValueError("Not enough data points to fit")
    if window and window > 0:
        if window < degree + 3:
            raise ValueError(f"Fit window cannot be smaller than degree+3 (current {window})")
        candles = candles[-window:]

    n = len(candles)
    if n < degree + 3:
        raise ValueError(f"{n} valid bars are not enough for a degree-{degree} fit (need at least {degree + 3})")

    y = np.asarray([c.close for c in candles], dtype=float)   # dependent variable: close prices
    ts = np.asarray([c.ts for c in candles], dtype=float) / 1000.0  # timestamps in seconds
    if np.any(y <= 0):        # non-positive prices can't be log-transformed; fall back to linear space
        log_space = False

    idx = np.arange(n, dtype=float)
    centre = float(idx.mean())                             # normalization centre
    half = float((idx[-1] - idx[0]) / 2.0) or 1.0          # normalization radius
    X = (idx - centre) / half                              # x ∈ [-1,1], avoids an ill-conditioned matrix
    horizon = max(int(horizon), 0)
    Xf = (np.arange(n, n + horizon, dtype=float) - centre) / half if horizon else np.asarray([])

    target = np.log(y) if log_space else y                 # log space: take ln first
    coef = np.polyfit(X, target, degree)                   # least-squares polynomial coefficients
    fit = np.polyval(coef, X)                              # fitted values over the history
    pred = np.polyval(coef, Xf) if Xf.size else np.asarray([])  # extrapolated future segment
    if log_space:
        fit = np.exp(fit)                                  # convert back to price scale
        pred = np.exp(pred) if pred.size else pred

    resid = y - fit                                        # residual = actual - fitted
    ss_res = float(np.sum(resid ** 2))                     # residual sum of squares
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1e-12   # total sum of squares (avoid div by zero)
    r2 = 1.0 - ss_res / ss_tot                             # coefficient of determination
    k = degree
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / max(n - k - 1, 1)  # degrees-of-freedom adjusted R²
    rmse = float(np.sqrt(ss_res / n))                      # root mean squared error
    mae = float(np.mean(np.abs(resid)))                    # mean absolute error
    mape = float(np.mean(np.abs(resid / np.maximum(y, 1e-9))) * 100.0)  # mean absolute percentage error %
    dof = max(n - k - 1, 1)                                # degrees of freedom
    sigma = float(np.sqrt(ss_res / dof))                   # residual standard error

    step = float(np.median(np.diff(ts))) if n > 1 else 86400.0   # average bar length (seconds)
    ts_future = ts[-1] + step * np.arange(1, horizon + 1) if horizon else np.asarray([])

    if pred.size:
        upper = pred + band_k * sigma                      # upper confidence band
        lower = np.maximum(pred - band_k * sigma, 1e-9)    # lower confidence band (never negative)
        next_value = float(pred[0])                        # next-period forecast
    else:
        upper = lower = np.asarray([])
        next_value = float(fit[-1])

    return FitResult(
        ts_hist=ts, y_fit=fit, ts_future=ts_future, y_pred=pred,
        upper=upper, lower=lower, coef=coef, degree=degree, log_space=log_space,
        r2=r2, adj_r2=adj_r2, rmse=rmse, mae=mae, mape=mape, sigma=sigma,
        n_points=n, window=window or n,
        next_value=next_value,
        next_change_pct=(next_value / float(y[-1]) - 1) * 100.0 if y[-1] else 0.0,
    )


# ============================================================================
# 7. Worker threads
# ============================================================================
class DataWorker(QObject):
    """Fetches market data in the background and reports back via ok / err signals."""
    ok = pyqtSignal(int, object)   # (rid, candles)
    err = pyqtSignal(int, str)     # (rid, error message)

    def __init__(self, rid: int, source: str, symbol: str, tf: Timeframe):
        super().__init__()
        self.rid, self.source, self.symbol, self.tf = rid, source, symbol, tf

    @pyqtSlot()
    def run(self):
        try:
            self.ok.emit(self.rid, load_market(self.source, self.symbol, self.tf))
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if self.source == "Binance":
                msg += " (you can switch the source to OKX / CoinGecko)"   # hint for switching source
            self.err.emit(self.rid, msg)


class FitWorker(QObject):
    """Runs the polynomial regression forecast in the background."""
    ok = pyqtSignal(int, object)   # (rid, FitResult)
    err = pyqtSignal(int, str)

    def __init__(self, rid, candles, degree, horizon, window, log_space):
        super().__init__()
        self.rid, self.candles = rid, candles
        self.degree, self.horizon = degree, horizon
        self.window, self.log_space = window, log_space

    @pyqtSlot()
    def run(self):
        try:
            self.ok.emit(self.rid, run_poly_fit(
                self.candles, self.degree, self.horizon, self.window, self.log_space))
        except Exception as exc:  # noqa: BLE001
            self.err.emit(self.rid, str(exc))


SAFE_BUILTINS = {   # the expression console only exposes safe builtins — no import / open etc.
    "abs": abs, "len": len, "min": min, "max": max, "sum": sum, "round": round,
    "range": range, "float": float, "int": int, "list": list, "tuple": tuple,
    "dict": dict, "sorted": sorted, "enumerate": enumerate, "zip": zip,
    "print": print, "any": any, "all": all, "bool": bool, "str": str,
}


class ExprWorker(QObject):
    """Evaluates a NumPy expression in a worker thread so the UI never freezes."""
    ok = pyqtSignal(int, str, str)   # (rid, expression, formatted result)
    err = pyqtSignal(int, str, str)  # (rid, expression, error message)

    def __init__(self, rid: int, expr: str, env: Dict[str, object]):
        super().__init__()
        self.rid, self.expr, self.env = rid, expr, env

    @pyqtSlot()
    def run(self):
        try:
            with np.errstate(all="ignore"):
                # strictly restricted env: SAFE_BUILTINS + the pre-injected NumPy data environment
                result = eval(self.expr, {"__builtins__": SAFE_BUILTINS}, self.env)  # noqa: S307
            self.ok.emit(self.rid, self.expr, _format_result(result))
        except Exception as exc:  # noqa: BLE001
            self.err.emit(self.rid, self.expr, f"{type(exc).__name__}: {exc}")


def _format_result(value) -> str:
    if isinstance(value, np.ndarray):
        if value.dtype.kind in "fc":
            return np.array2string(value, precision=6, suppress_small=True,
                                   max_line_width=110, threshold=60)
        return np.array2string(value, max_line_width=110, threshold=60)
    if isinstance(value, (np.floating, float)):
        return f"{float(value):,.10g}"
    if isinstance(value, (np.integer, int)):
        return f"{int(value):,}"
    return str(value)


# ============================================================================
# 8. QPicture pre-rendered graphics items
# ============================================================================
class CandlestickItem(pg.GraphicsObject):
    """Custom candlestick chart: pre-rendered into a QPicture for fast zooming/panning."""

    def __init__(self):
        super().__init__()
        self._up, self._down = QColor(C_UP), QColor(C_DOWN)   # green up / red down
        self._bars: List[Tuple[float, float, float, float, float]] = []
        self._pic, self._rect = QPicture(), QRectF()          # pre-render cache + bounding rect

    def setData(self, bars: Sequence[Tuple[float, float, float, float, float]]):
        """Update data: each bar is (x seconds, open, close, low, high)."""
        self._bars = list(bars)
        self._build()
        self.update()

    def _build(self):
        self.prepareGeometryChange()
        self._pic = QPicture()
        bars = self._bars
        if not bars:
            self._rect = QRectF()
            return
        p = QPainter(self._pic)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        span = bars[-1][0] - bars[0][0]                    # total time span
        step = span / max(len(bars) - 1, 1)                # average bar interval
        w = step * 0.62                                    # candle width ≈ 62% of the interval
        for x, o, c, low, high in bars:
            col = self._up if c >= o else self._down       # close>=open is a bullish (green) candle
            pen = QPen(col)
            pen.setWidth(1)
            pen.setCosmetic(True)                          # line width independent of zoom
            p.setPen(pen)
            p.drawLine(QPointF(x, low), QPointF(x, high))  # upper / lower wicks
            p.setBrush(QBrush(col))
            top, bot = max(o, c), min(o, c)
            if top - bot <= 1e-12:                         # doji: open==close, draw a flat line
                p.drawLine(QPointF(x - w / 2, top), QPointF(x + w / 2, top))
            else:
                p.drawRect(QRectF(x - w / 2, bot, w, top - bot))   # body
        p.end()
        y_lo = min(b[3] for b in bars)                     # lowest wick overall
        y_hi = max(b[4] for b in bars)                     # highest wick overall
        self._rect = QRectF(bars[0][0] - w, y_lo, span + 2 * w, (y_hi - y_lo) or 1e-9)

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self._pic)

    def boundingRect(self) -> QRectF:
        return self._rect


class VolumeItem(pg.GraphicsObject):
    """Volume bars, colored by up/down sentiment (green up, red down)."""
    def __init__(self):
        super().__init__()
        self._bars: List[Tuple[float, float, bool]] = []   # (x seconds, volume, is_up)
        self._pic, self._rect = QPicture(), QRectF()

    def setData(self, bars: Sequence[Tuple[float, float, bool]]):
        self._bars = list(bars)
        self._build()
        self.update()

    def _build(self):
        self.prepareGeometryChange()
        self._pic = QPicture()
        bars = self._bars
        if not bars:
            self._rect = QRectF()
            return
        p = QPainter(self._pic)
        span = bars[-1][0] - bars[0][0]                    # time span
        step = span / max(len(bars) - 1, 1)                # bar interval
        w = step * 0.62                                    # bar width
        vmax = max(b[1] for b in bars) or 1.0              # max volume, drives the bounding rect
        for x, v, is_up in bars:
            col = QColor(C_UP if is_up else C_DOWN)
            col.setAlpha(165)                              # semi-transparent
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))
            p.drawRect(QRectF(x - w / 2, 0.0, w, v))       # draw up from zero
        p.end()
        self._rect = QRectF(bars[0][0] - w, 0.0, span + 2 * w, vmax * 1.08)

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self._pic)

    def boundingRect(self) -> QRectF:
        return self._rect


def _render_degree_formula(degree: int) -> str:
    sup = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    head = " + ".join(f"a{str(p).translate(sup)}x{str(p).translate(sup)}"
                      for p in range(degree, max(degree - 2, 0) - 1, -1))
    return f"y = {head} + … + a₀"


# ============================================================================
# 9. Main window
# ============================================================================
STAT_CARDS = [
    ("median", "Median np.median"), ("mean", "Mean np.mean"),
    ("pstd", "Price Std np.std"), ("range", "Min / Max"),
    ("total", "Total Return"), ("medret", "Median Return"),
    ("annret", "Ann. Return (Geo)"), ("annvol", "Ann. Volatility"),
    ("mdd", "Max Drawdown"), ("sharpe", "Sharpe Ratio"),
    ("best", "Best Period"), ("worst", "Worst Period"),
    ("upratio", "Win Rate"), ("rsi", "RSI(14)"),
    ("skew", "Skewness"), ("kurt", "Kurtosis"),
]

QUICK_EXPRS = [
    "np.median(close)", "np.mean(close)", "np.std(close, ddof=1)",
    "np.percentile(close, [25, 50, 75])", "np.quantile(ret, 0.5)",
    "np.median(ret)", "np.std(ret, ddof=1) * np.sqrt(365)",
    "np.max(ret)", "np.min(ret)", "np.sum(ret > 0) / ret.size",
    "np.corrcoef(close, volume)[0,1]", "np.corrcoef(ret[:-1], ret[1:])[0,1]",
    "np.polyfit(np.arange(n), np.log(close), 1)", "np.gradient(close)[-5:]",
    "np.cumsum(ret)[-1]", "np.maximum.accumulate(close)[-1]",
    "np.histogram(ret, bins=10)[0]", "np.diff(close)[-5:]",
    "np.argmax(close), np.argmin(close)",
    "np.trapz(np.log(close)) if hasattr(np, 'trapz') else np.trapezoid(np.log(close))",
]


class MainWindow(QMainWindow):
    """Main window: chart on the left + four tabs on the right."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BTC Market Lab v2.2 · Stats / NumPy / Polynomial Regression")
        self.resize(1520, 940)
        self.setMinimumSize(1180, 780)

        self.candles: List[Candle] = []          # current candle data
        self.tf: Timeframe = TF_BY_KEY["1d"]     # current timeframe
        self._xs = np.asarray([])                # second timestamps (for crosshair position)
        self._data_rid = 0                       # auto-increment id; stale results are dropped
        self._fit_rid = 0                        # fit request auto-increment id
        self._expr_rid = 0                       # expression request auto-increment id
        self._threads: List[QThread] = []        # alive background threads
        self._workers: List[QObject] = []        # strong references to workers (avoid GC)
        self._last_fit: FitResult | None = None  # most recent fit result
        self._loading = False                    # is a data request in flight
        self._report = Report()                  # most recent statistics report

        self._build_ui()
        self._apply_style()

        # debounce after parameter changes: only the last trigger within 320 ms runs
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(320)
        self._auto_timer.timeout.connect(self.start_fit)

        QTimer.singleShot(80, self.refresh_data)  # fetch data automatically after startup

    # ================================================================ UI
    def _build_ui(self):
        root_widget = QWidget()
        root_widget.setObjectName("Root")
        root = QHBoxLayout(root_widget)
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(14)

        root.addWidget(self._build_chart_panel(), 1)
        root.addWidget(self._build_side_tabs())

        self.setCentralWidget(root_widget)

        bar = QStatusBar()
        self.setStatusBar(bar)
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setObjectName("Status")
        self.lbl_net = QLabel("Network: Not connected")
        self.lbl_net.setObjectName("Status")
        self.bar_busy = QProgressBar()
        self.bar_busy.setRange(0, 0)
        self.bar_busy.setFixedWidth(110)
        self.bar_busy.setTextVisible(False)
        self.bar_busy.hide()
        bar.addWidget(self.lbl_status, 1)
        bar.addPermanentWidget(self.lbl_net)
        bar.addPermanentWidget(self.bar_busy)

    # ---------------------------------------------------- left chart
    def _build_chart_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(14)
        self.lbl_title = QLabel(f"BTC · {self.tf.label}")
        self.lbl_title.setObjectName("ChartTitle")
        self.lbl_legend = QLabel(
            f'<span style="color:{C_ACCENT}">━</span> Price  '
            f'<span style="color:{C_FIT}">━</span> Poly-fit  '
            f'<span style="color:{C_PRED}">┄</span> Forecast  '
            f'<span style="color:{C_UP}">●</span>up<span style="color:{C_DOWN}">●</span>down')
        self.lbl_legend.setObjectName("LegendText")
        head.addWidget(self.lbl_title)
        head.addStretch(1)
        head.addWidget(self.lbl_legend)
        lay.addLayout(head)

        toggles = QFrame()
        toggles.setObjectName("ToggleBar")
        tl = QHBoxLayout(toggles)
        tl.setContentsMargins(12, 6, 12, 6)
        tl.setSpacing(18)
        self.chk_candle = QCheckBox("Candles")
        self.chk_candle.setChecked(True)
        self.chk_log_y = QCheckBox("Log Y")
        self.chk_grid = QCheckBox("Grid")
        self.chk_grid.setChecked(True)
        self.chk_cross = QCheckBox("Crosshair")
        self.chk_cross.setChecked(True)
        for w in (self.chk_candle, self.chk_log_y, self.chk_grid, self.chk_cross):
            tl.addWidget(w)
        tl.addStretch(1)
        self.chk_candle.toggled.connect(self._sync_view)
        self.chk_cross.toggled.connect(self._sync_view)
        self.chk_log_y.toggled.connect(self._on_log_y_changed)
        self.chk_grid.toggled.connect(
            lambda on: self.plot.showGrid(x=on, y=on, alpha=0.12))
        lay.addWidget(toggles)

        self.lbl_cursor = QLabel("Hover over the chart to inspect candle details")
        self.lbl_cursor.setObjectName("Cursor")
        lay.addWidget(self.lbl_cursor)

        # ---- main chart ----
        axis_bottom = pg.DateAxisItem(orientation="bottom")   # bottom time axis
        self.plot = pg.PlotWidget(axisItems={"bottom": axis_bottom})
        self.plot.setBackground(C_BG)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getAxis("left").setWidth(76)
        for name in ("left", "bottom"):
            ax = self.plot.getAxis(name)
            ax.setPen(pg.mkPen(C_BORDER))        # axis line color
            ax.setTextPen(pg.mkPen(C_MUTED))     # tick label color
        self.plot.getAxis("bottom").setStyle(showValues=False)  # dates shown by the volume sub-plot
        self.plot.getViewBox().setDefaultPadding(0.03)

        self.item_candles = CandlestickItem()               # candlestick item
        self.plot.addItem(self.item_candles)

        self.curve_close = pg.PlotDataItem(pen=pg.mkPen(C_ACCENT, width=2.2))  # close line (replaces candles in log mode)
        self.curve_close.setDownsampling(auto=True, method="peak")  # downsample large datasets
        self.curve_close.setClipToView(True)               # draw only the visible region
        self.plot.addItem(self.curve_close)

        self.curve_fit = pg.PlotDataItem(pen=pg.mkPen(C_FIT, width=2))   # fitted history curve
        self.curve_fit.setClipToView(True)
        self.plot.addItem(self.curve_fit)

        self.curve_up = pg.PlotDataItem(pen=pg.mkPen(None))   # upper band boundary (fill only)
        self.curve_low = pg.PlotDataItem(pen=pg.mkPen(None))  # lower band boundary
        self.plot.addItem(self.curve_up)
        self.plot.addItem(self.curve_low)
        self.band = pg.FillBetweenItem(self.curve_up, self.curve_low,   # fill between the two boundaries
                                       brush=pg.mkBrush(*C_BAND))
        self.plot.addItem(self.band)

        self.curve_pred = pg.PlotDataItem(                     # extrapolated forecast (dashed)
            pen=pg.mkPen(C_PRED, width=2.2, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.curve_pred)

        self.dot_pred = pg.ScatterPlotItem(size=12, brush=pg.mkBrush(C_PRED),  # forecast end-point marker
                                           pen=pg.mkPen(C_BG, width=2), symbol="o")
        self.plot.addItem(self.dot_pred)

        self.vline = pg.InfiniteLine(       # crosshair: vertical reference line
            angle=90, movable=False,
            pen=pg.mkPen("#5d6b7d", width=1, style=Qt.PenStyle.DashLine))
        self.hline = pg.InfiniteLine(       # crosshair: horizontal reference line
            angle=0, movable=False,
            pen=pg.mkPen("#5d6b7d", width=1, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)
        lay.addWidget(self.plot, 5)

        # ---- volume sub-plot ----
        self.vol_plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.vol_plot.setBackground(C_BG)
        self.vol_plot.setFixedHeight(132)
        self.vol_plot.getAxis("left").setWidth(76)
        self.vol_plot.getAxis("left").setLabel("Volume")
        for name in ("left", "bottom"):
            ax = self.vol_plot.getAxis(name)
            ax.setPen(pg.mkPen(C_BORDER))
            ax.setTextPen(pg.mkPen(C_MUTED))
        self.vol_plot.setMouseEnabled(x=True, y=False)
        self.vol_plot.setXLink(self.plot)
        self.item_volume = VolumeItem()
        self.vol_plot.addItem(self.item_volume)
        lay.addWidget(self.vol_plot, 1)

        self._set_overlay_visible(False)
        self.proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=60,
                                    slot=self._on_mouse_move)
        return panel

    # ---------------------------------------------------- right tabs
    def _build_side_tabs(self) -> QWidget:
        tabs = QTabWidget()
        tabs.setObjectName("SideTabs")
        tabs.setFixedWidth(408)
        tabs.setDocumentMode(True)
        tabs.addTab(self._wrap(self._tab_market()), "Market")
        tabs.addTab(self._wrap(self._tab_fit()), "Forecast")
        tabs.addTab(self._wrap(self._tab_stats()), "Stats")
        tabs.addTab(self._wrap(self._tab_numpy()), "NumPy")
        self.tabs = tabs
        return tabs

    @staticmethod
    def _wrap(inner: QWidget) -> QScrollArea:
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setWidget(inner)
        return sc

    def _panel(self, title: str) -> Tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Panel2")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(12)
        lbl = QLabel(title)
        lbl.setObjectName("PanelTitle")
        lay.addWidget(lbl)
        return frame, lay

    def _field(self, title: str, widget: QWidget) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        lbl = QLabel(title)
        lbl.setObjectName("FieldLabel")
        v.addWidget(lbl)
        v.addWidget(widget)
        return box

    def _card(self, title: str) -> Tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        val = QLabel("—")
        val.setObjectName("CardValue")
        val.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(val)
        return card, val

    # ---- Tab1 Market ----
    def _tab_market(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("Data Source")
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.cb_source = QComboBox()
        self.cb_source.addItems(["Binance", "OKX", "CoinGecko"])
        self.cb_source.setToolTip("Uses proxy 127.0.0.1:10808 by default; falls back to a direct connection")
        self.cb_tf = QComboBox()
        self.cb_tf.addItems([tf.label for tf in TIMEFRAMES])
        self.cb_tf.setCurrentIndex(2)
        row1.addWidget(self._field("Source", self.cb_source), 1)
        row1.addWidget(self._field("Timeframe", self.cb_tf), 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.ed_symbol = QLineEdit("BTCUSDT")
        self.btn_refresh = QPushButton("Refresh Data")
        self.btn_refresh.setObjectName("Primary")
        self.btn_refresh.clicked.connect(self.refresh_data)
        row2.addWidget(self._field("Pair", self.ed_symbol), 1)
        row2.addWidget(self.btn_refresh)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(10)
        self.btn_reconnect = QPushButton("Retest Network")
        self.btn_reconnect.setObjectName("Ghost")
        self.btn_reconnect.setToolTip("Forget the remembered proxy strategy and re-probe from 127.0.0.1:10808")
        self.btn_reconnect.clicked.connect(self._reset_network)
        row3.addWidget(self.btn_reconnect)
        row3.addStretch(1)
        lay.addLayout(row3)
        v.addWidget(frame)

        frame2, lay2 = self._panel("Key Metrics")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.cards: Dict[str, QLabel] = {}
        quick_keys = [("last", "Last Price"), ("change", "Change"),
                      ("vol", "Ann. Vol"), ("bars", "Bars")]
        for i, (key, title) in enumerate(quick_keys):
            card, val = self._card(title)
            grid.addWidget(card, i // 2, i % 2)
            self.cards[key] = val
        lay2.addLayout(grid)
        self.lbl_range = QLabel("—")
        self.lbl_range.setObjectName("Hint")
        self.lbl_range.setWordWrap(True)
        lay2.addWidget(self.lbl_range)
        v.addWidget(frame2)

        self.cb_tf.currentIndexChanged.connect(self._on_tf_changed)
        v.addStretch(1)
        return holder

    # ---- Tab2 Forecast ----
    def _tab_fit(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("Polynomial Regression")
        deg_row = QHBoxLayout()
        deg_row.setSpacing(12)
        self.sp_degree = QSpinBox()
        self.sp_degree.setRange(1, 24)
        self.sp_degree.setValue(5)
        self.sp_degree.setFixedWidth(74)
        self.sld_degree = QSlider(Qt.Orientation.Horizontal)
        self.sld_degree.setRange(1, 24)
        self.sld_degree.setValue(5)
        self.sp_degree.valueChanged.connect(self.sld_degree.setValue)
        self.sld_degree.valueChanged.connect(self.sp_degree.setValue)
        lbl = QLabel("Degree n")
        lbl.setMinimumWidth(58)
        deg_row.addWidget(lbl)
        deg_row.addWidget(self.sld_degree, 1)
        deg_row.addWidget(self.sp_degree)
        lay.addLayout(deg_row)

        self.lbl_formula = QLabel(_render_degree_formula(5))
        self.lbl_formula.setObjectName("Formula")
        self.lbl_formula.setWordWrap(True)
        lay.addWidget(self.lbl_formula)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.sp_horizon = QSpinBox()
        self.sp_horizon.setRange(0, 500)
        self.sp_horizon.setValue(30)
        self.sp_horizon.setSuffix(" bars")
        self.sp_window = QSpinBox()
        self.sp_window.setRange(0, 20000)
        self.sp_window.setValue(0)
        self.sp_window.setSuffix(" bars")
        for w in (self.sp_horizon, self.sp_window):
            w.setMinimumWidth(120)
        grid.addWidget(QLabel("Extrapolate"), 0, 0)
        grid.addWidget(self.sp_horizon, 0, 1)
        grid.addWidget(QLabel("Fit Window"), 1, 0)
        grid.addWidget(self.sp_window, 1, 1)
        lay.addLayout(grid)

        hint = QLabel("Fit window 0 = use all historical data")
        hint.setObjectName("Hint")
        lay.addWidget(hint)

        self.chk_log_fit = QCheckBox("Log-space fit (recommended for BTC long-term)")
        self.chk_log_fit.setChecked(True)
        self.chk_auto = QCheckBox("Auto re-forecast on parameter change")
        self.chk_auto.setChecked(True)
        lay.addWidget(self.chk_log_fit)
        lay.addWidget(self.chk_auto)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_fit = QPushButton("Run Polynomial Fit")
        self.btn_fit.setObjectName("Primary")
        self.btn_fit.clicked.connect(self.start_fit)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setObjectName("Ghost")
        self.btn_clear.clicked.connect(self.clear_forecast)
        btns.addWidget(self.btn_fit, 2)
        btns.addWidget(self.btn_clear, 1)
        lay.addLayout(btns)

        self.sp_degree.valueChanged.connect(self._on_degree_changed)
        for w in (self.sp_horizon, self.sp_window):
            w.valueChanged.connect(self._schedule_auto_fit)
        self.chk_log_fit.toggled.connect(self._schedule_auto_fit)
        v.addWidget(frame)

        frame2, lay2 = self._panel("Fit Quality")
        res_grid = QGridLayout()
        res_grid.setHorizontalSpacing(10)
        res_grid.setVerticalSpacing(8)
        self.res_labels: Dict[str, QLabel] = {}
        items = [("r2", "R² Goodness of Fit"), ("adj", "Adjusted R²"),
                 ("rmse", "RMSE Root Mean Sq."), ("mae", "MAE Mean Abs. Error"),
                 ("mape", "MAPE Mean Abs. %"), ("sigma", "Residual Std Err σ"),
                 ("samples", "Samples Used"), ("next", "Next Forecast")]
        for i, (key, text) in enumerate(items):
            t = QLabel(text)
            t.setObjectName("CardTitle")
            val = QLabel("—")
            val.setObjectName("ResultValue")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            res_grid.addWidget(t, i, 0)
            res_grid.addWidget(val, i, 1, alignment=Qt.AlignmentFlag.AlignRight)
            self.res_labels[key] = val
        lay2.addLayout(res_grid)
        v.addWidget(frame2)
        v.addStretch(1)
        return holder

    # ---- Tab3 Stats ----
    def _tab_stats(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("Key Stats (NumPy)")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.stat_cards: Dict[str, QLabel] = {}
        for i, (key, title) in enumerate(STAT_CARDS):
            card, val = self._card(title)
            grid.addWidget(card, i // 2, i % 2)
            self.stat_cards[key] = val
        lay.addLayout(grid)
        v.addWidget(frame)

        frame2, lay2 = self._panel("Full Report")
        self.txt_report = QPlainTextEdit()
        self.txt_report.setObjectName("Mono")
        self.txt_report.setReadOnly(True)
        self.txt_report.setMinimumHeight(340)
        self.txt_report.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay2.addWidget(self.txt_report)

        row = QHBoxLayout()
        row.setSpacing(10)
        btn_print = QPushButton("Recalc & Print")
        btn_print.setObjectName("Primary")
        btn_print.clicked.connect(self.recompute_report)
        btn_copy = QPushButton("Copy Report")
        btn_copy.setObjectName("Ghost")
        btn_copy.clicked.connect(self._copy_report)
        row.addWidget(btn_print, 2)
        row.addWidget(btn_copy, 1)
        lay2.addLayout(row)
        v.addWidget(frame2)
        v.addStretch(1)
        return holder

    # ---- Tab4 NumPy console ----
    def _tab_numpy(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("NumPy Expression")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.ed_expr = QLineEdit("np.median(close)")
        self.ed_expr.setPlaceholderText("e.g. np.percentile(close,[25,50,75])")
        self.ed_expr.returnPressed.connect(self.run_expression)
        btn = QPushButton("Run")
        btn.setObjectName("Primary")
        btn.clicked.connect(self.run_expression)
        row.addWidget(self.ed_expr, 1)
        row.addWidget(btn)
        lay.addLayout(row)

        hint = QLabel("Available: np / close / open / high / low / volume / ret / logret / idx / n / ts")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        v.addWidget(frame)

        frame2, lay2 = self._panel("Quick Expressions")
        qgrid = QGridLayout()
        qgrid.setHorizontalSpacing(8)
        qgrid.setVerticalSpacing(8)
        for i, expr in enumerate(QUICK_EXPRS):
            b = QPushButton(expr)
            b.setObjectName("Chip")
            b.setToolTip("Click to run")
            b.clicked.connect(lambda _=False, e=expr: self._use_expr(e))
            qgrid.addWidget(b, i // 2, i % 2)
        lay2.addLayout(qgrid)
        v.addWidget(frame2)

        frame3, lay3 = self._panel("Output")
        self.txt_expr = QPlainTextEdit()
        self.txt_expr.setObjectName("Mono")
        self.txt_expr.setReadOnly(True)
        self.txt_expr.setMinimumHeight(260)
        lay3.addWidget(self.txt_expr)
        btn_clear = QPushButton("Clear Output")
        btn_clear.setObjectName("Ghost")
        btn_clear.clicked.connect(self.txt_expr.clear)
        lay3.addWidget(btn_clear)
        v.addWidget(frame3)
        v.addStretch(1)
        return holder

    # ================================================================ style
    def _apply_style(self):
        self.setStyleSheet(f"""
        QMainWindow, QWidget#Root, QWidget#SideHolder {{ background: {C_BG}; }}

        QFrame#Panel {{
            background: {C_PANEL}; border: 1px solid {C_BORDER}; border-radius: 16px;
        }}
        QFrame#Panel2 {{
            background: {C_PANEL_2}; border: 1px solid {C_BORDER}; border-radius: 14px;
        }}
        QFrame#Card {{
            background: {C_INPUT}; border: 1px solid {C_BORDER}; border-radius: 11px;
        }}
        QFrame#ToggleBar {{
            background: {C_INPUT}; border: 1px solid {C_BORDER}; border-radius: 10px;
        }}

        QLabel {{ color: {C_TEXT}; font-size: 13px; }}
        QLabel#ChartTitle {{ font-size: 18px; font-weight: 700; }}
        QLabel#PanelTitle {{ font-size: 14px; font-weight: 700; padding-bottom: 2px; }}
        QLabel#LegendText {{ font-size: 12px; color: {C_MUTED}; }}
        QLabel#Cursor {{
            font-size: 13px; color: #b6c2d1; background: {C_INPUT};
            border: 1px solid {C_BORDER}; border-radius: 9px; padding: 8px 12px;
        }}
        QLabel#Hint {{ font-size: 12px; color: {C_MUTED}; }}
        QLabel#FieldLabel {{ font-size: 12px; color: {C_MUTED}; }}
        QLabel#CardTitle {{ font-size: 12px; color: {C_MUTED}; }}
        QLabel#CardValue {{ font-size: 17px; font-weight: 700; }}
        QLabel#ResultValue {{ font-size: 14px; font-weight: 700; color: {C_ACCENT}; }}
        QLabel#Formula {{
            font-size: 13px; color: {C_FIT}; background: {C_INPUT};
            border: 1px solid {C_BORDER}; border-radius: 9px; padding: 8px 10px;
        }}
        QLabel#Status {{ color: {C_MUTED}; font-size: 12px; padding-left: 8px; }}

        QPushButton {{
            font-size: 13px; border-radius: 10px; padding: 9px 14px; color: {C_TEXT};
            background: #1c2634; border: 1px solid #2b3849;
        }}
        QPushButton:hover {{ background: #253242; }}
        QPushButton:pressed {{ background: #18212e; }}
        QPushButton:disabled {{ color: #5c6a7b; background: #151c26; border-color: #1f2734; }}
        QPushButton#Primary {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 {C_ACCENT}, stop:1 #d97c0c);
            color: #17120a; border: none; font-weight: 700;
        }}
        QPushButton#Primary:hover {{ background: #ffa733; }}
        QPushButton#Primary:pressed {{ background: #d97c0c; }}
        QPushButton#Primary:disabled {{ background: #3a3226; color: #7d6f59; }}
        QPushButton#Ghost {{ background: transparent; border: 1px solid #2b3849; }}
        QPushButton#Ghost:hover {{ background: #1c2634; }}
        QPushButton#Chip {{
            font-size: 11px; padding: 6px 8px; border-radius: 8px;
            background: {C_INPUT}; color: #9fb0c3; border: 1px solid {C_BORDER};
            text-align: left;
        }}
        QPushButton#Chip:hover {{ color: {C_TEXT}; border-color: #3b4d66; background: #16202c; }}

        QComboBox, QSpinBox, QLineEdit {{
            background: {C_INPUT}; border: 1px solid {C_BORDER}; border-radius: 10px;
            padding: 8px 10px; color: {C_TEXT}; font-size: 13px;
            selection-background-color: {C_ACCENT}; selection-color: #17120a;
        }}
        QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: #3b4d66; }}
        QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {C_ACCENT}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox::down-arrow {{
            image: none; width: 0; height: 0; margin-right: 9px;
            border-left: 5px solid transparent; border-right: 5px solid transparent;
            border-top: 6px solid {C_MUTED};
        }}
        QComboBox QAbstractItemView {{
            background: {C_INPUT}; color: {C_TEXT}; outline: none;
            border: 1px solid {C_BORDER}; selection-background-color: #253242;
            padding: 4px;
        }}
        QSpinBox::up-button, QSpinBox::down-button {{
            subcontrol-origin: border; width: 20px; background: transparent;
            border-left: 1px solid {C_BORDER};
        }}
        QSpinBox::up-button {{ subcontrol-position: top right; }}
        QSpinBox::down-button {{ subcontrol-position: bottom right; }}
        QSpinBox::up-arrow {{
            image: none; width: 0; height: 0;
            border-left: 4px solid transparent; border-right: 4px solid transparent;
            border-bottom: 5px solid {C_MUTED};
        }}
        QSpinBox::down-arrow {{
            image: none; width: 0; height: 0;
            border-left: 4px solid transparent; border-right: 4px solid transparent;
            border-top: 5px solid {C_MUTED};
        }}

        QCheckBox {{ color: {C_TEXT}; font-size: 13px; spacing: 9px; }}
        QCheckBox::indicator {{
            width: 16px; height: 16px; border-radius: 5px;
            border: 1px solid #2f3d4f; background: {C_INPUT};
        }}
        QCheckBox::indicator:hover {{ border-color: #45536a; }}
        QCheckBox::indicator:checked {{ background: {C_ACCENT}; border-color: {C_ACCENT}; }}
        QCheckBox:disabled {{ color: #5c6a7b; }}

        QSlider::groove:horizontal {{ height: 5px; background: #1e2836; border-radius: 3px; }}
        QSlider::sub-page:horizontal {{ background: {C_ACCENT}; border-radius: 3px; }}
        QSlider::handle:horizontal {{
            width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
            background: #ffd08a; border: 1px solid #b9741a;
        }}

        QPlainTextEdit#Mono {{
            background: #0a0f16; border: 1px solid {C_BORDER}; border-radius: 10px;
            color: #cfe3f5; font-family: "JetBrains Mono", "Cascadia Mono", "Consolas",
                                     "DejaVu Sans Mono", monospace;
            font-size: 12px; padding: 10px;
        }}

        QTabWidget#SideTabs::pane {{
            border: 1px solid {C_BORDER}; background: {C_PANEL};
            border-radius: 14px; top: -1px; padding: 8px;
        }}
        QTabWidget#SideTabs::tab-bar {{ alignment: left; }}
        QTabBar::tab {{
            background: transparent; color: {C_MUTED}; padding: 9px 16px;
            font-size: 13px; border-bottom: 2px solid transparent; margin-right: 2px;
        }}
        QTabBar::tab:hover {{ color: {C_TEXT}; }}
        QTabBar::tab:selected {{ color: {C_TEXT}; font-weight: 700; border-bottom: 2px solid {C_ACCENT}; }}

        QScrollArea {{ background: transparent; border: none; }}
        QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: #2a3644; border-radius: 4px; min-height: 32px; }}
        QScrollBar::handle:vertical:hover {{ background: #3a4b60; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QScrollBar:horizontal {{ background: transparent; height: 9px; }}
        QScrollBar::handle:horizontal {{ background: #2a3644; border-radius: 4px; min-width: 32px; }}

        QStatusBar {{ background: #0d131b; border-top: 1px solid #1d2531; }}
        QProgressBar {{
            border: none; background: #17212d; border-radius: 5px;
            height: 7px; margin-right: 12px;
        }}
        QProgressBar::chunk {{ background: {C_ACCENT}; border-radius: 5px; }}
        QToolTip {{
            background: #17212d; color: {C_TEXT}; border: 1px solid {C_BORDER};
            padding: 5px 8px; border-radius: 7px; font-size: 12px;
        }}
        """)

    # ================================================================ threads
    def _run_worker(self, worker: QObject, ok_slot, err_slot):
        # Note: the worker must be strongly referenced by Python, otherwise it is
        # garbage-collected right after _run_worker returns, the thread.started
        # connection dies with it, and worker.run never executes.
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ok.connect(ok_slot)
        worker.err.connect(err_slot)
        worker.ok.connect(thread.quit)
        worker.err.connect(thread.quit)
        thread.finished.connect(lambda w=worker, t=thread: self._drop_worker(w, t))
        self._threads.append(thread)
        self._workers.append(worker)
        thread.start()

    def _drop_worker(self, worker: QObject, thread: QThread):
        # The thread has finished; only now is it safe to release the refs.
        if worker in self._workers:
            self._workers.remove(worker)
        if thread in self._threads:
            self._threads.remove(thread)
        worker.deleteLater()
        thread.deleteLater()

    def _set_loading(self, on: bool, text: str = ""):
        """Switch to busy / idle state: disable widgets that would trigger duplicate requests."""
        self._loading = on
        self.bar_busy.setVisible(on)
        for w in (self.btn_refresh, self.cb_source, self.cb_tf, self.ed_symbol):
            w.setEnabled(not on)
        if text:
            self.lbl_status.setText(text)

    def _refresh_net_label(self):
        """Bottom-right network status: show the remembered strategy, or a failure hint."""
        name = HTTP.preferred_name()
        if name:
            self.lbl_net.setText(f"Network: {name}")
        elif HTTP.last_error:
            self.lbl_net.setText("Network: All strategies failed")
        else:
            self.lbl_net.setText("Network: Not connected")

    # ============================================================ data
    def _reset_network(self):
        HTTP.reset()
        self._refresh_net_label()
        self.lbl_status.setText("Network strategies reset; next request re-probes from proxy 127.0.0.1:10808")

    def _on_tf_changed(self, index: int):
        self.tf = TIMEFRAMES[index]
        self.lbl_title.setText(f"BTC · {self.tf.label}")
        self.refresh_data()

    def refresh_data(self):
        """Called on startup / timeframe change / source change / refresh button: fetch candles in a thread."""
        if self._loading:
            return                            # a request is already in flight; ignore duplicate clicks
        self._data_rid += 1
        rid = self._data_rid
        source = self.cb_source.currentText()
        symbol = self.ed_symbol.text().strip() or "BTCUSDT"
        tf = self.tf
        self.lbl_net.setText("Network: Connecting…")
        self._set_loading(True, f"Fetching {tf.label} data from {source} (proxy-first 127.0.0.1:10808)…")
        self._run_worker(DataWorker(rid, source, symbol, tf),
                         self._on_data_ok, self._on_data_err)

    def _on_data_ok(self, rid: int, candles):
        if rid != self._data_rid:
            return                            # superseded by a newer request; drop it
        self._set_loading(False)
        self._refresh_net_label()
        self.candles = candles
        self._xs = np.asarray([c.ts / 1000.0 for c in candles], dtype=float)  # seconds, used by the crosshair

        self.item_candles.setData([(c.ts / 1000.0, c.open, c.close, c.low, c.high)
                                   for c in candles])   # refresh candles
        self.item_volume.setData([(c.ts / 1000.0, max(c.volume, 0.0), c.close >= c.open)
                                  for c in candles])   # refresh volume

        close = np.asarray([c.close for c in candles], dtype=float)
        self.curve_close.setData(self._xs, close, fillLevel=float(close.min()) * 0.97,
                                 brush=pg.mkBrush(247, 147, 26, 26))  # orange area fill

        self.clear_forecast(keep_status=True)  # new data invalidates the old forecast
        self._sync_view()
        self._update_quick_stats()
        self.vol_plot.autoRange(padding=0.02)
        self.plot.autoRange(padding=0.03)

        st = datetime.fromtimestamp(candles[0].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        en = datetime.fromtimestamp(candles[-1].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        self.lbl_status.setText(
            f"{self.cb_source.currentText()} · {self.tf.label} · {len(candles)} bars · {st} → {en}")

        self.recompute_report()               # recompute stat cards + console report

        if self.chk_auto.isChecked():
            self._auto_timer.start()          # auto re-run one fit

    def _on_data_err(self, rid: int, msg: str):
        if rid != self._data_rid:
            return
        self._set_loading(False)
        self._refresh_net_label()
        log(f"Data fetch failed: {msg}")
        self.lbl_status.setText(f"⚠ Data fetch failed: {msg}")
        self.lbl_cursor.setText(f"⚠ Data fetch failed: {msg}")

    def _update_quick_stats(self):
        """The four small cards on the Market tab: last / change / ann. vol / bar count."""
        close = np.asarray([c.close for c in self.candles], dtype=float)
        lr = np.diff(np.log(np.maximum(close, 1e-9)))
        total = float(close[-1] / close[0] - 1) * 100                # period change %
        vol = (float(np.std(lr, ddof=1)) * math.sqrt(self.tf.periods_per_year) * 100
               if lr.size > 1 else 0.0)                              # annualized volatility %
        self.cards["last"].setText(f"${close[-1]:,.2f}")
        self.cards["change"].setText(f"{total:+.2f}%")
        self.cards["change"].setStyleSheet(
            f"color:{C_UP if total >= 0 else C_DOWN}; font-size:17px; font-weight:700;")
        self.cards["vol"].setText(f"{vol:,.1f}%")
        self.cards["bars"].setText(str(len(self.candles)))
        s = datetime.fromtimestamp(self.candles[0].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        e = datetime.fromtimestamp(self.candles[-1].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        self.lbl_range.setText(f"Range {s} → {e}  Median "
                               f"${float(np.median(close)):,.2f}  Mean ${float(np.mean(close)):,.2f}")

    # ============================================================ stats report
    def recompute_report(self):
        """Recompute every statistic: fill the cards + refresh the console report text."""
        if not self.candles:
            self.lbl_status.setText("Please refresh data first")
            return
        log_rule(f"Recalculating stats · {self.cb_source.currentText()} · {self.tf.label}")
        self._report = build_report(self.candles, self.tf,
                                    self.cb_source.currentText(),
                                    self.ed_symbol.text().strip() or "BTCUSDT")
        self.txt_report.setPlainText(self._report.text)      # full text of the report tab
        for key, (value, color) in self._report.cards.items():   # stat cards at the top of the Market tab
            lbl = self.stat_cards.get(key)
            if lbl is None:
                continue
            lbl.setText(value)
            if color:
                lbl.setStyleSheet(f"color:{color}; font-size:17px; font-weight:700;")
            else:
                lbl.setStyleSheet("")
        self.lbl_status.setText("Stats done; results printed to the console")

    def _copy_report(self):
        if self._report.text:
            QApplication.clipboard().setText(self._report.text)
            self.lbl_status.setText("Report copied to clipboard")

    # ============================================================ fitting
    def _on_degree_changed(self, value: int):
        self.lbl_formula.setText(_render_degree_formula(value))
        self._schedule_auto_fit()

    def _schedule_auto_fit(self, *args):
        if self.chk_auto.isChecked() and self.candles:
            self._auto_timer.start()

    def start_fit(self):
        """Start a background polynomial fit with the current parameters (degree / horizon / window)."""
        if not self.candles:
            self.lbl_status.setText("Please refresh data first")
            return
        self._fit_rid += 1
        rid = self._fit_rid
        self.lbl_status.setText(f"Fitting a degree-{self.sp_degree.value()} polynomial…")
        log_rule(f"Polynomial regression · n={self.sp_degree.value()} · horizon {self.sp_horizon.value()} bars "
                 f"· window {self.sp_window.value() or 'all'} · "
                 f"{'log space' if self.chk_log_fit.isChecked() else 'linear space'}")
        self._run_worker(
            FitWorker(rid, self.candles, self.sp_degree.value(), self.sp_horizon.value(),
                      self.sp_window.value(), self.chk_log_fit.isChecked()),
            self._on_fit_ok, self._on_fit_err)

    def _on_fit_ok(self, rid: int, r: FitResult):
        if rid != self._fit_rid:
            return                                  # a newer fit superseded this one; drop it
        self._last_fit = r

        anchor_x, anchor_y = float(r.ts_hist[-1]), float(r.y_fit[-1])  # the last history point becomes the anchor
        self.curve_fit.setData(r.ts_hist, r.y_fit)

        if r.ts_future.size:
            # forecast segment: stitch "anchor + future extrapolation" into one continuous curve
            xs = np.concatenate([[anchor_x], r.ts_future])
            ys = np.concatenate([[anchor_y], r.y_pred])
            self.curve_pred.setData(xs, ys)
            self.curve_up.setData(xs, np.concatenate([[anchor_y], r.upper]))
            self.curve_low.setData(xs, np.concatenate([[anchor_y], r.lower]))
            self.dot_pred.setData([xs[-1]], [ys[-1]])   # end-point marker
        else:
            for item in (self.curve_pred, self.curve_up, self.curve_low):
                item.setData([], [])
            self.dot_pred.setData([], [])

        self._set_overlay_visible(True)
        self._update_fit_labels(r)
        if r.ts_future.size:
            self.plot.setXRange(float(r.ts_hist[0]), float(r.ts_future[-1]), padding=0.02)
        else:
            self.plot.autoRange(padding=0.03)

        # ---- console print of fit details ----
        space = "log space ln(y)" if r.log_space else "linear space"
        print(f"\n[Polynomial Regression Result] n={r.degree}   space={space}   samples={r.n_points}"
              f" (window={r.window})   horizon={r.ts_future.size} bars")
        print(f"  coefficients (highest → constant): "
              + ", ".join(f"{c:.6g}" for c in r.coef))
        print(f"  R²={r.r2:.6f}   adjusted R²={r.adj_r2:.6f}   RMSE={r.rmse:,.4f}   "
              f"MAE={r.mae:,.4f}   MAPE={r.mape:.4f}%   σ={r.sigma:,.4f}")
        if r.ts_future.size:
            step_days = (float(np.median(np.diff(r.ts_future))) / 86400.0
                         if r.ts_future.size > 1 else 0.0)
            print(f"  Next-period forecast {r.next_value:,.2f}  ({r.next_change_pct:+.2f}%)"
                  + (f"  step≈{step_days:.2f} days" if step_days else ""))
            print(f"  End-of-horizon forecast {float(r.y_pred[-1]):,.2f}"
                  f"   ({(float(r.y_pred[-1]) / float(r.y_fit[-1]) - 1) * 100:+.2f}% vs. current)")
            print(f"  Confidence band ±1.96σ: [{float(r.lower[-1]):,.2f}, {float(r.upper[-1]):,.2f}]")
        print("─" * 70, flush=True)

        self.lbl_status.setText(
            f"Fit done · n={r.degree} · {space} · {r.n_points} samples · R²={r.r2:.4f} · "
            f"next {r.next_value:,.2f} ({r.next_change_pct:+.2f}%)")

    def _on_fit_err(self, rid: int, msg: str):
        if rid != self._fit_rid:
            return
        log(f"Fit failed: {msg}")
        self.lbl_status.setText(f"⚠ Fit failed: {msg}")

    def clear_forecast(self, keep_status: bool = False):
        """Clear every fit / forecast curve from the chart; keep_status=True skips the status bar."""
        self._last_fit = None
        self._fit_rid += 1                                # invalidate any in-flight fit
        for item in (self.curve_fit, self.curve_pred, self.curve_up, self.curve_low):
            item.setData([], [])
        self.dot_pred.setData([], [])
        self._set_overlay_visible(False)
        for lbl in self.res_labels.values():
            lbl.setText("—")
        if not keep_status:
            self.lbl_status.setText("Forecast cleared")
            log("Forecast cleared")

    def _set_overlay_visible(self, on: bool):
        """Toggle all fit / forecast graphics items together."""
        items = [self.curve_fit, self.curve_pred, self.curve_up,
                 self.curve_low, self.dot_pred]
        if self.band is not None:
            items.append(self.band)
        for item in items:
            item.setVisible(on)

    def _update_fit_labels(self, r: FitResult):
        """Fill the fit metrics into the result panel."""
        self.res_labels["r2"].setText(f"{r.r2:.4f}")
        self.res_labels["adj"].setText(f"{r.adj_r2:.4f}")
        self.res_labels["rmse"].setText(f"{r.rmse:,.2f}")
        self.res_labels["mae"].setText(f"{r.mae:,.2f}")
        self.res_labels["mape"].setText(f"{r.mape:.2f}%")
        self.res_labels["sigma"].setText(f"{r.sigma:,.2f}")
        self.res_labels["samples"].setText(f"{r.n_points}")
        color = C_UP if r.next_change_pct >= 0 else C_DOWN   # color the forecast by sign
        self.res_labels["next"].setText(
            f'<span style="color:{color}">{r.next_value:,.2f} '
            f'({r.next_change_pct:+.2f}%)</span>')

    # ============================================================ NumPy console
    def _numpy_env(self) -> Dict[str, object]:
        """Build the sandbox env for expression evaluation: expose data as variables."""
        close = np.asarray([c.close for c in self.candles], dtype=float)
        env = {
            "np": np, "numpy": np,
            "close": close,
            "open": np.asarray([c.open for c in self.candles], dtype=float),
            "high": np.asarray([c.high for c in self.candles], dtype=float),
            "low": np.asarray([c.low for c in self.candles], dtype=float),
            "volume": np.asarray([c.volume for c in self.candles], dtype=float),
            "ts": np.asarray([c.ts for c in self.candles], dtype=float) / 1000.0,  # seconds
            "ret": np.diff(close) / np.maximum(close[:-1], 1e-9),   # simple returns
            "logret": np.diff(np.log(np.maximum(close, 1e-9))),      # log returns
            "idx": np.arange(close.size, dtype=float),
            "n": int(close.size),
            "tf": self.tf.key,                                       # current timeframe key
            "periods_per_year": float(self.tf.periods_per_year),     # bars per year
        }
        return env

    def _use_expr(self, expr: str):
        self.ed_expr.setText(expr)
        self.run_expression()

    def run_expression(self):
        """Evaluate the expression in the text box on a worker thread (restricted sandbox)."""
        expr = self.ed_expr.text().strip()
        if not expr:
            return
        if not self.candles:
            self.lbl_status.setText("Please refresh data first")
            return
        self._expr_rid += 1
        self._run_worker(ExprWorker(self._expr_rid, expr, self._numpy_env()),
                         self._on_expr_ok, self._on_expr_err)

    def _on_expr_ok(self, rid: int, expr: str, result: str):
        if rid != self._expr_rid:
            return
        log_rule("NumPy expression evaluation")
        print(f"  >>> {expr}\n  {result}", flush=True)
        self.txt_expr.appendPlainText(f">>> {expr}\n{result}\n")     # also append to the console widget
        self.lbl_status.setText("Expression evaluated (printed to console)")

    def _on_expr_err(self, rid: int, expr: str, msg: str):
        if rid != self._expr_rid:
            return
        log(f"Expression error {expr} → {msg}")
        self.txt_expr.appendPlainText(f">>> {expr}\n✗ {msg}\n")

    # ============================================================ view
    def _sync_view(self):
        """Switch between candles / close line based on the checkboxes, and toggle the crosshair."""
        candle_on = self.chk_candle.isChecked() and not self.chk_log_y.isChecked()
        self.item_candles.setVisible(candle_on)        # no candles in log mode
        self.curve_close.setVisible(not candle_on)     # the close line replaces them
        self.vline.setVisible(self.chk_cross.isChecked())
        self.hline.setVisible(self.chk_cross.isChecked())
        if not candle_on and self.candles:
            close = np.asarray([c.close for c in self.candles], dtype=float)
            self.curve_close.setData(self._xs, close,
                                     fillLevel=max(float(close.min()) * 0.97, 1e-9),
                                     brush=pg.mkBrush(247, 147, 26, 26))

    def _on_log_y_changed(self, on: bool):
        """Toggle the Y axis log mode: candles are replaced by the close line."""
        self.chk_candle.setEnabled(not on)
        if on:
            self.chk_candle.setChecked(False)
        self.plot.setLogMode(False, on)
        self._sync_view()
        if self.candles:
            self.plot.autoRange(padding=0.05)

    def _on_mouse_move(self, evt):
        """On hover, snap to the nearest candle and show its details."""
        if not self.candles or not self.chk_cross.isChecked():
            return
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        x = float(self.plot.getViewBox().mapSceneToView(pos).x())   # scene coords → data coords
        i = int(np.clip(np.searchsorted(self._xs, x), 0, len(self._xs) - 1))  # binary search for the index
        if i > 0 and abs(self._xs[i - 1] - x) < abs(self._xs[i] - x):
            i -= 1                                                  # compare neighbours, take the nearer one
        c = self.candles[i]
        dt = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        fmt = "%Y-%m-%d %H:%M" if self.tf.key in ("1h", "4h") else "%Y-%m-%d"
        chg = (c.close / c.open - 1) * 100 if c.open else 0.0       # this bar's change
        col = C_UP if chg >= 0 else C_DOWN
        self.vline.setPos(self._xs[i])
        self.hline.setPos(c.close)
        self.lbl_cursor.setText(
            f'<b>{dt.strftime(fmt)}</b>  O <b>{c.open:,.2f}</b>  H <b>{c.high:,.2f}</b>  '
            f'L <b>{c.low:,.2f}</b>  C <b>{c.close:,.2f}</b>  '
            f'<span style="color:{col}">{chg:+.2f}%</span>  Vol {c.volume:,.0f}')

    # ============================================================ closing
    def closeEvent(self, event):
        """Stop all background threads before quitting so leftover QThreads don't hang the process."""
        for t in list(self._threads):
            t.quit()
            t.wait(2000)
        self._threads.clear()
        self._workers.clear()
        super().closeEvent(event)


# ============================================================================
# 10. Entry point
# ============================================================================
def main() -> int:
    """Program entry: configure console encoding → create the app → show the main window."""
    # The Windows console defaults to GBK; printing "²"/"³"-style glyphs raises a
    # UnicodeEncodeError. We keep the encoding (so the terminal stays readable)
    # and only replace unencodable characters with "?" instead of crashing.
    # getattr keeps this compatible with pseudo-stdouts such as IDLE that have no reconfigure().
    getattr(sys.stdout, "reconfigure", lambda **_: None)(errors="replace")
    getattr(sys.stderr, "reconfigure", lambda **_: None)(errors="replace")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(FONT_STACK)
    font.setPointSize(10)
    app.setFont(font)

    print("=" * 74)
    print("  BTC Market Lab v2.2 · PyQt6 + pyqtgraph + NumPy")
    print(f"  pyqtgraph version: {getattr(pg, '__version__', 'unknown')}")
    print("  Network strategy order: HTTP 10808 → HTTP 10809 → SOCKS5 10808 → direct")
    print("  All stats and expression results are printed to this console")
    print("=" * 74, flush=True)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())