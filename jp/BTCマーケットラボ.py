#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC マーケットラボ v2.2 — PyQt6 + pyqtgraph + NumPy
=================================================================
v2.2 の修正点:
  * [重要] QThread ワーカーオブジェクトを Python 側で強参照していなかったため
    GC に回収され、thread.started → worker.run が実行されない不具合がありました
    （UI が「ネットワーク: 未接続」のまま、Binance のデータが読み込まれない）。
    現在は _run_worker がスレッド終了までワーカーへの強参照を保持します。
  * [重要] Windows コンソールの既定エンコーディングは GBK のため、print 時に
    "R²"/"σ" が UnicodeEncodeError を引き起こし、フィット処理が途中で中断されて
    いました。起動時に stdout / stderr を errors="replace" で再設定し、非対応の
    グリフはクラッシュせず "?" に置き換えられます。
  * 依存パッケージのインポートを整理: PyQt6 / pyqtgraph / NumPy / PySocks は
    直接インポートします（try/except によるエラー握りつぶしは廃止）。
  * バージョン互換ラッパー（_safe_downsampling / _safe_clip / _make_fill_between など）を
    廃止し、安定版 pyqtgraph API を直接呼び出します。
  * ネットワーク状態の表示をより正確に: 「接続中 / 接続済み / 全戦略失敗」を区別。

依存パッケージ:
    pip install PyQt6 pyqtgraph numpy pysocks

コマンドラインから実行してください（IDLE は非推奨）:
    python BTCマーケットラボ.py
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
# 0. コンソールログ出力
# ============================================================================
def log(*args) -> None:
    print(f"[{datetime.now():%H:%M:%S}]", *args, flush=True)


def log_rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * 4} {title} {'─' * max(4, 58 - len(title))}", flush=True)
    else:
        print("─" * 70, flush=True)


# ============================================================================
# 1. テーマカラー（上昇=緑 / 下落=赤）とプロット設定
# ============================================================================
C_BG      = "#0b0e13"       # ウィンドウ / チャート背景
C_PANEL   = "#141a24"       # メインパネル背景
C_PANEL_2 = "#111722"       # サイドサブパネル背景
C_INPUT   = "#0f151e"       # 入力欄 / カード背景
C_BORDER  = "#232e3e"       # ボーダーカラー
C_TEXT    = "#e8eef6"       # 主テキスト
C_MUTED   = "#93a1b1"       # 補助テキスト
C_ACCENT  = "#f7931a"      # ビットコインオレンジ
C_UP      = "#16c784"      # 上昇 — 緑
C_DOWN    = "#ea3943"      # 下落 — 赤
C_FIT     = "#4fc3f7"      # フィット曲線
C_PRED    = "#ffb347"      # 予測曲線
C_BAND    = (255, 179, 71, 30)  # 予測信頼帯（RGBA）

pg.setConfigOptions(antialias=True, background=C_BG, foreground=C_MUTED)  # 全体のアンチエイリアス + テーマ

FONT_STACK = ["Inter", "Segoe UI", "Yu Gothic UI", "Meiryo", "Noto Sans CJK JP"]  # UI フォント


# ============================================================================
# 2. ネットワーク層: プロキシ 127.0.0.1:10808 → 直接接続へ自動フォールバック
# ============================================================================
PROXY_HOST = "127.0.0.1"   # ローカルプロキシのアドレス（v2rayN / clash など）
PROXY_PORT = 10808         # ローカルプロキシのポート（HTTP/SOCKS 混在モードでも可）

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
}


class Http:
    """
    複数のネットワーク戦略を順に試行し、失敗時は自動で次へ切り替えます:
        HTTP プロキシ 10808 → HTTP プロキシ 10809 → SOCKS5 プロキシ 10808 → 直接接続
    一度成功した戦略は以降のリクエストで再利用されます。
    """

    STRATEGIES = (
        # （表示名, キー, プロキシ先, タイムアウト秒）
        (f"HTTP プロキシ {PROXY_HOST}:{PROXY_PORT}", "http8", f"http://{PROXY_HOST}:{PROXY_PORT}", 6.0),
        (f"HTTP プロキシ {PROXY_HOST}:10809", "http9", f"http://{PROXY_HOST}:10809", 6.0),
        (f"SOCKS5 プロキシ {PROXY_HOST}:{PROXY_PORT}", "socks8", f"socks5h://{PROXY_HOST}:{PROXY_PORT}", 6.0),
        ("直接接続（プロキシなし）", "direct", "", 20.0),
    )

    def __init__(self):
        self._openers: Dict[str, urllib.request.OpenerDirector] = {}  # キー -> 構築済みオープナーキャッシュ
        self._preferred: str | None = None                            # 現在優先する戦略のキー
        self.last_error: str = ""                                     # 前回の全失敗時の要約
        self._lock = threading.Lock()                                 # キャッシュ / 設定をスレッドセーフに

    @staticmethod
    def _make_opener(key: str, target: str):
        if key == "direct":
            # プロキシを明示的に空にして、本当に「直接接続」にする
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if key.startswith("http"):
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": target, "https": target}))
        # SOCKS5 — PySocks がインストール済みなので直接ハンドラを構築
        handler = sockshandler.SocksiPyHandler(
            socks.SOCKS5, PROXY_HOST, PROXY_PORT, rdns=True)
        return urllib.request.build_opener(handler)

    def _opener(self, key: str, target: str):
        with self._lock:
            if key not in self._openers:
                self._openers[key] = self._make_opener(key, target)   # 初回使用時に遅延構築
            return self._openers[key]

    def get_json(self, url: str, timeout: float | None = None):
        order: List[str] = []
        if self._preferred:
            order.append(self._preferred)                             # 以前成功した戦略を先頭にする
        order += [s[1] for s in self.STRATEGIES if s[1] != self._preferred]  # 残りは固定順でフォールバック

        errors: List[str] = []
        for key in order:
            name, _, target, default_to = next(
                (s for s in self.STRATEGIES if s[1] == key), (key, key, "", 15.0))
            try:
                opener = self._opener(key, target)                    # 現在の戦略を試す
                req = urllib.request.Request(url, headers=UA_HEADERS)
                with opener.open(req, timeout=timeout or default_to) as resp:
                    raw = resp.read().decode("utf-8", "replace")      # 寛容なデコード: 不正バイトは置換
                data = json.loads(raw)                                # JSON をパース
                if self._preferred != key:
                    with self._lock:
                        self._preferred = key                         # 成功した戦略を記憶
                    log(f"ネットワーク戦略 → {name}（記憶済み・以降は優先）")
                self.last_error = ""
                return data
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name} ✗ {exc}")                      # 失敗理由を記録
                log(f"ネットワーク戦略 {name} 失敗: {exc}")           # 次の戦略へ進む
        self.last_error = " | ".join(errors)
        raise RuntimeError("すべてのネットワーク戦略が失敗しました | " + self.last_error)

    def preferred_name(self) -> str:
        for s in self.STRATEGIES:
            if s[1] == self._preferred:
                return s[0]                                           # ステータスバー用の表示名
        return ""

    def reset(self) -> None:
        with self._lock:
            self._preferred = None                                    # 記憶した戦略を忘れて再探索
        self.last_error = ""
        log("ネットワーク戦略をリセットしました")


HTTP = Http()


# ============================================================================
# 3. データモデルと時間足
# ============================================================================
@dataclass(frozen=True)
class Candle:
    """1 本のローソク足（K ライン）。フィールド順は取引所が返す配列の順序と同じ。"""
    ts: float            # ミリ秒タイムスタンプ（UTC）
    open: float          # 始値
    high: float          # 高値
    low: float           # 安値
    close: float         # 終値
    volume: float = 0.0  # 出来高（Binance では売買基準通貨額）


@dataclass(frozen=True)
class Timeframe:
    """時間足設定: 取引所ごとのインターバル設定 + ローカル換算設定。"""
    key: str                # 内部 ID（例: "1d"）
    label: str              # UI 表示名（例: "日足"）
    binance: str            # Binance インターバル（例: "1d"）
    okx: str                # OKX インターバル（例: "1D"）
    cg_days: str            # CoinGecko market_chart の days パラメータ
    target: int             # 読み込む目標ローソク足数（不足分はページング取得）
    periods_per_year: float # 年率換算係数: 1 年あたりの足数
    resample: str = ""      # 設定時、元データをこの期間へリサンプリング（例: 年足 "Y"）


TIMEFRAMES: List[Timeframe] = [
    Timeframe("1h", "1時間",  "1h", "1H", "90", 1000, 24 * 365),
    Timeframe("4h", "4時間",  "4h", "4H", "90", 1000, 6 * 365),
    Timeframe("1d", "日足",   "1d", "1D", "max", 2000, 365),
    Timeframe("1w", "週足",   "1w", "1W", "max", 1000, 52),
    Timeframe("1M", "月足",   "1M", "1M", "max", 500, 12),
    Timeframe("1Y", "年足",   "1M", "1M", "max", 500, 1, resample="Y"),  # 月足データを年足へリサンプリング
]
TF_BY_KEY = {tf.key: tf for tf in TIMEFRAMES}  # 時間足をキーですぐ引けるように


# ============================================================================
# 4. マーケットデータ取得
# ============================================================================
BINANCE_HOSTS = (   # Binance 公式エンドポイント（順に試行）
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api1.binance.com",
)


def _dedup(candles: Sequence[Candle]) -> List[Candle]:
    bucket = {int(c.ts): c for c in candles}   # ミリ秒タイムスタンプで重複排除（後勝ち）
    return [bucket[k] for k in sorted(bucket)] # 時刻昇順で返す


def _binance_paged(host: str, symbol: str, interval: str, target: int) -> List[Candle]:
    """Binance klines を endTime で過去方向にページングし、`target` 本に到達するまで取得。"""
    out: List[Candle] = []
    end_ms = None
    while len(out) < target:
        limit = int(min(1000, max(50, target - len(out))))  # 1 ページ最大 1000 本
        query = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_ms is not None:
            query["endTime"] = int(end_ms)                  # 前ページ最古の足より前のみに限定
        rows = HTTP.get_json(f"{host}/api/v3/klines?" + urllib.parse.urlencode(query))
        if not isinstance(rows, list) or not rows:
            break                                            # もうデータがない
        out = [Candle(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5])) for r in rows] + out      # 新しいページを前方に接続
        end_ms = int(rows[0][0]) - 1                         # 最古の足より前へ進める
        if len(rows) < limit:
            break                                            # 短いページ = 起点に到達
        time.sleep(0.1)                                      # 軽いレート制限で BAN 回避
    return _dedup(out)


def fetch_binance(symbol: str, tf: Timeframe) -> List[Candle]:
    symbol = symbol.upper().replace("-", "").replace("/", "")  # BTCUSDT 形式に正規化
    err = None
    for host in BINANCE_HOSTS:                                  # エンドポイント間でリトライ
        try:
            data = _binance_paged(host, symbol, tf.binance, tf.target)
            if data:
                log(f"Binance {host}: {len(data)} 本のローソク足を取得しました")
                return data
        except Exception as exc:  # noqa: BLE001
            err = exc
            log(f"Binance {host} 失敗: {exc}")
    raise RuntimeError(f"Binance のデータ取得に失敗: {err}")


def _to_okx_inst(symbol: str) -> str:
    """BTCUSDT / BTC-USDT を OKX が期待する BTC-USDT 形式に正規化する。"""
    s = symbol.upper().replace("/", "-")
    if "-" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):   # 取引通貨を見つけてダッシュを挿入
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
            query["after"] = after                            # カーソル: 過去へページング
        js = HTTP.get_json("https://www.okx.com/api/v5/market/history-candles?"
                           + urllib.parse.urlencode(query))
        rows = js.get("data") or []
        if not rows:
            break
        out = [Candle(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5])) for r in rows] + out
        after = rows[-1][0]                                   # 最古の足から続行
        if len(rows) < 100:
            break
        time.sleep(0.12)
    data = _dedup(out)
    if not data:
        raise RuntimeError("OKX が空のデータを返しました")
    log(f"OKX {inst}: {len(data)} 本のローソク足を取得しました")
    return data


def fetch_coingecko(tf: Timeframe) -> List[Candle]:
    js = HTTP.get_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                       f"?vs_currency=usd&days={tf.cg_days}", timeout=25.0)
    prices = js.get("prices") or []   # [[ミリ秒タイムスタンプ, 価格], ...]
    vols = {int(t): float(v) for t, v in (js.get("total_volumes") or [])}  # タイムスタンプ -> 出来高
    if not prices:
        raise RuntimeError("CoinGecko が空のデータを返しました")
    candles = [Candle(float(t), float(p), float(p), float(p), float(p), vols.get(int(t), 0.0))
               for t, p in prices]   # CoinGecko には価格ポイントのみ。OHLC は同一価格を使用
    log(f"CoinGecko: {len(candles)} 個の価格ポイントを取得しました")
    return candles[-tf.target:] if len(candles) > tf.target else candles  # 最後の区間だけ残す


def resample(candles: Sequence[Candle], rule: str) -> List[Candle]:
    """細かいローソク足をより粗い期間（年 / 月 / 週 / 日）へ集約する。"""
    if not candles:
        return []
    groups: Dict[tuple, List[Tuple[int, Candle]]] = {}
    order: List[tuple] = []
    for c in candles:
        dt = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        if rule == "Y":
            key = (dt.year,)                                    # バケットキー: 年
            start = datetime(dt.year, 1, 1, tzinfo=timezone.utc)
        elif rule == "M":
            key = (dt.year, dt.month)                           # 月
            start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
        elif rule == "W":
            monday = dt - timedelta(days=dt.weekday())          # 週の始まりを月曜日とする
            key = (monday.year, monday.month, monday.day)
            start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        else:
            key = (dt.year, dt.month, dt.day)                   # 自然日
            start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        if key not in groups:
            groups[key] = []
            order.append(key)                                   # 最初に出現した順を記憶
        groups[key].append((int(start.timestamp() * 1000), c))

    result: List[Candle] = []
    for key in order:
        items = groups[key]
        cs = [c for _, c in items]
        result.append(Candle(
            ts=float(items[0][0]),
            open=cs[0].open,             # 期間の始値 = 最初の足の始値
            high=max(c.high for c in cs),   # 期間の高値 = 区間内の最大値
            low=min(c.low for c in cs),     # 期間の安値 = 区間内の最小値
            close=cs[-1].close,          # 期間の終値 = 最後の足の終値
            volume=float(sum(c.volume for c in cs)),  # 出来高は合計
        ))
    result.sort(key=lambda c: c.ts)
    return result


def load_market(source: str, symbol: str, tf: Timeframe) -> List[Candle]:
    """統合エントリ: ソースから取得 → （必要なら）リサンプリング → 重複排除して返す。"""
    if source == "Binance":
        candles = fetch_binance(symbol, tf)
    elif source == "OKX":
        candles = fetch_okx(symbol, tf)
    else:
        candles = fetch_coingecko(tf)

    if tf.resample:                      # より粗い期間（年足など）が指定されたときはリサンプリング
        before = len(candles)
        candles = resample(candles, tf.resample)
        log(f"{before} → {len(candles)} 本にリサンプリング（{tf.label}）")

    candles = _dedup(candles)
    if len(candles) < 10:                # 足が少なすぎると統計 / フィットが無意味
        raise RuntimeError(f"有効なローソク足が少なすぎます（{len(candles)} 本）。データソースまたは時間足を変更してください。")
    return candles


# ============================================================================
# 5. NumPy 統計レポート
# ============================================================================
def _skew(a: np.ndarray) -> float:
    if a.size < 2:
        return 0.0
    m, s = float(a.mean()), float(a.std(ddof=0))
    return 0.0 if s == 0 else float(np.mean(((a - m) / s) ** 3))


def _kurt(a: np.ndarray) -> float:
    """超過尖度（正規分布 = 0）。"""
    if a.size < 2:
        return 0.0
    m, s = float(a.mean()), float(a.std(ddof=0))
    return 0.0 if s == 0 else float(np.mean(((a - m) / s) ** 4) - 3.0)  # E[z^4]-3


def _max_streak(flags: np.ndarray) -> int:
    """True が連続する最長の長さ（上昇 / 下落の連続本数）。"""
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0     # 連続中は +1、途切れたらリセット
        if cur > best:
            best = cur
    return int(best)


def _ema_series(a: np.ndarray, span: int) -> np.ndarray:
    """再帰的 EMA 系列: alpha=2/(span+1)。"""
    if a.size == 0:
        return np.asarray([])
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(a, dtype=float)
    out[0] = a[0]                     # 最初の値で初期化
    for i in range(1, a.size):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(a: np.ndarray, period: int = 14) -> float:
    """Wilder 式 RSI。平滑化された平均利益 / 平均損失から計算。"""
    if a.size < period + 1:
        return float("nan")
    d = np.diff(a)
    gain = np.where(d > 0, d, 0.0)    # 上昇幅
    loss = np.where(d < 0, -d, 0.0)   # 下落幅
    ag = float(gain[:period].mean())  # 初期の平均利益
    al = float(loss[:period].mean())  # 初期の平均損失
    for i in range(period, d.size):   # Wilder 平滑化
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
    if al == 0:
        return 100.0                  # 損失ゼロ => RSI は最大値
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _pct(v: float) -> str:
    return f"{v * 100:+,.2f}%"   # 割合を符号付き・小数 2 桁のパーセント文字列に


def _money(v: float) -> str:
    return f"${v:,.2f}"          # 3 桁区切りの通貨形式


@dataclass
class Report:
    text: str = ""
    cards: Dict[str, Tuple[str, str]] = field(default_factory=dict)


def build_report(candles: Sequence[Candle], tf: Timeframe,
                 source: str, symbol: str) -> Report:
    """NumPy で計算できる限りを計算: コンソールへ出力し、UI カード用にもパースする。"""
    close = np.asarray([c.close for c in candles], dtype=float)   # 終値系列
    hi = np.asarray([c.high for c in candles], dtype=float)       # 高値系列
    lo = np.asarray([c.low for c in candles], dtype=float)        # 安値系列
    vol = np.asarray([c.volume for c in candles], dtype=float)    # 出来高系列
    ts = np.asarray([c.ts for c in candles], dtype=float) / 1000.0  # 秒単位のタイムスタンプ
    n = close.size                                                 # サンプル数

    sr = np.diff(close) / np.maximum(close[:-1], 1e-9)          # 単利リターン
    lr = np.diff(np.log(np.maximum(close, 1e-9)))               # 対数リターン
    ann = float(tf.periods_per_year)                              # 年率換算用の年間足数

    lines: List[str] = []
    cards: Dict[str, Tuple[str, str]] = {}

    def add(text: str) -> None:
        lines.append(text)

    start = datetime.fromtimestamp(ts[0], tz=timezone.utc)
    end = datetime.fromtimestamp(ts[-1], tz=timezone.utc)

    # ---------------- header ----------------
    add("=" * 74)
    add(f"  BTC 統計レポート   {source} · {symbol} · {tf.label}")
    add(f"  サンプル {n} 本   期間 {start:%Y-%m-%d} → {end:%Y-%m-%d}   "
        f"（{(end - start).days} 日間）")
    add("=" * 74)

    # ---------------- price ----------------
    p_mean, p_med = float(np.mean(close)), float(np.median(close))
    p_std = float(np.std(close, ddof=1)) if n > 1 else 0.0
    p_var = float(np.var(close, ddof=1)) if n > 1 else 0.0
    p_min, p_max = float(np.min(close)), float(np.max(close))
    p_ptp = float(np.ptp(close))
    q = np.percentile(close, [5, 25, 50, 75, 95])
    iqr = float(np.percentile(close, 75) - np.percentile(close, 25))

    log_rule("NumPy による価格統計")
    add("【価格・NumPy】close = np.array([...])")
    add(f"  np.mean            平均価格　　　　　　{_money(p_mean)}")
    add(f"  np.median          中央価格　　　　　　{_money(p_med)}")
    add(f"  np.std(ddof=1)     標準偏差　　　　　　${p_std:,.2f}   (CV {p_std / p_mean * 100:.2f}%)")
    add(f"  np.var(ddof=1)     分散　　　　　　　　{p_var:,.2f}")
    add(f"  np.min/np.max      安値 / 高値　　　　{_money(p_min)} / {_money(p_max)}")
    add(f"  np.ptp             レンジ　　　　　　{_money(p_ptp)}   (振幅 {p_ptp / p_min * 100:.2f}%)")
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

    peak = np.maximum.accumulate(close)          # 履歴上の最高値の推移
    dd = close / np.maximum(peak, 1e-9) - 1.0    # 最高値からのドローダウン（<=0）
    mdd = float(np.min(dd)) if n else 0.0        # 最大ドローダウン
    mdd_i = int(np.argmin(dd)) if n else 0       # 最も深いドローダウンの位置
    calmar = (ann_ret / abs(mdd)) if mdd < 0 else float("inf")   # Calmar = 年率 / |MDD|

    def _d(idx: int) -> str:
        """sr[i] は close[i+1] の期間に対応する。"""
        return datetime.fromtimestamp(ts[min(idx + 1, n - 1)],
                                      tz=timezone.utc).strftime("%Y-%m-%d")

    log_rule("NumPy によるリターン・変動統計")
    add("【リターン・NumPy】sr = np.diff(close) / close[:-1]")
    add(f"  合計リターン         (close[-1]/close[0]-1)   {_pct(total)}")
    add(f"  np.mean(sr)          1期間あたり平均リターン {_pct(mean_sr)}")
    add(f"  np.median(sr)        期間リターンの中央値     {_pct(med_sr)}")
    add(f"  np.std(sr, ddof=1)   期間ボラティリティ       {_pct(std_sr)}")
    add(f"  np.max(sr)           最良期間                 {_pct(best)}   @ {_d(best_i)}")
    add(f"  np.min(sr)           最悪期間                 {_pct(worst)}   @ {_d(worst_i)}")
    add(f"  np.sum(sr>0)/np.sum(sr<0)   上昇 {up} / 下落 {down} / 横ばい {flat}"
        f"   勝率 {up_ratio * 100:.2f}%")
    add(f"  最長の連続上昇 / 下落      {_max_streak(sr > 0)} / {_max_streak(sr < 0)} 本")
    add(f"  年率リターン（幾何平均）  {_pct(ann_ret)}   (x {ann:g} 期間)")
    add(f"  年率ボラティリティ        {ann_vol * 100:,.2f}%")
    add(f"  シャープレシオ（無リスク=0） {sharpe:,.3f}")
    add(f"  最大ドローダウン          {_pct(mdd)}   @ {_d(mdd_i)}"
        f"   (ピーク {_money(float(peak[mdd_i]))} から)")
    add(f"  カルマーレシオ            {calmar:,.3f}" if math.isfinite(calmar) else "  カルマーレシオ            ∞")
    add(f"  累積純資産 np.prod(1+sr)  {float(np.prod(1 + sr)):,.4f}")
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
    log_rule("NumPy による分布形状")
    add("【分布・NumPy】")
    add(f"  log リターンの平均 / 標準偏差  {float(np.mean(lr)) if lr.size else 0:+.6f} / "
        f"{float(np.std(lr, ddof=1)) if lr.size > 1 else 0:.6f}")
    add(f"  歪度    Skew         {sk:+.4f}   (>0 右に裾が長い)")
    add(f"  尖度    超過尖度     {ku:+.4f}   (>0 裾が厚い)")
    if n > 1:
        add(f"  np.corrcoef(close, np.arange(n))   価格 vs 時間   "
            f"{float(np.corrcoef(close, np.arange(n))[0, 1]):+.4f}")
    if vol.size > 1 and float(np.std(vol)) > 0:
        add(f"  np.corrcoef(close, volume)         価格 vs 出来高 "
            f"{float(np.corrcoef(close, vol)[0, 1]):+.4f}")
    if lr.size > 2:
        add(f"  np.corrcoef(lr[:-1], lr[1:])      リターン自己相関   "
            f"{float(np.corrcoef(lr[:-1], lr[1:])[0, 1]):+.4f}")
    cards["skew"] = (f"{sk:+.3f}", "")
    cards["kurt"] = (f"{ku:+.3f}", "")

    # ---------------- trend / indicators ----------------
    idx = np.arange(n, dtype=float)             # サンプルインデックス（回帰 / 相関用）
    slope, _intercept = np.polyfit(idx, np.log(np.maximum(close, 1e-9)), 1)  # log 価格の線形トレンド
    trend_ann = math.exp(float(slope) * ann) - 1      # 傾きを年率換算
    grad = float(np.gradient(close)[-1]) if n > 2 else 0.0  # 直近の限界変化

    def _sma(w: int) -> float:
        return float(np.mean(close[-w:])) if n >= w else float("nan")

    ema12, ema26 = _ema_series(close, 12), _ema_series(close, 26)  # 短期 / 長期 EMA
    macd_series = ema12 - ema26                       # MACD（DIF）
    signal = _ema_series(macd_series, 9)              # DEA（シグナル線）
    rsi14 = _rsi(close, 14)
    boll_w = 20                                       # BOLL 窓
    if n >= boll_w:
        b_mid = float(np.mean(close[-boll_w:]))       # 中央バンド = SMA20
        b_sd = float(np.std(close[-boll_w:], ddof=0)) # 標準偏差（母分散 σ）
    else:
        b_mid = b_sd = float("nan")

    log_rule("NumPy によるトレンド・指標")
    add("【トレンド・NumPy】")
    add(f"  np.polyfit(idx, log(close), 1)   トレンド傾き / 年率換算  {float(slope):+.6f} / {_pct(trend_ann)}")
    add(f"  np.gradient(close)[-1]           直近の限界変化          {grad:+,.2f}")
    add(f"  SMA20 / SMA50 / SMA200           "
        f"{_money(_sma(20))} / {_money(_sma(50))} / {_money(_sma(200))}")
    add(f"  EMA12 / EMA26                    {_money(float(ema12[-1]))} / {_money(float(ema26[-1]))}")
    add(f"  MACD / Signal                    {float(macd_series[-1]):+,.2f} / {float(signal[-1]):+,.2f}")
    add(f"  RSI(14)                          {rsi14:.2f}")
    add(f"  BOLL20 中央 / ±2σ               {_money(b_mid)}  ±{b_sd * 2:,.2f}")
    add(f"  平均レンジ np.mean(high-low)     {float(np.mean(hi - lo)):,.2f}"
        f"   (直近終値比 {float(np.mean(hi - lo)) / float(close[-1]) * 100:.2f}%)")
    add("【出来高・NumPy】")
    add(f"  np.sum / np.mean / np.median     {float(np.sum(vol)):,.0f} / "
        f"{float(np.mean(vol)):,.2f} / {float(np.median(vol)):,.2f}")
    cards["rsi"] = (f"{rsi14:.1f}", C_UP if rsi14 >= 50 else C_DOWN)

    add("")
    add(f"  直近価格 {_money(float(close[-1]))}    最終足の変動 "
        f"{_pct(float(close[-1] / close[-2] - 1)) if n > 1 else '—'}")
    add("=" * 74)

    text = "\n".join(lines)
    print(text, flush=True)
    return Report(text=text, cards=cards)# ============================================================================
# 6. 多項式回帰による予測
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
    最小二乗フィット y = a_n x^n + … + a_0
      * x を先に [-1, 1] へ正規化し、高次フィットで悪条件になる行列を回避
        （これにより高次の次数も実用的に使える）
      * log_space=True の場合、代わりに ln(y) をフィットし、BTC の長期的な
        指数成長に合わせる
    """
    if len(candles) < 4:
        raise ValueError("フィットに十分なデータポイントがありません")
    if window and window > 0:
        if window < degree + 3:
            raise ValueError(f"フィット窓は次数+3 未満にできません（現在 {window}）")
        candles = candles[-window:]

    n = len(candles)
    if n < degree + 3:
        raise ValueError(f"{n} 本の有効な足では次数 {degree} のフィットに不足です（最低 {degree + 3} 本必要）")

    y = np.asarray([c.close for c in candles], dtype=float)   # 従属変数: 終値
    ts = np.asarray([c.ts for c in candles], dtype=float) / 1000.0  # 秒単位のタイムスタンプ
    if np.any(y <= 0):        # 非正の価格は対数変換できないので線形空間にフォールバック
        log_space = False

    idx = np.arange(n, dtype=float)
    centre = float(idx.mean())                             # 正規化の中心
    half = float((idx[-1] - idx[0]) / 2.0) or 1.0          # 正規化の半径
    X = (idx - centre) / half                              # x ∈ [-1,1]: 悪条件行列を回避
    horizon = max(int(horizon), 0)
    Xf = (np.arange(n, n + horizon, dtype=float) - centre) / half if horizon else np.asarray([])

    target = np.log(y) if log_space else y                 # 対数空間: 先に ln を取る
    coef = np.polyfit(X, target, degree)                   # 最小二乗多項式の係数
    fit = np.polyval(coef, X)                              # 履歴全体のフィット値
    pred = np.polyval(coef, Xf) if Xf.size else np.asarray([])  # 未来への外挿区間
    if log_space:
        fit = np.exp(fit)                                  # 価格スケールへ戻す
        pred = np.exp(pred) if pred.size else pred

    resid = y - fit                                        # 残差 = 実測 - フィット
    ss_res = float(np.sum(resid ** 2))                     # 残差平方和
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1e-12   # 総平方和（ゼロ除算を回避）
    r2 = 1.0 - ss_res / ss_tot                             # 決定係数
    k = degree
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / max(n - k - 1, 1)  # 自由度調整済み R²
    rmse = float(np.sqrt(ss_res / n))                      # 平均平方根誤差
    mae = float(np.mean(np.abs(resid)))                    # 平均絶対誤差
    mape = float(np.mean(np.abs(resid / np.maximum(y, 1e-9))) * 100.0)  # 平均絶対パーセント誤差 %
    dof = max(n - k - 1, 1)                                # 自由度
    sigma = float(np.sqrt(ss_res / dof))                   # 残差標準誤差

    step = float(np.median(np.diff(ts))) if n > 1 else 86400.0   # 平均足長（秒）
    ts_future = ts[-1] + step * np.arange(1, horizon + 1) if horizon else np.asarray([])

    if pred.size:
        upper = pred + band_k * sigma                      # 上方信頼帯
        lower = np.maximum(pred - band_k * sigma, 1e-9)    # 下方信頼帯（非負）
        next_value = float(pred[0])                        # 次期予測値
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
# 7. ワーカースレッド
# ============================================================================
class DataWorker(QObject):
    """バックグラウンドでマーケットデータを取得し、ok / err シグナルで結果を返す。"""
    ok = pyqtSignal(int, object)   # (rid, candles)
    err = pyqtSignal(int, str)     # (rid, エラーメッセージ)

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
                msg += "（ソースを OKX / CoinGecko に切り替えることもできます）"
            self.err.emit(self.rid, msg)


class FitWorker(QObject):
    """バックグラウンドで多項式回帰予測を実行する。"""
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


SAFE_BUILTINS = {   # 式コンソールでは安全な組み込み関数のみ公開 — import / open などは不可
    "abs": abs, "len": len, "min": min, "max": max, "sum": sum, "round": round,
    "range": range, "float": float, "int": int, "list": list, "tuple": tuple,
    "dict": dict, "sorted": sorted, "enumerate": enumerate, "zip": zip,
    "print": print, "any": any, "all": all, "bool": bool, "str": str,
}


class ExprWorker(QObject):
    """UI をフリーズさせないよう、ワーカースレッドで NumPy 式を評価する。"""
    ok = pyqtSignal(int, str, str)   # (rid, expression, 整形済みの結果)
    err = pyqtSignal(int, str, str)  # (rid, expression, エラーメッセージ)

    def __init__(self, rid: int, expr: str, env: Dict[str, object]):
        super().__init__()
        self.rid, self.expr, self.env = rid, expr, env

    @pyqtSlot()
    def run(self):
        try:
            with np.errstate(all="ignore"):
                # 厳格制限付き環境: SAFE_BUILTINS + 事前注入された NumPy データ環境
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
# 8. QPicture によるプリレンダー描画アイテム
# ============================================================================
class CandlestickItem(pg.GraphicsObject):
    """カスタムローソク足チャート: QPicture にプリレンダーして高速にズーム / パン。"""

    def __init__(self):
        super().__init__()
        self._up, self._down = QColor(C_UP), QColor(C_DOWN)   # 上昇=緑 / 下落=赤
        self._bars: List[Tuple[float, float, float, float, float]] = []
        self._pic, self._rect = QPicture(), QRectF()          # プリレンダーキャッシュ + バウンディング矩形

    def setData(self, bars: Sequence[Tuple[float, float, float, float, float]]):
        """データ更新: 各バーは (x 秒, 始値, 終値, 安値, 高値)。"""
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
        span = bars[-1][0] - bars[0][0]                    # 合計時間スパン
        step = span / max(len(bars) - 1, 1)                # 平均足間隔
        w = step * 0.62                                    # 足幅 ≈ 間隔の 62%
        for x, o, c, low, high in bars:
            col = self._up if c >= o else self._down       # 終値>=始値 は陽線（緑）
            pen = QPen(col)
            pen.setWidth(1)
            pen.setCosmetic(True)                          # ズームに依存しない線幅
            p.setPen(pen)
            p.drawLine(QPointF(x, low), QPointF(x, high))  # 上ヒゲ・下ヒゲ
            p.setBrush(QBrush(col))
            top, bot = max(o, c), min(o, c)
            if top - bot <= 1e-12:                         # ドージ: 始値==終値 は水平線
                p.drawLine(QPointF(x - w / 2, top), QPointF(x + w / 2, top))
            else:
                p.drawRect(QRectF(x - w / 2, bot, w, top - bot))   # 実体
        p.end()
        y_lo = min(b[3] for b in bars)                     # 全体の最安ヒゲ
        y_hi = max(b[4] for b in bars)                     # 全体の最高ヒゲ
        self._rect = QRectF(bars[0][0] - w, y_lo, span + 2 * w, (y_hi - y_lo) or 1e-9)

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self._pic)

    def boundingRect(self) -> QRectF:
        return self._rect


class VolumeItem(pg.GraphicsObject):
    """出来高バー。上昇 / 下落で色分け（上昇=緑、下落=赤）。"""
    def __init__(self):
        super().__init__()
        self._bars: List[Tuple[float, float, bool]] = []   # (x 秒, 出来高, 上昇かどうか)
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
        span = bars[-1][0] - bars[0][0]                    # 時間スパン
        step = span / max(len(bars) - 1, 1)                # 足間隔
        w = step * 0.62                                    # バー幅
        vmax = max(b[1] for b in bars) or 1.0              # 最大出来高（バウンディング矩形の基準）
        for x, v, is_up in bars:
            col = QColor(C_UP if is_up else C_DOWN)
            col.setAlpha(165)                              # 半透明
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))
            p.drawRect(QRectF(x - w / 2, 0.0, w, v))       # ゼロから上向きに描画
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
# 9. メインウィンドウ
# ============================================================================
STAT_CARDS = [
    ("median", "中央値 np.median"), ("mean", "平均値 np.mean"),
    ("pstd", "価格σ np.std"), ("range", "安値 / 高値"),
    ("total", "合計リターン"), ("medret", "リターン中央値"),
    ("annret", "年率リターン(幾何)"), ("annvol", "年率ボラティリティ"),
    ("mdd", "最大ドローダウン"), ("sharpe", "シャープレシオ"),
    ("best", "最良期間"), ("worst", "最悪期間"),
    ("upratio", "勝率"), ("rsi", "RSI(14)"),
    ("skew", "歪度"), ("kurt", "尖度"),
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
    """メインウィンドウ: 左にチャート、右に 4 つのタブ。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BTC マーケットラボ v2.2 · 統計 / NumPy / 多項式回帰")
        self.resize(1520, 940)
        self.setMinimumSize(1180, 780)

        self.candles: List[Candle] = []          # 現在のローソク足データ
        self.tf: Timeframe = TF_BY_KEY["1d"]     # 現在の時間足
        self._xs = np.asarray([])                # 秒単位タイムスタンプ（十字カーソル用）
        self._data_rid = 0                       # 自動インクリメント ID。古い結果は破棄
        self._fit_rid = 0                        # フィット要求の自動インクリメント ID
        self._expr_rid = 0                       # 式要求の自動インクリメント ID
        self._threads: List[QThread] = []        # 生存中のバックグラウンドスレッド
        self._workers: List[QObject] = []        # ワーカーへの強参照（GC 防止）
        self._last_fit: FitResult | None = None  # 最新のフィット結果
        self._loading = False                    # データ取得中か
        self._report = Report()                  # 最新の統計レポート

        self._build_ui()
        self._apply_style()

        # パラメータ変更後のデバウンス: 320 ms 以内の最後のトリガーのみ実行
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(320)
        self._auto_timer.timeout.connect(self.start_fit)

        QTimer.singleShot(80, self.refresh_data)  # 起動後に自動でデータ取得

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
        self.lbl_status = QLabel("準備完了")
        self.lbl_status.setObjectName("Status")
        self.lbl_net = QLabel("ネットワーク: 未接続")
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
            f'<span style="color:{C_ACCENT}">━</span> 価格  '
            f'<span style="color:{C_FIT}">━</span> 多項式フィット  '
            f'<span style="color:{C_PRED}">┄</span> 予測  '
            f'<span style="color:{C_UP}">●</span>上昇<span style="color:{C_DOWN}">●</span>下落')
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
        self.chk_candle = QCheckBox("ローソク足")
        self.chk_candle.setChecked(True)
        self.chk_log_y = QCheckBox("対数Y")
        self.chk_grid = QCheckBox("グリッド")
        self.chk_grid.setChecked(True)
        self.chk_cross = QCheckBox("十字カーソル")
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

        self.lbl_cursor = QLabel("チャートにホバーするとローソク足の詳細を確認できます")
        self.lbl_cursor.setObjectName("Cursor")
        lay.addWidget(self.lbl_cursor)

        # ---- main chart ----
        axis_bottom = pg.DateAxisItem(orientation="bottom")   # 下部の時間軸
        self.plot = pg.PlotWidget(axisItems={"bottom": axis_bottom})
        self.plot.setBackground(C_BG)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getAxis("left").setWidth(76)
        for name in ("left", "bottom"):
            ax = self.plot.getAxis(name)
            ax.setPen(pg.mkPen(C_BORDER))        # 軸線の色
            ax.setTextPen(pg.mkPen(C_MUTED))     # 目盛ラベルの色
        self.plot.getAxis("bottom").setStyle(showValues=False)  # 日付は出来高サブプロット側で表示
        self.plot.getViewBox().setDefaultPadding(0.03)

        self.item_candles = CandlestickItem()               # ローソク足アイテム
        self.plot.addItem(self.item_candles)

        self.curve_close = pg.PlotDataItem(pen=pg.mkPen(C_ACCENT, width=2.2))  # 終値ライン（対数モードでローソク足の代わり）
        self.curve_close.setDownsampling(auto=True, method="peak")  # 大規模データのダウンサンプリング
        self.curve_close.setClipToView(True)               # 可視領域のみ描画
        self.plot.addItem(self.curve_close)

        self.curve_fit = pg.PlotDataItem(pen=pg.mkPen(C_FIT, width=2))   # フィット履歴曲線
        self.curve_fit.setClipToView(True)
        self.plot.addItem(self.curve_fit)

        self.curve_up = pg.PlotDataItem(pen=pg.mkPen(None))   # 上方バンド境界（塗りのみ）
        self.curve_low = pg.PlotDataItem(pen=pg.mkPen(None))  # 下方バンド境界
        self.plot.addItem(self.curve_up)
        self.plot.addItem(self.curve_low)
        self.band = pg.FillBetweenItem(self.curve_up, self.curve_low,   # 2 つの境界の間を塗りつぶし
                                       brush=pg.mkBrush(*C_BAND))
        self.plot.addItem(self.band)

        self.curve_pred = pg.PlotDataItem(                     # 外挿予測（破線）
            pen=pg.mkPen(C_PRED, width=2.2, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.curve_pred)

        self.dot_pred = pg.ScatterPlotItem(size=12, brush=pg.mkBrush(C_PRED),  # 予測終端マーカー
                                           pen=pg.mkPen(C_BG, width=2), symbol="o")
        self.plot.addItem(self.dot_pred)

        self.vline = pg.InfiniteLine(       # 十字カーソル: 垂直参照線
            angle=90, movable=False,
            pen=pg.mkPen("#5d6b7d", width=1, style=Qt.PenStyle.DashLine))
        self.hline = pg.InfiniteLine(       # 十字カーソル: 水平参照線
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
        self.vol_plot.getAxis("left").setLabel("出来高")
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
        tabs.addTab(self._wrap(self._tab_market()), "市場")
        tabs.addTab(self._wrap(self._tab_fit()), "予測")
        tabs.addTab(self._wrap(self._tab_stats()), "統計")
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

    # ---- タブ1 市場 ----
    def _tab_market(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("データソース")
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.cb_source = QComboBox()
        self.cb_source.addItems(["Binance", "OKX", "CoinGecko"])
        self.cb_source.setToolTip("既定ではプロキシ 127.0.0.1:10808 を使用。失敗時は直接接続にフォールバック")
        self.cb_tf = QComboBox()
        self.cb_tf.addItems([tf.label for tf in TIMEFRAMES])
        self.cb_tf.setCurrentIndex(2)
        row1.addWidget(self._field("ソース", self.cb_source), 1)
        row1.addWidget(self._field("時間足", self.cb_tf), 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.ed_symbol = QLineEdit("BTCUSDT")
        self.btn_refresh = QPushButton("データ取得")
        self.btn_refresh.setObjectName("Primary")
        self.btn_refresh.clicked.connect(self.refresh_data)
        row2.addWidget(self._field("ペア", self.ed_symbol), 1)
        row2.addWidget(self.btn_refresh)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(10)
        self.btn_reconnect = QPushButton("ネットワーク再テスト")
        self.btn_reconnect.setObjectName("Ghost")
        self.btn_reconnect.setToolTip("記憶したプロキシ戦略を破棄し、127.0.0.1:10808 から再探索します")
        self.btn_reconnect.clicked.connect(self._reset_network)
        row3.addWidget(self.btn_reconnect)
        row3.addStretch(1)
        lay.addLayout(row3)
        v.addWidget(frame)

        frame2, lay2 = self._panel("主要指標")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.cards: Dict[str, QLabel] = {}
        quick_keys = [("last", "直近価格"), ("change", "変動"),
                      ("vol", "年率ボラ"), ("bars", "本数")]
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

    # ---- タブ2 予測 ----
    def _tab_fit(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("多項式回帰")
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
        lbl = QLabel("次数 n")
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
        self.sp_horizon.setSuffix(" 本")
        self.sp_window = QSpinBox()
        self.sp_window.setRange(0, 20000)
        self.sp_window.setValue(0)
        self.sp_window.setSuffix(" 本")
        for w in (self.sp_horizon, self.sp_window):
            w.setMinimumWidth(120)
        grid.addWidget(QLabel("外挿期間"), 0, 0)
        grid.addWidget(self.sp_horizon, 0, 1)
        grid.addWidget(QLabel("フィット窓"), 1, 0)
        grid.addWidget(self.sp_window, 1, 1)
        lay.addLayout(grid)

        hint = QLabel("フィット窓 0 = 全履歴データを使用")
        hint.setObjectName("Hint")
        lay.addWidget(hint)

        self.chk_log_fit = QCheckBox("対数空間フィット（BTC 長期推奨）")
        self.chk_log_fit.setChecked(True)
        self.chk_auto = QCheckBox("パラメータ変更時に自動再予測")
        self.chk_auto.setChecked(True)
        lay.addWidget(self.chk_log_fit)
        lay.addWidget(self.chk_auto)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_fit = QPushButton("多項式フィット実行")
        self.btn_fit.setObjectName("Primary")
        self.btn_fit.clicked.connect(self.start_fit)
        self.btn_clear = QPushButton("クリア")
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

        frame2, lay2 = self._panel("フィット品質")
        res_grid = QGridLayout()
        res_grid.setHorizontalSpacing(10)
        res_grid.setVerticalSpacing(8)
        self.res_labels: Dict[str, QLabel] = {}
        items = [("r2", "R² 決定係数"), ("adj", "調整済み R²"),
                 ("rmse", "RMSE 平均平方根誤差"), ("mae", "MAE 平均絶対誤差"),
                 ("mape", "MAPE 平均絶対 %"), ("sigma", "残差標準誤差 σ"),
                 ("samples", "使用サンプル数"), ("next", "次期予測")]
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

    # ---- タブ3 統計 ----
    def _tab_stats(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("主要統計（NumPy）")
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

        frame2, lay2 = self._panel("完全レポート")
        self.txt_report = QPlainTextEdit()
        self.txt_report.setObjectName("Mono")
        self.txt_report.setReadOnly(True)
        self.txt_report.setMinimumHeight(340)
        self.txt_report.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay2.addWidget(self.txt_report)

        row = QHBoxLayout()
        row.setSpacing(10)
        btn_print = QPushButton("再計算＆出力")
        btn_print.setObjectName("Primary")
        btn_print.clicked.connect(self.recompute_report)
        btn_copy = QPushButton("レポートをコピー")
        btn_copy.setObjectName("Ghost")
        btn_copy.clicked.connect(self._copy_report)
        row.addWidget(btn_print, 2)
        row.addWidget(btn_copy, 1)
        lay2.addLayout(row)
        v.addWidget(frame2)
        v.addStretch(1)
        return holder# ---- Tab4 NumPy console ----
    def _tab_numpy(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("NumPy 式")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.ed_expr = QLineEdit("np.median(close)")
        self.ed_expr.setPlaceholderText("例: np.percentile(close,[25,50,75])")
        self.ed_expr.returnPressed.connect(self.run_expression)
        btn = QPushButton("実行")
        btn.setObjectName("Primary")
        btn.clicked.connect(self.run_expression)
        row.addWidget(self.ed_expr, 1)
        row.addWidget(btn)
        lay.addLayout(row)

        hint = QLabel("利用可能: np / close / open / high / low / volume / ret / logret / idx / n / ts")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        v.addWidget(frame)

        frame2, lay2 = self._panel("クイック式")
        qgrid = QGridLayout()
        qgrid.setHorizontalSpacing(8)
        qgrid.setVerticalSpacing(8)
        for i, expr in enumerate(QUICK_EXPRS):
            b = QPushButton(expr)
            b.setObjectName("Chip")
            b.setToolTip("クリックで実行")
            b.clicked.connect(lambda _=False, e=expr: self._use_expr(e))
            qgrid.addWidget(b, i // 2, i % 2)
        lay2.addLayout(qgrid)
        v.addWidget(frame2)

        frame3, lay3 = self._panel("出力")
        self.txt_expr = QPlainTextEdit()
        self.txt_expr.setObjectName("Mono")
        self.txt_expr.setReadOnly(True)
        self.txt_expr.setMinimumHeight(260)
        lay3.addWidget(self.txt_expr)
        btn_clear = QPushButton("出力をクリア")
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
        # 注意: ワーカーは Python 側で強参照しておかなければなりません。さもないと
        # _run_worker が返った直後に GC に回収され、thread.started の接続ごと消滅して
        # worker.run が実行されません。
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
        # スレッドが終了した時点ではじめて参照を解放してよい。
        if worker in self._workers:
            self._workers.remove(worker)
        if thread in self._threads:
            self._threads.remove(thread)
        worker.deleteLater()
        thread.deleteLater()

    def _set_loading(self, on: bool, text: str = ""):
        """ビジー / アイドル状態を切り替え: 重複リクエストを防ぐためウィジェットを無効化。"""
        self._loading = on
        self.bar_busy.setVisible(on)
        for w in (self.btn_refresh, self.cb_source, self.cb_tf, self.ed_symbol):
            w.setEnabled(not on)
        if text:
            self.lbl_status.setText(text)

    def _refresh_net_label(self):
        """右下のネットワーク状態: 記憶済みの戦略、または失敗ヒントを表示。"""
        name = HTTP.preferred_name()
        if name:
            self.lbl_net.setText(f"ネットワーク: {name}")
        elif HTTP.last_error:
            self.lbl_net.setText("ネットワーク: 全戦略失敗")
        else:
            self.lbl_net.setText("ネットワーク: 未接続")

    # ============================================================ data
    def _reset_network(self):
        HTTP.reset()
        self._refresh_net_label()
        self.lbl_status.setText("ネットワーク戦略をリセットしました。次のリクエストはプロキシ 127.0.0.1:10808 から再探索します")

    def _on_tf_changed(self, index: int):
        self.tf = TIMEFRAMES[index]
        self.lbl_title.setText(f"BTC · {self.tf.label}")
        self.refresh_data()

    def refresh_data(self):
        """起動時 / 時間足変更 / ソース変更 / 取得ボタンで呼ばれる: スレッドでローソク足を取得。"""
        if self._loading:
            return                            # すでに取得中なら重複クリックを無視
        self._data_rid += 1
        rid = self._data_rid
        source = self.cb_source.currentText()
        symbol = self.ed_symbol.text().strip() or "BTCUSDT"
        tf = self.tf
        self.lbl_net.setText("ネットワーク: 接続中…")
        self._set_loading(True, f"{tf.label} のデータを {source} から取得中（プロキシ優先 127.0.0.1:10808）…")
        self._run_worker(DataWorker(rid, source, symbol, tf),
                         self._on_data_ok, self._on_data_err)

    def _on_data_ok(self, rid: int, candles):
        if rid != self._data_rid:
            return                            # より新しいリクエストに取って代わられた場合は破棄
        self._set_loading(False)
        self._refresh_net_label()
        self.candles = candles
        self._xs = np.asarray([c.ts / 1000.0 for c in candles], dtype=float)  # 秒単位（十字カーソル用）

        self.item_candles.setData([(c.ts / 1000.0, c.open, c.close, c.low, c.high)
                                   for c in candles])   # ローソク足を更新
        self.item_volume.setData([(c.ts / 1000.0, max(c.volume, 0.0), c.close >= c.open)
                                  for c in candles])   # 出来高を更新

        close = np.asarray([c.close for c in candles], dtype=float)
        self.curve_close.setData(self._xs, close, fillLevel=float(close.min()) * 0.97,
                                 brush=pg.mkBrush(247, 147, 26, 26))  # オレンジのエリア塗り

        self.clear_forecast(keep_status=True)  # 新しいデータは古い予測を無効化
        self._sync_view()
        self._update_quick_stats()
        self.vol_plot.autoRange(padding=0.02)
        self.plot.autoRange(padding=0.03)

        st = datetime.fromtimestamp(candles[0].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        en = datetime.fromtimestamp(candles[-1].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        self.lbl_status.setText(
            f"{self.cb_source.currentText()} · {self.tf.label} · {len(candles)} 本 · {st} → {en}")

        self.recompute_report()               # 統計カード + コンソールレポートを再計算

        if self.chk_auto.isChecked():
            self._auto_timer.start()          # 自動でフィットを 1 回実行

    def _on_data_err(self, rid: int, msg: str):
        if rid != self._data_rid:
            return
        self._set_loading(False)
        self._refresh_net_label()
        log(f"データ取得失敗: {msg}")
        self.lbl_status.setText(f"⚠ データ取得失敗: {msg}")
        self.lbl_cursor.setText(f"⚠ データ取得失敗: {msg}")

    def _update_quick_stats(self):
        """市場タブの 4 枚の小カード: 直近 / 変動 / 年率ボラ / 本数。"""
        close = np.asarray([c.close for c in self.candles], dtype=float)
        lr = np.diff(np.log(np.maximum(close, 1e-9)))
        total = float(close[-1] / close[0] - 1) * 100                # 期間変動 %
        vol = (float(np.std(lr, ddof=1)) * math.sqrt(self.tf.periods_per_year) * 100
               if lr.size > 1 else 0.0)                              # 年率ボラティリティ %
        self.cards["last"].setText(f"${close[-1]:,.2f}")
        self.cards["change"].setText(f"{total:+.2f}%")
        self.cards["change"].setStyleSheet(
            f"color:{C_UP if total >= 0 else C_DOWN}; font-size:17px; font-weight:700;")
        self.cards["vol"].setText(f"{vol:,.1f}%")
        self.cards["bars"].setText(str(len(self.candles)))
        s = datetime.fromtimestamp(self.candles[0].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        e = datetime.fromtimestamp(self.candles[-1].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        self.lbl_range.setText(f"レンジ {s} → {e}  中央値 "
                               f"${float(np.median(close)):,.2f}  平均 ${float(np.mean(close)):,.2f}")

    # ============================================================ stats report
    def recompute_report(self):
        """すべての統計を再計算: カードへ反映 + コンソールレポートを更新。"""
        if not self.candles:
            self.lbl_status.setText("先にデータを取得してください")
            return
        log_rule(f"統計を再計算中 · {self.cb_source.currentText()} · {self.tf.label}")
        self._report = build_report(self.candles, self.tf,
                                    self.cb_source.currentText(),
                                    self.ed_symbol.text().strip() or "BTCUSDT")
        self.txt_report.setPlainText(self._report.text)      # レポートタブの全文
        for key, (value, color) in self._report.cards.items():   # 市場タブ上部の統計カード
            lbl = self.stat_cards.get(key)
            if lbl is None:
                continue
            lbl.setText(value)
            if color:
                lbl.setStyleSheet(f"color:{color}; font-size:17px; font-weight:700;")
            else:
                lbl.setStyleSheet("")
        self.lbl_status.setText("統計の計算が完了しました。結果はコンソールにも出力済み")

    def _copy_report(self):
        if self._report.text:
            QApplication.clipboard().setText(self._report.text)
            self.lbl_status.setText("レポートをクリップボードにコピーしました")

    # ============================================================ fitting
    def _on_degree_changed(self, value: int):
        self.lbl_formula.setText(_render_degree_formula(value))
        self._schedule_auto_fit()

    def _schedule_auto_fit(self, *args):
        if self.chk_auto.isChecked() and self.candles:
            self._auto_timer.start()

    def start_fit(self):
        """現在のパラメータ（次数 / 外挿期間 / フィット窓）でバックグラウンドの多項式フィットを開始。"""
        if not self.candles:
            self.lbl_status.setText("先にデータを取得してください")
            return
        self._fit_rid += 1
        rid = self._fit_rid
        self.lbl_status.setText(f"次数 {self.sp_degree.value()} の多項式をフィット中…")
        log_rule(f"多項式回帰 · n={self.sp_degree.value()} · 外挿 {self.sp_horizon.value()} 本 "
                 f"· 窓 {self.sp_window.value() or 'すべて'} · "
                 f"{'対数空間' if self.chk_log_fit.isChecked() else '線形空間'}")
        self._run_worker(
            FitWorker(rid, self.candles, self.sp_degree.value(), self.sp_horizon.value(),
                      self.sp_window.value(), self.chk_log_fit.isChecked()),
            self._on_fit_ok, self._on_fit_err)

    def _on_fit_ok(self, rid: int, r: FitResult):
        if rid != self._fit_rid:
            return                                  # より新しいフィットに取って代わられた場合は破棄
        self._last_fit = r

        anchor_x, anchor_y = float(r.ts_hist[-1]), float(r.y_fit[-1])  # 履歴の最後の点がアンカーになる
        self.curve_fit.setData(r.ts_hist, r.y_fit)

        if r.ts_future.size:
            # 予測区間: 「アンカー + 未来外挿」を 1 本の連続曲線に連結
            xs = np.concatenate([[anchor_x], r.ts_future])
            ys = np.concatenate([[anchor_y], r.y_pred])
            self.curve_pred.setData(xs, ys)
            self.curve_up.setData(xs, np.concatenate([[anchor_y], r.upper]))
            self.curve_low.setData(xs, np.concatenate([[anchor_y], r.lower]))
            self.dot_pred.setData([xs[-1]], [ys[-1]])   # 終端マーカー
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

        # ---- フィット詳細のコンソール出力 ----
        space = "対数空間 ln(y)" if r.log_space else "線形空間"
        print(f"\n[多項式回帰の結果] n={r.degree}   空間={space}   サンプル数={r.n_points}"
              f" （窓={r.window}）   外挿={r.ts_future.size} 本")
        print(f"  係数（高次 → 定数）: "
              + ", ".join(f"{c:.6g}" for c in r.coef))
        print(f"  R²={r.r2:.6f}   調整済みR²={r.adj_r2:.6f}   RMSE={r.rmse:,.4f}   "
              f"MAE={r.mae:,.4f}   MAPE={r.mape:.4f}%   σ={r.sigma:,.4f}")
        if r.ts_future.size:
            step_days = (float(np.median(np.diff(r.ts_future))) / 86400.0
                         if r.ts_future.size > 1 else 0.0)
            print(f"  次期予測 {r.next_value:,.2f}  ({r.next_change_pct:+.2f}%)"
                  + (f"  ステップ約 {step_days:.2f} 日" if step_days else ""))
            print(f"  期間末予測 {float(r.y_pred[-1]):,.2f}"
                  f"   (現在比 {(float(r.y_pred[-1]) / float(r.y_fit[-1]) - 1) * 100:+.2f}%)")
            print(f"  信頼帯 ±1.96σ: [{float(r.lower[-1]):,.2f}, {float(r.upper[-1]):,.2f}]")
        print("─" * 70, flush=True)

        self.lbl_status.setText(
            f"フィット完了 · n={r.degree} · {space} · {r.n_points} サンプル · R²={r.r2:.4f} · "
            f"次期 {r.next_value:,.2f} ({r.next_change_pct:+.2f}%)")

    def _on_fit_err(self, rid: int, msg: str):
        if rid != self._fit_rid:
            return
        log(f"フィット失敗: {msg}")
        self.lbl_status.setText(f"⚠ フィット失敗: {msg}")

    def clear_forecast(self, keep_status: bool = False):
        """チャートからすべてのフィット / 予測曲線をクリア。keep_status=True でステータスバーを更新しない。"""
        self._last_fit = None
        self._fit_rid += 1                                # 進行中のフィットも無効化
        for item in (self.curve_fit, self.curve_pred, self.curve_up, self.curve_low):
            item.setData([], [])
        self.dot_pred.setData([], [])
        self._set_overlay_visible(False)
        for lbl in self.res_labels.values():
            lbl.setText("—")
        if not keep_status:
            self.lbl_status.setText("予測をクリアしました")
            log("予測をクリアしました")

    def _set_overlay_visible(self, on: bool):
        """フィット / 予測の描画アイテムを一括で表示・非表示に切り替える。"""
        items = [self.curve_fit, self.curve_pred, self.curve_up,
                 self.curve_low, self.dot_pred]
        if self.band is not None:
            items.append(self.band)
        for item in items:
            item.setVisible(on)

    def _update_fit_labels(self, r: FitResult):
        """フィット指標を結果パネルへ反映する。"""
        self.res_labels["r2"].setText(f"{r.r2:.4f}")
        self.res_labels["adj"].setText(f"{r.adj_r2:.4f}")
        self.res_labels["rmse"].setText(f"{r.rmse:,.2f}")
        self.res_labels["mae"].setText(f"{r.mae:,.2f}")
        self.res_labels["mape"].setText(f"{r.mape:.2f}%")
        self.res_labels["sigma"].setText(f"{r.sigma:,.2f}")
        self.res_labels["samples"].setText(f"{r.n_points}")
        color = C_UP if r.next_change_pct >= 0 else C_DOWN   # 予測の符号で色分け
        self.res_labels["next"].setText(
            f'<span style="color:{color}">{r.next_value:,.2f} '
            f'({r.next_change_pct:+.2f}%)</span>')

    # ============================================================ NumPy console
    def _numpy_env(self) -> Dict[str, object]:
        """式評価用のサンドボックス環境を構築: データを変数として公開する。"""
        close = np.asarray([c.close for c in self.candles], dtype=float)
        env = {
            "np": np, "numpy": np,
            "close": close,
            "open": np.asarray([c.open for c in self.candles], dtype=float),
            "high": np.asarray([c.high for c in self.candles], dtype=float),
            "low": np.asarray([c.low for c in self.candles], dtype=float),
            "volume": np.asarray([c.volume for c in self.candles], dtype=float),
            "ts": np.asarray([c.ts for c in self.candles], dtype=float) / 1000.0,  # 秒単位
            "ret": np.diff(close) / np.maximum(close[:-1], 1e-9),   # 単利リターン
            "logret": np.diff(np.log(np.maximum(close, 1e-9))),      # 対数リターン
            "idx": np.arange(close.size, dtype=float),
            "n": int(close.size),
            "tf": self.tf.key,                                       # 現在の時間足キー
            "periods_per_year": float(self.tf.periods_per_year),     # 年間足数
        }
        return env

    def _use_expr(self, expr: str):
        self.ed_expr.setText(expr)
        self.run_expression()

    def run_expression(self):
        """テキストボックスの式をワーカースレッドで評価する（制限付きサンドボックス）。"""
        expr = self.ed_expr.text().strip()
        if not expr:
            return
        if not self.candles:
            self.lbl_status.setText("先にデータを取得してください")
            return
        self._expr_rid += 1
        self._run_worker(ExprWorker(self._expr_rid, expr, self._numpy_env()),
                         self._on_expr_ok, self._on_expr_err)

    def _on_expr_ok(self, rid: int, expr: str, result: str):
        if rid != self._expr_rid:
            return
        log_rule("NumPy 式の評価")
        print(f"  >>> {expr}\n  {result}", flush=True)
        self.txt_expr.appendPlainText(f">>> {expr}\n{result}\n")     # コンソールウィジェットにも追記
        self.lbl_status.setText("式を評価しました（コンソールにも出力済み）")

    def _on_expr_err(self, rid: int, expr: str, msg: str):
        if rid != self._expr_rid:
            return
        log(f"式エラー {expr} → {msg}")
        self.txt_expr.appendPlainText(f">>> {expr}\n✗ {msg}\n")

    # ============================================================ view
    def _sync_view(self):
        """チェックボックスに応じてローソク足 / 終値ラインを切り替え、十字カーソルもトグル。"""
        candle_on = self.chk_candle.isChecked() and not self.chk_log_y.isChecked()
        self.item_candles.setVisible(candle_on)        # 対数モードではローソク足を表示しない
        self.curve_close.setVisible(not candle_on)     # 代わりに終値ラインを表示
        self.vline.setVisible(self.chk_cross.isChecked())
        self.hline.setVisible(self.chk_cross.isChecked())
        if not candle_on and self.candles:
            close = np.asarray([c.close for c in self.candles], dtype=float)
            self.curve_close.setData(self._xs, close,
                                     fillLevel=max(float(close.min()) * 0.97, 1e-9),
                                     brush=pg.mkBrush(247, 147, 26, 26))

    def _on_log_y_changed(self, on: bool):
        """Y 軸の対数モード切替: ローソク足は終値ラインに置き換わる。"""
        self.chk_candle.setEnabled(not on)
        if on:
            self.chk_candle.setChecked(False)
        self.plot.setLogMode(False, on)
        self._sync_view()
        if self.candles:
            self.plot.autoRange(padding=0.05)

    def _on_mouse_move(self, evt):
        """ホバー時、最寄りのローソク足にスナップして詳細を表示する。"""
        if not self.candles or not self.chk_cross.isChecked():
            return
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        x = float(self.plot.getViewBox().mapSceneToView(pos).x())   # シーン座標 → データ座標
        i = int(np.clip(np.searchsorted(self._xs, x), 0, len(self._xs) - 1))  # バイナリサーチでインデックス検索
        if i > 0 and abs(self._xs[i - 1] - x) < abs(self._xs[i] - x):
            i -= 1                                                  # 隣と比較して近い方を採用
        c = self.candles[i]
        dt = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        fmt = "%Y-%m-%d %H:%M" if self.tf.key in ("1h", "4h") else "%Y-%m-%d"
        chg = (c.close / c.open - 1) * 100 if c.open else 0.0       # この足の変動
        col = C_UP if chg >= 0 else C_DOWN
        self.vline.setPos(self._xs[i])
        self.hline.setPos(c.close)
        self.lbl_cursor.setText(
            f'<b>{dt.strftime(fmt)}</b>  O <b>{c.open:,.2f}</b>  H <b>{c.high:,.2f}</b>  '
            f'L <b>{c.low:,.2f}</b>  C <b>{c.close:,.2f}</b>  '
            f'<span style="color:{col}">{chg:+.2f}%</span>  Vol {c.volume:,.0f}')

    # ============================================================ closing
    def closeEvent(self, event):
        """終了前にすべてのバックグラウンドスレッドを停止し、QThread 残留によるプロセスハングを防ぐ。"""
        for t in list(self._threads):
            t.quit()
            t.wait(2000)
        self._threads.clear()
        self._workers.clear()
        super().closeEvent(event)


# ============================================================================
# 10. エントリポイント
# ============================================================================
def main() -> int:
    """プログラムのエントリ: コンソールの文字コード設定 → アプリ作成 → メインウィンドウ表示。"""
    # Windows コンソールの既定エンコーディングは GBK のため、"²"/"³" 系グリフの print で
    # UnicodeEncodeError が発生します。エンコーディングは維持したまま（ターミナルは読みやすい）、
    # エンコードできない文字だけ "?" に置き換えてクラッシュを回避します。
    # getattr により、reconfigure() を持たない IDLE などの疑似 stdout でも互換性を保ちます。
    getattr(sys.stdout, "reconfigure", lambda **_: None)(errors="replace")
    getattr(sys.stderr, "reconfigure", lambda **_: None)(errors="replace")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(FONT_STACK)
    font.setPointSize(10)
    app.setFont(font)

    print("=" * 74)
    print("  BTC マーケットラボ v2.2 · PyQt6 + pyqtgraph + NumPy")
    print(f"  pyqtgraph バージョン: {getattr(pg, '__version__', 'unknown')}")
    print("  ネットワーク戦略の順序: HTTP 10808 → HTTP 10809 → SOCKS5 10808 → 直接接続")
    print("  すべての統計と式の結果はこのコンソールに出力されます")
    print("=" * 74, flush=True)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())