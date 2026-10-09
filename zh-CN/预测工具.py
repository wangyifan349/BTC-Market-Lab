#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 行情实验室 v2.2 —— PyQt6 + pyqtgraph + NumPy
=================================================================
v2.2 修复:
  * 【关键】QThread worker 对象没有被 Python 持有引用，会被 GC 回收，
    导致 thread.started → worker.run 永远不会被调用：界面一直停在
    “网络：未连接”，币安数据看起来请求失败。现在 _run_worker 会持有
    worker / thread 的强引用，直到线程结束后再释放。
  * 【关键】Windows 控制台默认 GBK，打印 “R²”/“σ” 等字符会抛
    UnicodeEncodeError，导致拟合结果回调中途中断。启动时把 stdout/stderr
    切到 UTF-8（errors=replace）。
  * 精简依赖导入：PyQt6 / pyqtgraph / NumPy / PySocks 均已安装，直接 import，
    不再用 try/except 吞掉导入错误。
  * 删除版本兼容包装（_safe_downsampling / _safe_clip / _make_fill_between 等），
    直接调用当前 pyqtgraph 稳定 API。
  * 网络状态标签更准确：区分“连接中 / 已连接 / 全部策略失败”。

依赖:
    pip install PyQt6 pyqtgraph numpy pysocks

运行（请用命令行，不要用 IDLE）:
    python 预测工具.py
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
# 0. 控制台日志
# ============================================================================
def log(*args) -> None:
    print(f"[{datetime.now():%H:%M:%S}]", *args, flush=True)


def log_rule(title: str = "") -> None:
    if title:
        print(f"\n{'─' * 4} {title} {'─' * max(4, 58 - len(title))}", flush=True)
    else:
        print("─" * 70, flush=True)


# ============================================================================
# 1. 主题配色（绿涨红跌）& 绘图配置
# ============================================================================
C_BG      = "#0b0e13"       # 窗口 / 图表背景
C_PANEL   = "#141a24"       # 主面板底色
C_PANEL_2 = "#111722"       # 侧栏子面板底色
C_INPUT   = "#0f151e"       # 输入框 / 卡片底色
C_BORDER  = "#232e3e"       # 边框颜色
C_TEXT    = "#e8eef6"       # 主文字
C_MUTED   = "#93a1b1"       # 次要文字
C_ACCENT  = "#f7931a"      # 比特币橙
C_UP      = "#16c784"      # 涨 —— 绿
C_DOWN    = "#ea3943"      # 跌 —— 红
C_FIT     = "#4fc3f7"      # 拟合曲线
C_PRED    = "#ffb347"      # 预测曲线
C_BAND    = (255, 179, 71, 30)  # 预测置信带颜色 (RGBA)

pg.setConfigOptions(antialias=True, background=C_BG, foreground=C_MUTED)  # 全局抗锯齿 + 主题色

FONT_STACK = ["Inter", "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC"]  # 界面字体栈


# ============================================================================
# 2. 网络层：代理 127.0.0.1:10808 → 失败自动回退直连
# ============================================================================
PROXY_HOST = "127.0.0.1"   # 本地代理地址（v2rayN / clash 等）
PROXY_PORT = 10808         # 本地代理端口（HTTP/SOCKS 混入模式亦可）

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class Http:
    """
    按顺序尝试多种网络策略，失败自动降级：
        HTTP 代理 10808 → HTTP 代理 10809 → SOCKS5 代理 10808 → 直连(无代理)
    一旦某个策略成功，后续请求优先复用它。
    """

    STRATEGIES = (
        # (显示名, key, 代理目标, 超时秒)
        (f"HTTP 代理 {PROXY_HOST}:{PROXY_PORT}", "http8", f"http://{PROXY_HOST}:{PROXY_PORT}", 6.0),
        (f"HTTP 代理 {PROXY_HOST}:10809", "http9", f"http://{PROXY_HOST}:10809", 6.0),
        (f"SOCKS5 代理 {PROXY_HOST}:{PROXY_PORT}", "socks8", f"socks5h://{PROXY_HOST}:{PROXY_PORT}", 6.0),
        ("直连（无代理）", "direct", "", 20.0),
    )

    def __init__(self):
        self._openers: Dict[str, urllib.request.OpenerDirector] = {}  # key -> 已构建的 opener 缓存
        self._preferred: str | None = None                            # 当前优选的策略 key
        self.last_error: str = ""                                     # 最近一次全部策略失败的摘要
        self._lock = threading.Lock()                                 # 保证多线程下缓存 / 优选状态安全

    @staticmethod
    def _make_opener(key: str, target: str):
        if key == "direct":
            # 显式清空代理，确保是真正的“直连”
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if key.startswith("http"):
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": target, "https": target}))
        # SOCKS5 —— PySocks 已安装，直接构造
        handler = sockshandler.SocksiPyHandler(
            socks.SOCKS5, PROXY_HOST, PROXY_PORT, rdns=True)
        return urllib.request.build_opener(handler)

    def _opener(self, key: str, target: str):
        with self._lock:
            if key not in self._openers:
                self._openers[key] = self._make_opener(key, target)   # 首次使用时才构建
            return self._openers[key]

    def get_json(self, url: str, timeout: float | None = None):
        order: List[str] = []
        if self._preferred:
            order.append(self._preferred)                             # 之前成功的策略放最前
        order += [s[1] for s in self.STRATEGIES if s[1] != self._preferred]  # 其余按固定顺序兜底

        errors: List[str] = []
        for key in order:
            name, _, target, default_to = next(
                (s for s in self.STRATEGIES if s[1] == key), (key, key, "", 15.0))
            try:
                opener = self._opener(key, target)                    # 尝试当前策略
                req = urllib.request.Request(url, headers=UA_HEADERS)
                with opener.open(req, timeout=timeout or default_to) as resp:
                    raw = resp.read().decode("utf-8", "replace")      # 容错解码，坏字符替换
                data = json.loads(raw)                                # 解析 JSON
                if self._preferred != key:
                    with self._lock:
                        self._preferred = key                         # 记住该策略，后续优先
                    log(f"网络策略 → {name}（已记住，后续优先使用）")
                self.last_error = ""
                return data
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name} ✗ {exc}")                      # 记下失败原因
                log(f"网络策略 {name} 失败：{exc}")                     # 继续尝试下一个策略
        self.last_error = " | ".join(errors)
        raise RuntimeError("所有网络策略均失败 | " + self.last_error)

    def preferred_name(self) -> str:
        for s in self.STRATEGIES:
            if s[1] == self._preferred:
                return s[0]                                           # 返回显示名（供状态栏）
        return ""

    def reset(self) -> None:
        with self._lock:
            self._preferred = None                                    # 清空记住的策略，重新探测
        self.last_error = ""
        log("网络策略已重置")


HTTP = Http()


# ============================================================================
# 3. 数据模型 & 周期
# ============================================================================
@dataclass(frozen=True)
class Candle:
    """一根 K 线，字段顺序与各交易所返回数组一致。"""
    ts: float            # 毫秒时间戳 (UTC)
    open: float          # 开
    high: float          # 高
    low: float           # 低
    close: float         # 收
    volume: float = 0.0  # 成交量（币安为计价币数量）


@dataclass(frozen=True)
class Timeframe:
    """周期配置：各家数据源的周期参数 + 本地换算参数。"""
    key: str                # 内部唯一标识，如 "1d"
    label: str              # 界面显示名，如 "日线"
    binance: str            # Binance 周期参数，如 "1d"
    okx: str                # OKX 周期参数，如 "1D"
    cg_days: str            # CoinGecko market_chart 的 days 参数
    target: int             # 目标加载 K 线根数（分页直到取满）
    periods_per_year: float # 年化折算系数：1 年包含多少期
    resample: str = ""      # 非空则从原始 K 线重采样成的周期（如年线 "Y"）


TIMEFRAMES: List[Timeframe] = [
    Timeframe("1h", "1 小时", "1h", "1H", "90", 1000, 24 * 365),
    Timeframe("4h", "4 小时", "4h", "4H", "90", 1000, 6 * 365),
    Timeframe("1d", "日线",   "1d", "1D", "max", 2000, 365),
    Timeframe("1w", "周线",   "1w", "1W", "max", 1000, 52),
    Timeframe("1M", "月线",   "1M", "1M", "max", 500, 12),
    Timeframe("1Y", "年线",   "1M", "1M", "max", 500, 1, resample="Y"),  # 月线原始数据重采样成 年线
]
TF_BY_KEY = {tf.key: tf for tf in TIMEFRAMES}  # 快速按 key 查周期对象


# ============================================================================
# 4. 行情抓取
# ============================================================================
BINANCE_HOSTS = (   # Binance 官方多机房，逐个重试
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api1.binance.com",
)


def _dedup(candles: Sequence[Candle]) -> List[Candle]:
    bucket = {int(c.ts): c for c in candles}   # 按毫秒时间戳去重（后到覆盖先到）
    return [bucket[k] for k in sorted(bucket)] # 再按时间升序返回


def _binance_paged(host: str, symbol: str, interval: str, target: int) -> List[Candle]:
    """按 endTime 向前分页抓取 Binance K 线，直到凑够 target 根。"""
    out: List[Candle] = []
    end_ms = None
    while len(out) < target:
        limit = int(min(1000, max(50, target - len(out))))  # 单页上限 1000
        query = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_ms is not None:
            query["endTime"] = int(end_ms)                  # 限定在上一页最早一根之前
        rows = HTTP.get_json(f"{host}/api/v3/klines?" + urllib.parse.urlencode(query))
        if not isinstance(rows, list) or not rows:
            break                                            # 没有更多数据
        out = [Candle(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5])) for r in rows] + out      # 新页追加到最前面
        end_ms = int(rows[0][0]) - 1                         # 下一页从最早一根之前继续
        if len(rows) < limit:
            break                                            # 返回不足一页，说明已到开头
        time.sleep(0.1)                                      # 轻量限速，避免风控
    return _dedup(out)


def fetch_binance(symbol: str, tf: Timeframe) -> List[Candle]:
    symbol = symbol.upper().replace("-", "").replace("/", "")  # 归一化为 BTCUSDT 形式
    err = None
    for host in BINANCE_HOSTS:                                  # 多机房依次重试
        try:
            data = _binance_paged(host, symbol, tf.binance, tf.target)
            if data:
                log(f"Binance {host} 返回 {len(data)} 根 K 线")
                return data
        except Exception as exc:  # noqa: BLE001
            err = exc
            log(f"Binance {host} 失败：{exc}")
    raise RuntimeError(f"Binance 获取失败：{err}")


def _to_okx_inst(symbol: str) -> str:
    """把 BTCUSDT / BTC-USDT 统一成 OKX 需要的 BTC-USDT 形式。"""
    s = symbol.upper().replace("/", "-")
    if "-" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):   # 识别计价币并补上短横线
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
            query["after"] = after                            # 游标：向历史方向翻页
        js = HTTP.get_json("https://www.okx.com/api/v5/market/history-candles?"
                           + urllib.parse.urlencode(query))
        rows = js.get("data") or []
        if not rows:
            break
        out = [Candle(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5])) for r in rows] + out
        after = rows[-1][0]                                   # 下一页从最早一条继续
        if len(rows) < 100:
            break
        time.sleep(0.12)
    data = _dedup(out)
    if not data:
        raise RuntimeError("OKX 返回空数据")
    log(f"OKX {inst} 返回 {len(data)} 根 K 线")
    return data


def fetch_coingecko(tf: Timeframe) -> List[Candle]:
    js = HTTP.get_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                       f"?vs_currency=usd&days={tf.cg_days}", timeout=25.0)
    prices = js.get("prices") or []   # [[毫秒时间戳, 价格], ...]
    vols = {int(t): float(v) for t, v in (js.get("total_volumes") or [])}  # 时间戳 -> 成交量
    if not prices:
        raise RuntimeError("CoinGecko 返回空数据")
    candles = [Candle(float(t), float(p), float(p), float(p), float(p), vols.get(int(t), 0.0))
               for t, p in prices]   # CoinGecko 只有价格点，OHLC 用同一价格填充
    log(f"CoinGecko 返回 {len(candles)} 个价格点")
    return candles[-tf.target:] if len(candles) > tf.target else candles  # 截取尾部目标根数


def resample(candles: Sequence[Candle], rule: str) -> List[Candle]:
    """把细周期 K 线聚合成更大周期（年 / 月 / 周 / 日）。"""
    if not candles:
        return []
    groups: Dict[tuple, List[Tuple[int, Candle]]] = {}
    order: List[tuple] = []
    for c in candles:
        dt = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        if rule == "Y":
            key = (dt.year,)                                    # 周期桶 key：年份
            start = datetime(dt.year, 1, 1, tzinfo=timezone.utc)
        elif rule == "M":
            key = (dt.year, dt.month)                           # 月份
            start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
        elif rule == "W":
            monday = dt - timedelta(days=dt.weekday())          # 周一作为一周起点
            key = (monday.year, monday.month, monday.day)
            start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        else:
            key = (dt.year, dt.month, dt.day)                   # 自然日
            start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        if key not in groups:
            groups[key] = []
            order.append(key)                                   # 记住出现顺序
        groups[key].append((int(start.timestamp() * 1000), c))

    result: List[Candle] = []
    for key in order:
        items = groups[key]
        cs = [c for _, c in items]
        result.append(Candle(
            ts=float(items[0][0]),
            open=cs[0].open,             # 周期开 = 第一根开
            high=max(c.high for c in cs),   # 周期高 = 区间最高
            low=min(c.low for c in cs),     # 周期低 = 区间最低
            close=cs[-1].close,          # 周期收 = 最后一根收
            volume=float(sum(c.volume for c in cs)),  # 成交量和
        ))
    result.sort(key=lambda c: c.ts)
    return result


def load_market(source: str, symbol: str, tf: Timeframe) -> List[Candle]:
    """统一入口：按数据源抓取 → （可选）重采样 → 去重，返回干净的 K 线列表。"""
    if source == "Binance":
        candles = fetch_binance(symbol, tf)
    elif source == "OKX":
        candles = fetch_okx(symbol, tf)
    else:
        candles = fetch_coingecko(tf)

    if tf.resample:                      # 需要更粗周期（如年线）时对原始数据重采样
        before = len(candles)
        candles = resample(candles, tf.resample)
        log(f"重采样 {before} → {len(candles)} 根（{tf.label}）")

    candles = _dedup(candles)
    if len(candles) < 10:                # 数据太少时统计 / 拟合都没意义
        raise RuntimeError(f"有效数据过少（{len(candles)} 根），请更换数据源或周期")
    return candles


# ============================================================================
# 5. NumPy 统计报告
# ============================================================================
def _skew(a: np.ndarray) -> float:
    if a.size < 2:
        return 0.0
    m, s = float(a.mean()), float(a.std(ddof=0))
    return 0.0 if s == 0 else float(np.mean(((a - m) / s) ** 3))


def _kurt(a: np.ndarray) -> float:
    """超额峰度（正态分布 = 0）。"""
    if a.size < 2:
        return 0.0
    m, s = float(a.mean()), float(a.std(ddof=0))
    return 0.0 if s == 0 else float(np.mean(((a - m) / s) ** 4) - 3.0)  # E[z^4]-3


def _max_streak(flags: np.ndarray) -> int:
    """连续为真的最长段数（连涨 / 连跌期数）。"""
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0     # 连续则 +1，断开清零
        if cur > best:
            best = cur
    return int(best)


def _ema_series(a: np.ndarray, span: int) -> np.ndarray:
    """递推计算 EMA 序列：alpha=2/(span+1)。"""
    if a.size == 0:
        return np.asarray([])
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(a, dtype=float)
    out[0] = a[0]                     # 起点取第一个数
    for i in range(1, a.size):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(a: np.ndarray, period: int = 14) -> float:
    """Wilder RSI，用平滑后的平均涨 / 跌计算。"""
    if a.size < period + 1:
        return float("nan")
    d = np.diff(a)
    gain = np.where(d > 0, d, 0.0)    # 上涨幅度
    loss = np.where(d < 0, -d, 0.0)   # 下跌幅度
    ag = float(gain[:period].mean())  # 初始平均涨幅
    al = float(loss[:period].mean())  # 初始平均跌幅
    for i in range(period, d.size):   # Wilder 平滑递推
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
    if al == 0:
        return 100.0                  # 无下跌 => RSI 满分
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _pct(v: float) -> str:
    return f"{v * 100:+,.2f}%"   # 小数转百分比字符串，带正负号


def _money(v: float) -> str:
    return f"${v:,.2f}"          # 千分位货币格式


@dataclass
class Report:
    text: str = ""
    cards: Dict[str, Tuple[str, str]] = field(default_factory=dict)


def build_report(candles: Sequence[Candle], tf: Timeframe,
                 source: str, symbol: str) -> Report:
    """把能用 numpy 算的都算一遍：既打印到控制台，也填给界面。"""
    close = np.asarray([c.close for c in candles], dtype=float)   # 收盘价序列
    hi = np.asarray([c.high for c in candles], dtype=float)       # 最高价序列
    lo = np.asarray([c.low for c in candles], dtype=float)        # 最低价序列
    vol = np.asarray([c.volume for c in candles], dtype=float)    # 成交量序列
    ts = np.asarray([c.ts for c in candles], dtype=float) / 1000.0  # 秒级时间戳
    n = close.size                                                 # 样本数

    sr = np.diff(close) / np.maximum(close[:-1], 1e-9)          # 简单收益率
    lr = np.diff(np.log(np.maximum(close, 1e-9)))               # 对数收益率
    ann = float(tf.periods_per_year)                              # 年化折算期数

    lines: List[str] = []
    cards: Dict[str, Tuple[str, str]] = {}

    def add(text: str) -> None:
        lines.append(text)

    start = datetime.fromtimestamp(ts[0], tz=timezone.utc)
    end = datetime.fromtimestamp(ts[-1], tz=timezone.utc)

    # ---------------- 头部 ----------------
    add("=" * 74)
    add(f"  BTC 行情统计报告   {source} · {symbol} · {tf.label}")
    add(f"  样本 {n} 根   区间 {start:%Y-%m-%d} → {end:%Y-%m-%d}   "
        f"（{(end - start).days} 天）")
    add("=" * 74)

    # ---------------- 价格 ----------------
    p_mean, p_med = float(np.mean(close)), float(np.median(close))
    p_std = float(np.std(close, ddof=1)) if n > 1 else 0.0
    p_var = float(np.var(close, ddof=1)) if n > 1 else 0.0
    p_min, p_max = float(np.min(close)), float(np.max(close))
    p_ptp = float(np.ptp(close))
    q = np.percentile(close, [5, 25, 50, 75, 95])
    iqr = float(np.percentile(close, 75) - np.percentile(close, 25))

    log_rule("价格统计 numpy")
    add("【价格·numpy】close = np.array([...])")
    add(f"  np.mean       均价        {_money(p_mean)}")
    add(f"  np.median     中位价      {_money(p_med)}")
    add(f"  np.std(ddof=1) 标准差     ${p_std:,.2f}   （变异系数 {p_std / p_mean * 100:.2f}%）")
    add(f"  np.var(ddof=1) 方差       {p_var:,.2f}")
    add(f"  np.min/np.max 最低/最高   {_money(p_min)} / {_money(p_max)}")
    add(f"  np.ptp        极差        {_money(p_ptp)}   （振幅 {p_ptp / p_min * 100:.2f}%）")
    add(f"  np.percentile 5/25/50/75/95  "
        + " / ".join(f"{x:,.0f}" for x in q))
    add(f"  np.subtract(q75,q25) IQR  {iqr:,.2f}")
    cards["median"] = (_money(p_med), "")
    cards["mean"] = (_money(p_mean), "")
    cards["pstd"] = (f"${p_std:,.2f}", "")
    cards["range"] = (f"{_money(p_min)}\n{_money(p_max)}", "")

    # ---------------- 涨跌幅 ----------------
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

    peak = np.maximum.accumulate(close)          # 滚动历史峰值
    dd = close / np.maximum(peak, 1e-9) - 1.0    # 相对峰值的回撤比例（<=0）
    mdd = float(np.min(dd)) if n else 0.0        # 最大回撤
    mdd_i = int(np.argmin(dd)) if n else 0       # 回撤最深的位置下标
    calmar = (ann_ret / abs(mdd)) if mdd < 0 else float("inf")   # 卡玛比率 = 年化/|MDD|

    def _d(idx: int) -> str:
        """收益率 sr[i] 对应 close[i+1] 这一期。"""
        return datetime.fromtimestamp(ts[min(idx + 1, n - 1)],
                                      tz=timezone.utc).strftime("%Y-%m-%d")

    log_rule("收益 / 涨跌幅统计 numpy")
    add("【收益·numpy】sr = np.diff(close) / close[:-1]")
    add(f"  区间涨跌幅   (close[-1]/close[0]-1)  {_pct(total)}")
    add(f"  np.mean(sr)        平均单期涨跌       {_pct(mean_sr)}")
    add(f"  np.median(sr)      中位单期涨跌       {_pct(med_sr)}")
    add(f"  np.std(sr, ddof=1) 单期波动           {_pct(std_sr)}")
    add(f"  np.max(sr)         最大单期涨幅       {_pct(best)}   @ {_d(best_i)}")
    add(f"  np.min(sr)         最大单期跌幅       {_pct(worst)}   @ {_d(worst_i)}")
    add(f"  np.sum(sr>0)/np.sum(sr<0)  上涨 {up} / 下跌 {down} / 平盘 {flat}"
        f"   上涨占比 {up_ratio * 100:.2f}%")
    add(f"  最长连涨 / 连跌     {_max_streak(sr > 0)} / {_max_streak(sr < 0)} 期")
    add(f"  年化收益(几何)      {_pct(ann_ret)}   （每期 {ann:g} 期）")
    add(f"  年化波动率          {ann_vol * 100:,.2f}%")
    add(f"  夏普比率(无风险=0)  {sharpe:,.3f}")
    add(f"  最大回撤            {_pct(mdd)}   @ {_d(mdd_i)}"
        f"   （从峰值 {_money(float(peak[mdd_i]))} 回撤）")
    add(f"  卡玛比率            {calmar:,.3f}" if math.isfinite(calmar) else "  卡玛比率            ∞")
    add(f"  累计净值 np.prod(1+sr) 末值 {float(np.prod(1 + sr)):,.4f}")
    add(f"  np.quantile(sr, [0.05,0.5,0.95])  "
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

    # ---------------- 分布形态 ----------------
    sk, ku = _skew(lr), _kurt(lr)
    log_rule("分布形态 numpy")
    add("【分布·numpy】")
    add(f"  对数收益均值/标准差  {float(np.mean(lr)) if lr.size else 0:+.6f} / "
        f"{float(np.std(lr, ddof=1)) if lr.size > 1 else 0:.6f}")
    add(f"  偏度  Skewness      {sk:+.4f}   （>0 右偏）")
    add(f"  峰度  Excess Kurt   {ku:+.4f}   （>0 尖峰厚尾）")
    if n > 1:
        add(f"  np.corrcoef(close, np.arange(n))  价格与时间的相关性 "
            f"{float(np.corrcoef(close, np.arange(n))[0, 1]):+.4f}")
    if vol.size > 1 and float(np.std(vol)) > 0:
        add(f"  np.corrcoef(close, volume)        价格与成交量相关性 "
            f"{float(np.corrcoef(close, vol)[0, 1]):+.4f}")
    if lr.size > 2:
        add(f"  np.corrcoef(lr[:-1], lr[1:])     收益一阶自相关 "
            f"{float(np.corrcoef(lr[:-1], lr[1:])[0, 1]):+.4f}")
    cards["skew"] = (f"{sk:+.3f}", "")
    cards["kurt"] = (f"{ku:+.3f}", "")

    # ---------------- 趋势 / 指标 ----------------
    idx = np.arange(n, dtype=float)             # 样本序号，用于回归 / 相关性
    slope, _intercept = np.polyfit(idx, np.log(np.maximum(close, 1e-9)), 1)  # log 价格线性趋势
    trend_ann = math.exp(float(slope) * ann) - 1      # 趋势斜率折算的年化
    grad = float(np.gradient(close)[-1]) if n > 2 else 0.0  # 最新边际变化

    def _sma(w: int) -> float:
        return float(np.mean(close[-w:])) if n >= w else float("nan")

    ema12, ema26 = _ema_series(close, 12), _ema_series(close, 26)  # 快 / 慢 EMA
    macd_series = ema12 - ema26                       # MACD（DIF）
    signal = _ema_series(macd_series, 9)              # DEA（signal 线）
    rsi14 = _rsi(close, 14)
    boll_w = 20                                       # BOLL 窗口
    if n >= boll_w:
        b_mid = float(np.mean(close[-boll_w:]))       # 中轨 = SMA20
        b_sd = float(np.std(close[-boll_w:], ddof=0)) # 标准差（总体 σ）
    else:
        b_mid = b_sd = float("nan")

    log_rule("趋势与指标 numpy")
    add("【趋势·numpy】")
    add(f"  np.polyfit(idx, log(close), 1)  线性趋势斜率/年化  {float(slope):+.6f} / {_pct(trend_ann)}")
    add(f"  np.gradient(close)[-1]          最新边际变化       {grad:+,.2f}")
    add(f"  SMA20 / SMA50 / SMA200          "
        f"{_money(_sma(20))} / {_money(_sma(50))} / {_money(_sma(200))}")
    add(f"  EMA12 / EMA26                   {_money(float(ema12[-1]))} / {_money(float(ema26[-1]))}")
    add(f"  MACD / Signal                   {float(macd_series[-1]):+,.2f} / {float(signal[-1]):+,.2f}")
    add(f"  RSI(14)                         {rsi14:.2f}")
    add(f"  BOLL20 中轨 / ±2σ               {_money(b_mid)}  ±{b_sd * 2:,.2f}")
    add(f"  平均振幅 np.mean(high-low)      {float(np.mean(hi - lo)):,.2f}"
        f"   （占现价 {float(np.mean(hi - lo)) / float(close[-1]) * 100:.2f}%）")
    add("【成交量·numpy】")
    add(f"  np.sum / np.mean / np.median    {float(np.sum(vol)):,.0f} / "
        f"{float(np.mean(vol)):,.2f} / {float(np.median(vol)):,.2f}")
    cards["rsi"] = (f"{rsi14:.1f}", C_UP if rsi14 >= 50 else C_DOWN)

    add("")
    add(f"  最新价 {_money(float(close[-1]))}    最近一根涨跌 "
        f"{_pct(float(close[-1] / close[-2] - 1)) if n > 1 else '—'}")
    add("=" * 74)

    text = "\n".join(lines)
    print(text, flush=True)
    return Report(text=text, cards=cards)


# ============================================================================
# 6. 多项式回归预测
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
    最小二乘拟合 y = a_n x^n + … + a_0
      * x 先归一化到 [-1, 1]，避免高阶多项式矩阵病态（高阶能用的关键）
      * log_space=True 先取 ln 再拟合，贴合 BTC 的长期指数增长
    """
    if len(candles) < 4:
        raise ValueError("数据点太少，无法拟合")
    if window and window > 0:
        if window < degree + 3:
            raise ValueError(f"拟合窗口不能小于阶数+3（当前窗口 {window}）")
        candles = candles[-window:]

    n = len(candles)
    if n < degree + 3:
        raise ValueError(f"有效数据 {n} 根，不足以拟合 {degree} 阶（至少需 {degree + 3} 根）")

    y = np.asarray([c.close for c in candles], dtype=float)   # 因变量：收盘价
    ts = np.asarray([c.ts for c in candles], dtype=float) / 1000.0  # 秒级时间戳
    if np.any(y <= 0):        # 价格出现非正数时无法取对数，退回线性空间
        log_space = False

    idx = np.arange(n, dtype=float)
    centre = float(idx.mean())                             # 归一化中心
    half = float((idx[-1] - idx[0]) / 2.0) or 1.0          # 归一化半径
    X = (idx - centre) / half                              # x ∈ [-1,1]，避免矩阵病态
    horizon = max(int(horizon), 0)
    Xf = (np.arange(n, n + horizon, dtype=float) - centre) / half if horizon else np.asarray([])

    target = np.log(y) if log_space else y                 # 对数空间：先取 ln
    coef = np.polyfit(X, target, degree)                   # 最小二乘求多项式系数
    fit = np.polyval(coef, X)                              # 历史段的拟合值
    pred = np.polyval(coef, Xf) if Xf.size else np.asarray([])  # 未来外推段
    if log_space:
        fit = np.exp(fit)                                  # 还原为价格量纲
        pred = np.exp(pred) if pred.size else pred

    resid = y - fit                                        # 残差 = 真实 - 拟合
    ss_res = float(np.sum(resid ** 2))                     # 残差平方和
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1e-12   # 总平方和（防除零）
    r2 = 1.0 - ss_res / ss_tot                             # 决定系数
    k = degree
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / max(n - k - 1, 1)  # 自由度校正后 R²
    rmse = float(np.sqrt(ss_res / n))                      # 均方根误差
    mae = float(np.mean(np.abs(resid)))                    # 平均绝对误差
    mape = float(np.mean(np.abs(resid / np.maximum(y, 1e-9))) * 100.0)  # 平均绝对百分比误差 %
    dof = max(n - k - 1, 1)                                # 自由度
    sigma = float(np.sqrt(ss_res / dof))                   # 残差标准误

    step = float(np.median(np.diff(ts))) if n > 1 else 86400.0   # 平均周期时长（秒）
    ts_future = ts[-1] + step * np.arange(1, horizon + 1) if horizon else np.asarray([])

    if pred.size:
        upper = pred + band_k * sigma                      # 置信带上界
        lower = np.maximum(pred - band_k * sigma, 1e-9)    # 置信带下界（不为负）
        next_value = float(pred[0])                        # 下一期预测值
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
# 7. 子线程 Worker
# ============================================================================
class DataWorker(QObject):
    """后台抓取 K 线数据，完成后通过 ok / err 信号回主线程。"""
    ok = pyqtSignal(int, object)   # (rid, candles)
    err = pyqtSignal(int, str)     # (rid, 错误消息)

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
                msg += "（可切换数据源为 OKX / CoinGecko）"   # 给用户换源提示
            self.err.emit(self.rid, msg)


class FitWorker(QObject):
    """后台执行多项式回归预测。"""
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


SAFE_BUILTINS = {   # 表达式控制台只暴露安全的内建函数，不暴露 import / open 等
    "abs": abs, "len": len, "min": min, "max": max, "sum": sum, "round": round,
    "range": range, "float": float, "int": int, "list": list, "tuple": tuple,
    "dict": dict, "sorted": sorted, "enumerate": enumerate, "zip": zip,
    "print": print, "any": any, "all": all, "bool": bool, "str": str,
}


class ExprWorker(QObject):
    """在子线程里执行 NumPy 表达式，避免复杂计算卡住界面。"""
    ok = pyqtSignal(int, str, str)   # (rid, 表达式, 格式化结果)
    err = pyqtSignal(int, str, str)  # (rid, 表达式, 错误消息)

    def __init__(self, rid: int, expr: str, env: Dict[str, object]):
        super().__init__()
        self.rid, self.expr, self.env = rid, expr, env

    @pyqtSlot()
    def run(self):
        try:
            with np.errstate(all="ignore"):
                # 严格受限环境：仅 SAFE_BUILTINS + 预先注入的 numpy 数据环境
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
# 8. QPicture 预渲染图形项
# ============================================================================
class CandlestickItem(pg.GraphicsObject):
    """自定义 K 线蜡烛图：提前渲染进 QPicture，缩放滚动性能更好。"""

    def __init__(self):
        super().__init__()
        self._up, self._down = QColor(C_UP), QColor(C_DOWN)   # 涨绿 / 跌红
        self._bars: List[Tuple[float, float, float, float, float]] = []
        self._pic, self._rect = QPicture(), QRectF()          # 预渲染缓存 + 包围盒

    def setData(self, bars: Sequence[Tuple[float, float, float, float, float]]):
        """更新数据：每根 (x秒, open, close, low, high)。"""
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
        span = bars[-1][0] - bars[0][0]                    # 整段时间跨度
        step = span / max(len(bars) - 1, 1)                # 平均每根间隔
        w = step * 0.62                                    # 蜡烛宽度约占间隔 62%
        for x, o, c, low, high in bars:
            col = self._up if c >= o else self._down       # 收>=开 为阳线（绿）
            pen = QPen(col)
            pen.setWidth(1)
            pen.setCosmetic(True)                          # 线宽不随缩放改变
            p.setPen(pen)
            p.drawLine(QPointF(x, low), QPointF(x, high))  # 上下影线
            p.setBrush(QBrush(col))
            top, bot = max(o, c), min(o, c)
            if top - bot <= 1e-12:                         # 开收相等：画一条横线
                p.drawLine(QPointF(x - w / 2, top), QPointF(x + w / 2, top))
            else:
                p.drawRect(QRectF(x - w / 2, bot, w, top - bot))   # 实体
        p.end()
        y_lo = min(b[3] for b in bars)                     # 全局最低影线
        y_hi = max(b[4] for b in bars)                     # 全局最高影线
        self._rect = QRectF(bars[0][0] - w, y_lo, span + 2 * w, (y_hi - y_lo) or 1e-9)

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self._pic)

    def boundingRect(self) -> QRectF:
        return self._rect


class VolumeItem(pg.GraphicsObject):
    """成交量柱，颜色跟随涨跌（绿涨红跌）。"""
    def __init__(self):
        super().__init__()
        self._bars: List[Tuple[float, float, bool]] = []   # (x秒, 成交量, 是否上涨)
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
        span = bars[-1][0] - bars[0][0]                    # 时间跨度
        step = span / max(len(bars) - 1, 1)                # 每根间隔
        w = step * 0.62                                    # 柱宽
        vmax = max(b[1] for b in bars) or 1.0              # 最大成交量，用于包围盒
        for x, v, is_up in bars:
            col = QColor(C_UP if is_up else C_DOWN)
            col.setAlpha(165)                              # 半透明
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))
            p.drawRect(QRectF(x - w / 2, 0.0, w, v))       # 从 0 向上画柱
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
# 9. 主窗口
# ============================================================================
STAT_CARDS = [
    ("median", "中位价 np.median"), ("mean", "均价 np.mean"),
    ("pstd", "价格标准差 np.std"), ("range", "区间最低 / 最高"),
    ("total", "区间涨跌幅"), ("medret", "单期中位涨跌"),
    ("annret", "年化收益(几何)"), ("annvol", "年化波动率"),
    ("mdd", "最大回撤"), ("sharpe", "夏普比率"),
    ("best", "最大单期涨幅"), ("worst", "最大单期跌幅"),
    ("upratio", "上涨期占比"), ("rsi", "RSI(14)"),
    ("skew", "偏度 Skew"), ("kurt", "峰度 Kurt"),
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
    """主窗口：左侧图表 + 右侧四个 Tab。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BTC 行情实验室 v2.2 · 统计 / NumPy / 多项式回归预测")
        self.resize(1520, 940)
        self.setMinimumSize(1180, 780)

        self.candles: List[Candle] = []          # 当前 K 线数据
        self.tf: Timeframe = TF_BY_KEY["1d"]     # 当前周期
        self._xs = np.asarray([])                # 秒级时间戳（用于光标定位）
        self._data_rid = 0                       # 数据请求自增 ID，丢弃过期结果
        self._fit_rid = 0                        # 拟合请求自增 ID
        self._expr_rid = 0                       # 表达式请求自增 ID
        self._threads: List[QThread] = []        # 存活的后台线程
        self._workers: List[QObject] = []        # 强引用 worker，防止被 GC
        self._last_fit: FitResult | None = None  # 最近一次拟合结果
        self._loading = False                    # 是否有数据请求在途
        self._report = Report()                  # 最近一次统计报告

        self._build_ui()
        self._apply_style()

        # 参数变化后防抖：320ms 内多次触发只执行最后一次拟合
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(320)
        self._auto_timer.timeout.connect(self.start_fit)

        QTimer.singleShot(80, self.refresh_data)  # 启动后自动拉取一次数据

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
        self.lbl_status = QLabel("准备就绪")
        self.lbl_status.setObjectName("Status")
        self.lbl_net = QLabel("网络：未连接")
        self.lbl_net.setObjectName("Status")
        self.bar_busy = QProgressBar()
        self.bar_busy.setRange(0, 0)
        self.bar_busy.setFixedWidth(110)
        self.bar_busy.setTextVisible(False)
        self.bar_busy.hide()
        bar.addWidget(self.lbl_status, 1)
        bar.addPermanentWidget(self.lbl_net)
        bar.addPermanentWidget(self.bar_busy)

    # ---------------------------------------------------- 左侧图表
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
            f'<span style="color:{C_ACCENT}">━</span> 价格　'
            f'<span style="color:{C_FIT}">━</span> 多项式拟合　'
            f'<span style="color:{C_PRED}">┄</span> 预测外推　'
            f'<span style="color:{C_UP}">●</span>涨<span style="color:{C_DOWN}">●</span>跌')
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
        self.chk_candle = QCheckBox("K 线蜡烛")
        self.chk_candle.setChecked(True)
        self.chk_log_y = QCheckBox("对数坐标")
        self.chk_grid = QCheckBox("网格")
        self.chk_grid.setChecked(True)
        self.chk_cross = QCheckBox("十字光标")
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

        self.lbl_cursor = QLabel("把鼠标移到图表上查看每根 K 线的明细")
        self.lbl_cursor.setObjectName("Cursor")
        lay.addWidget(self.lbl_cursor)

        # ---- 主图 ----
        axis_bottom = pg.DateAxisItem(orientation="bottom")   # 底部时间轴
        self.plot = pg.PlotWidget(axisItems={"bottom": axis_bottom})
        self.plot.setBackground(C_BG)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getAxis("left").setWidth(76)
        for name in ("left", "bottom"):
            ax = self.plot.getAxis(name)
            ax.setPen(pg.mkPen(C_BORDER))        # 坐标轴颜色
            ax.setTextPen(pg.mkPen(C_MUTED))     # 刻度文字颜色
        self.plot.getAxis("bottom").setStyle(showValues=False)  # 底部日期由副图显示
        self.plot.getViewBox().setDefaultPadding(0.03)

        self.item_candles = CandlestickItem()               # K 线蜡烛
        self.plot.addItem(self.item_candles)

        self.curve_close = pg.PlotDataItem(pen=pg.mkPen(C_ACCENT, width=2.2))  # 收盘价折线（对数坐标时替代蜡烛）
        self.curve_close.setDownsampling(auto=True, method="peak")  # 大数据量降采样
        self.curve_close.setClipToView(True)               # 只绘制可视区域
        self.plot.addItem(self.curve_close)

        self.curve_fit = pg.PlotDataItem(pen=pg.mkPen(C_FIT, width=2))   # 历史拟合曲线
        self.curve_fit.setClipToView(True)
        self.plot.addItem(self.curve_fit)

        self.curve_up = pg.PlotDataItem(pen=pg.mkPen(None))   # 置信带上界（我只管填充）
        self.curve_low = pg.PlotDataItem(pen=pg.mkPen(None))  # 置信带下界
        self.plot.addItem(self.curve_up)
        self.plot.addItem(self.curve_low)
        self.band = pg.FillBetweenItem(self.curve_up, self.curve_low,   # 上下界之间的填充
                                       brush=pg.mkBrush(*C_BAND))
        self.plot.addItem(self.band)

        self.curve_pred = pg.PlotDataItem(                     # 未来外推预测（虚线）
            pen=pg.mkPen(C_PRED, width=2.2, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.curve_pred)

        self.dot_pred = pg.ScatterPlotItem(size=12, brush=pg.mkBrush(C_PRED),  # 预测末端点
                                           pen=pg.mkPen(C_BG, width=2), symbol="o")
        self.plot.addItem(self.dot_pred)

        self.vline = pg.InfiniteLine(       # 十字光标：垂直参考线
            angle=90, movable=False,
            pen=pg.mkPen("#5d6b7d", width=1, style=Qt.PenStyle.DashLine))
        self.hline = pg.InfiniteLine(       # 十字光标：水平参考线
            angle=0, movable=False,
            pen=pg.mkPen("#5d6b7d", width=1, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)
        lay.addWidget(self.plot, 5)

        # ---- 成交量副图 ----
        self.vol_plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.vol_plot.setBackground(C_BG)
        self.vol_plot.setFixedHeight(132)
        self.vol_plot.getAxis("left").setWidth(76)
        self.vol_plot.getAxis("left").setLabel("成交量")
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

    # ---------------------------------------------------- 右侧 Tab
    def _build_side_tabs(self) -> QWidget:
        tabs = QTabWidget()
        tabs.setObjectName("SideTabs")
        tabs.setFixedWidth(408)
        tabs.setDocumentMode(True)
        tabs.addTab(self._wrap(self._tab_market()), "行情")
        tabs.addTab(self._wrap(self._tab_fit()), "预测")
        tabs.addTab(self._wrap(self._tab_stats()), "统计")
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

    # ---- Tab1 行情 ----
    def _tab_market(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("数据源")
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.cb_source = QComboBox()
        self.cb_source.addItems(["Binance", "OKX", "CoinGecko"])
        self.cb_source.setToolTip("默认走代理 127.0.0.1:10808，不通则自动回退直连")
        self.cb_tf = QComboBox()
        self.cb_tf.addItems([tf.label for tf in TIMEFRAMES])
        self.cb_tf.setCurrentIndex(2)
        row1.addWidget(self._field("数据源", self.cb_source), 1)
        row1.addWidget(self._field("周期", self.cb_tf), 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.ed_symbol = QLineEdit("BTCUSDT")
        self.btn_refresh = QPushButton("刷新数据")
        self.btn_refresh.setObjectName("Primary")
        self.btn_refresh.clicked.connect(self.refresh_data)
        row2.addWidget(self._field("币对", self.ed_symbol), 1)
        row2.addWidget(self.btn_refresh)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(10)
        self.btn_reconnect = QPushButton("重测网络")
        self.btn_reconnect.setObjectName("Ghost")
        self.btn_reconnect.setToolTip("清空已记住的代理策略，重新从 127.0.0.1:10808 开始探测")
        self.btn_reconnect.clicked.connect(self._reset_network)
        row3.addWidget(self.btn_reconnect)
        row3.addStretch(1)
        lay.addLayout(row3)
        v.addWidget(frame)

        frame2, lay2 = self._panel("关键指标")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.cards: Dict[str, QLabel] = {}
        quick_keys = [("last", "最新价"), ("change", "区间涨跌"),
                      ("vol", "年化波动"), ("bars", "样本根数")]
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

    # ---- Tab2 预测 ----
    def _tab_fit(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("多项式回归预测")
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
        lbl = QLabel("最高次 n")
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
        self.sp_horizon.setSuffix(" 期")
        self.sp_window = QSpinBox()
        self.sp_window.setRange(0, 20000)
        self.sp_window.setValue(0)
        self.sp_window.setSuffix(" 根")
        for w in (self.sp_horizon, self.sp_window):
            w.setMinimumWidth(120)
        grid.addWidget(QLabel("外推长度"), 0, 0)
        grid.addWidget(self.sp_horizon, 0, 1)
        grid.addWidget(QLabel("拟合窗口"), 1, 0)
        grid.addWidget(self.sp_window, 1, 1)
        lay.addLayout(grid)

        hint = QLabel("拟合窗口 0 = 使用全部历史数据")
        hint.setObjectName("Hint")
        lay.addWidget(hint)

        self.chk_log_fit = QCheckBox("对数空间拟合（BTC 长周期推荐）")
        self.chk_log_fit.setChecked(True)
        self.chk_auto = QCheckBox("参数变化时自动重新预测")
        self.chk_auto.setChecked(True)
        lay.addWidget(self.chk_log_fit)
        lay.addWidget(self.chk_auto)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_fit = QPushButton("运行多项式回归预测")
        self.btn_fit.setObjectName("Primary")
        self.btn_fit.clicked.connect(self.start_fit)
        self.btn_clear = QPushButton("清除")
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

        frame2, lay2 = self._panel("拟合质量")
        res_grid = QGridLayout()
        res_grid.setHorizontalSpacing(10)
        res_grid.setVerticalSpacing(8)
        self.res_labels: Dict[str, QLabel] = {}
        items = [("r2", "R² 拟合优度"), ("adj", "调整后 R²"),
                 ("rmse", "RMSE 均方根误差"), ("mae", "MAE 平均绝对误差"),
                 ("mape", "MAPE 平均绝对百分比"), ("sigma", "残差标准误 σ"),
                 ("samples", "参与拟合样本"), ("next", "下一期预测")]
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

    # ---- Tab3 统计 ----
    def _tab_stats(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("NumPy 关键统计")
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

        frame2, lay2 = self._panel("完整计算报告")
        self.txt_report = QPlainTextEdit()
        self.txt_report.setObjectName("Mono")
        self.txt_report.setReadOnly(True)
        self.txt_report.setMinimumHeight(340)
        self.txt_report.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay2.addWidget(self.txt_report)

        row = QHBoxLayout()
        row.setSpacing(10)
        btn_print = QPushButton("重新计算并打印")
        btn_print.setObjectName("Primary")
        btn_print.clicked.connect(self.recompute_report)
        btn_copy = QPushButton("复制报告")
        btn_copy.setObjectName("Ghost")
        btn_copy.clicked.connect(self._copy_report)
        row.addWidget(btn_print, 2)
        row.addWidget(btn_copy, 1)
        lay2.addLayout(row)
        v.addWidget(frame2)
        v.addStretch(1)
        return holder

    # ---- Tab4 NumPy 控制台 ----
    def _tab_numpy(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("SideHolder")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        frame, lay = self._panel("NumPy 表达式")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.ed_expr = QLineEdit("np.median(close)")
        self.ed_expr.setPlaceholderText("如 np.percentile(close,[25,50,75])")
        self.ed_expr.returnPressed.connect(self.run_expression)
        btn = QPushButton("运行")
        btn.setObjectName("Primary")
        btn.clicked.connect(self.run_expression)
        row.addWidget(self.ed_expr, 1)
        row.addWidget(btn)
        lay.addLayout(row)

        hint = QLabel("可用变量：np / close / open / high / low / volume / ret / logret / idx / n / ts")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        v.addWidget(frame)

        frame2, lay2 = self._panel("快捷计算")
        qgrid = QGridLayout()
        qgrid.setHorizontalSpacing(8)
        qgrid.setVerticalSpacing(8)
        for i, expr in enumerate(QUICK_EXPRS):
            b = QPushButton(expr)
            b.setObjectName("Chip")
            b.setToolTip("点击直接运行")
            b.clicked.connect(lambda _=False, e=expr: self._use_expr(e))
            qgrid.addWidget(b, i // 2, i % 2)
        lay2.addLayout(qgrid)
        v.addWidget(frame2)

        frame3, lay3 = self._panel("输出")
        self.txt_expr = QPlainTextEdit()
        self.txt_expr.setObjectName("Mono")
        self.txt_expr.setReadOnly(True)
        self.txt_expr.setMinimumHeight(260)
        lay3.addWidget(self.txt_expr)
        btn_clear = QPushButton("清空输出")
        btn_clear.setObjectName("Ghost")
        btn_clear.clicked.connect(self.txt_expr.clear)
        lay3.addWidget(btn_clear)
        v.addWidget(frame3)
        v.addStretch(1)
        return holder

    # ================================================================ 样式
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

    # ================================================================ 线程
    def _run_worker(self, worker: QObject, ok_slot, err_slot):
        # 注意：worker 必须被 Python 持有强引用，否则会在 _run_worker 返回后
        # 被垃圾回收，thread.started 连接随之失效，worker.run 永远不会执行。
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
        # 线程已结束，此时才可以安全释放 worker / thread 的引用。
        if worker in self._workers:
            self._workers.remove(worker)
        if thread in self._threads:
            self._threads.remove(thread)
        worker.deleteLater()
        thread.deleteLater()

    def _set_loading(self, on: bool, text: str = ""):
        """切换到忙 / 闲状态：忙碌时禁用会引发重复请求的控件。"""
        self._loading = on
        self.bar_busy.setVisible(on)
        for w in (self.btn_refresh, self.cb_source, self.cb_tf, self.ed_symbol):
            w.setEnabled(not on)
        if text:
            self.lbl_status.setText(text)

    def _refresh_net_label(self):
        """右下角网络状态：优先显示已记住的策略，失败则提示。"""
        name = HTTP.preferred_name()
        if name:
            self.lbl_net.setText(f"网络：{name}")
        elif HTTP.last_error:
            self.lbl_net.setText("网络：全部策略失败")
        else:
            self.lbl_net.setText("网络：未连接")

    # ============================================================ 数据
    def _reset_network(self):
        HTTP.reset()
        self._refresh_net_label()
        self.lbl_status.setText("已重置网络策略，下次请求将从代理 127.0.0.1:10808 重新探测")

    def _on_tf_changed(self, index: int):
        self.tf = TIMEFRAMES[index]
        self.lbl_title.setText(f"BTC · {self.tf.label}")
        self.refresh_data()

    def refresh_data(self):
        """启动 / 换周期 / 切换数据源 / 点刷新按钮时调用：后台线程拉取 K 线。"""
        if self._loading:
            return                            # 已有请求在途，忽略重复点击
        self._data_rid += 1
        rid = self._data_rid
        source = self.cb_source.currentText()
        symbol = self.ed_symbol.text().strip() or "BTCUSDT"
        tf = self.tf
        self.lbl_net.setText("网络：连接中…")
        self._set_loading(True, f"正在从 {source} 拉取 {tf.label} 数据（代理优先 127.0.0.1:10808）…")
        self._run_worker(DataWorker(rid, source, symbol, tf),
                         self._on_data_ok, self._on_data_err)

    def _on_data_ok(self, rid: int, candles):
        if rid != self._data_rid:
            return                            # 已被更新的请求取代，丢弃
        self._set_loading(False)
        self._refresh_net_label()
        self.candles = candles
        self._xs = np.asarray([c.ts / 1000.0 for c in candles], dtype=float)  # 秒级时间用于光标

        self.item_candles.setData([(c.ts / 1000.0, c.open, c.close, c.low, c.high)
                                   for c in candles])   # 刷新蜡烛图
        self.item_volume.setData([(c.ts / 1000.0, max(c.volume, 0.0), c.close >= c.open)
                                  for c in candles])   # 刷新成交量

        close = np.asarray([c.close for c in candles], dtype=float)
        self.curve_close.setData(self._xs, close, fillLevel=float(close.min()) * 0.97,
                                 brush=pg.mkBrush(247, 147, 26, 26))  # 橙色面积

        self.clear_forecast(keep_status=True)  # 新数据来了，旧预测作废
        self._sync_view()
        self._update_quick_stats()
        self.vol_plot.autoRange(padding=0.02)
        self.plot.autoRange(padding=0.03)

        st = datetime.fromtimestamp(candles[0].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        en = datetime.fromtimestamp(candles[-1].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        self.lbl_status.setText(
            f"{self.cb_source.currentText()} · {self.tf.label} · {len(candles)} 根 · {st} → {en}")

        self.recompute_report()               # 重新算统计卡片 + 控制台报告

        if self.chk_auto.isChecked():
            self._auto_timer.start()          # 自动重跑一次拟合

    def _on_data_err(self, rid: int, msg: str):
        if rid != self._data_rid:
            return
        self._set_loading(False)
        self._refresh_net_label()
        log(f"数据获取失败：{msg}")
        self.lbl_status.setText(f"⚠ 数据获取失败：{msg}")
        self.lbl_cursor.setText(f"⚠ 数据获取失败：{msg}")

    def _update_quick_stats(self):
        """行情 Tab 顶部的四个小卡片：最新价 / 区间涨跌 / 年化波动 / 根数。"""
        close = np.asarray([c.close for c in self.candles], dtype=float)
        lr = np.diff(np.log(np.maximum(close, 1e-9)))
        total = float(close[-1] / close[0] - 1) * 100                # 区间涨跌 %
        vol = (float(np.std(lr, ddof=1)) * math.sqrt(self.tf.periods_per_year) * 100
               if lr.size > 1 else 0.0)                              # 年化波动率 %
        self.cards["last"].setText(f"${close[-1]:,.2f}")
        self.cards["change"].setText(f"{total:+.2f}%")
        self.cards["change"].setStyleSheet(
            f"color:{C_UP if total >= 0 else C_DOWN}; font-size:17px; font-weight:700;")
        self.cards["vol"].setText(f"{vol:,.1f}%")
        self.cards["bars"].setText(str(len(self.candles)))
        s = datetime.fromtimestamp(self.candles[0].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        e = datetime.fromtimestamp(self.candles[-1].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        self.lbl_range.setText(f"区间 {s} → {e}　中位价 "
                               f"${float(np.median(close)):,.2f}　均价 ${float(np.mean(close)):,.2f}")

    # ============================================================ 统计报告
    def recompute_report(self):
        """重算全部统计指标：填卡片 + 刷新控制台报告文本。"""
        if not self.candles:
            self.lbl_status.setText("请先刷新数据")
            return
        log_rule(f"重新计算统计 · {self.cb_source.currentText()} · {self.tf.label}")
        self._report = build_report(self.candles, self.tf,
                                    self.cb_source.currentText(),
                                    self.ed_symbol.text().strip() or "BTCUSDT")
        self.txt_report.setPlainText(self._report.text)      # 报告 Tab 全文
        for key, (value, color) in self._report.cards.items():   # 行情 Tab 顶部的统计卡片
            lbl = self.stat_cards.get(key)
            if lbl is None:
                continue
            lbl.setText(value)
            if color:
                lbl.setStyleSheet(f"color:{color}; font-size:17px; font-weight:700;")
            else:
                lbl.setStyleSheet("")
        self.lbl_status.setText("统计完成，结果已打印到控制台")

    def _copy_report(self):
        if self._report.text:
            QApplication.clipboard().setText(self._report.text)
            self.lbl_status.setText("报告已复制到剪贴板")

    # ============================================================ 拟合
    def _on_degree_changed(self, value: int):
        self.lbl_formula.setText(_render_degree_formula(value))
        self._schedule_auto_fit()

    def _schedule_auto_fit(self, *args):
        if self.chk_auto.isChecked() and self.candles:
            self._auto_timer.start()

    def start_fit(self):
        """按当前参数（阶数 / 外推期数 / 窗口）启动后台多项式拟合。"""
        if not self.candles:
            self.lbl_status.setText("请先刷新数据")
            return
        self._fit_rid += 1
        rid = self._fit_rid
        self.lbl_status.setText(f"正在拟合 {self.sp_degree.value()} 阶多项式…")
        log_rule(f"多项式回归 · n={self.sp_degree.value()} · 外推 {self.sp_horizon.value()} 期 "
                 f"· 窗口 {self.sp_window.value() or '全部'} · "
                 f"{'对数空间' if self.chk_log_fit.isChecked() else '线性空间'}")
        self._run_worker(
            FitWorker(rid, self.candles, self.sp_degree.value(), self.sp_horizon.value(),
                      self.sp_window.value(), self.chk_log_fit.isChecked()),
            self._on_fit_ok, self._on_fit_err)

    def _on_fit_ok(self, rid: int, r: FitResult):
        if rid != self._fit_rid:
            return                                  # 已有更新的拟合，丢弃旧结果
        self._last_fit = r

        anchor_x, anchor_y = float(r.ts_hist[-1]), float(r.y_fit[-1])  # 以最后一点为预测起点
        self.curve_fit.setData(r.ts_hist, r.y_fit)

        if r.ts_future.size:
            # 预测段：把“锚点 + 未来外推”拼接成一条连续曲线
            xs = np.concatenate([[anchor_x], r.ts_future])
            ys = np.concatenate([[anchor_y], r.y_pred])
            self.curve_pred.setData(xs, ys)
            self.curve_up.setData(xs, np.concatenate([[anchor_y], r.upper]))
            self.curve_low.setData(xs, np.concatenate([[anchor_y], r.lower]))
            self.dot_pred.setData([xs[-1]], [ys[-1]])   # 末端标记点
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

        # ---- 控制台打印拟合细节 ----
        space = "对数空间 ln(y)" if r.log_space else "线性空间"
        print(f"\n【多项式回归结果】n={r.degree}  空间={space}  样本={r.n_points}"
              f"（窗口={r.window}）  外推={r.ts_future.size} 期")
        print(f"  系数(从高次到常数项): "
              + ", ".join(f"{c:.6g}" for c in r.coef))
        print(f"  R²={r.r2:.6f}   调整R²={r.adj_r2:.6f}   RMSE={r.rmse:,.4f}   "
              f"MAE={r.mae:,.4f}   MAPE={r.mape:.4f}%   σ={r.sigma:,.4f}")
        if r.ts_future.size:
            step_days = (float(np.median(np.diff(r.ts_future))) / 86400.0
                         if r.ts_future.size > 1 else 0.0)
            print(f"  下一期预测 {r.next_value:,.2f}  ({r.next_change_pct:+.2f}%)"
                  + (f"  步长≈{step_days:.2f} 天" if step_days else ""))
            print(f"  末期预测 {float(r.y_pred[-1]):,.2f}"
                  f"   （相对现价 {(float(r.y_pred[-1]) / float(r.y_fit[-1]) - 1) * 100:+.2f}%）")
            print(f"  置信带 ±1.96σ: [{float(r.lower[-1]):,.2f}, {float(r.upper[-1]):,.2f}]")
        print("─" * 70, flush=True)

        self.lbl_status.setText(
            f"拟合完成 · n={r.degree} · {space} · {r.n_points} 样本 · R²={r.r2:.4f} · "
            f"下一期 {r.next_value:,.2f} ({r.next_change_pct:+.2f}%)")

    def _on_fit_err(self, rid: int, msg: str):
        if rid != self._fit_rid:
            return
        log(f"拟合失败：{msg}")
        self.lbl_status.setText(f"⚠ 拟合失败：{msg}")

    def clear_forecast(self, keep_status: bool = False):
        """清空图表上的拟合 / 预测曲线；keep_status=True 时不覆盖状态栏。"""
        self._last_fit = None
        self._fit_rid += 1                                # 使在途拟合结果失效
        for item in (self.curve_fit, self.curve_pred, self.curve_up, self.curve_low):
            item.setData([], [])
        self.dot_pred.setData([], [])
        self._set_overlay_visible(False)
        for lbl in self.res_labels.values():
            lbl.setText("—")
        if not keep_status:
            self.lbl_status.setText("已清除预测曲线")
            log("已清除预测曲线")

    def _set_overlay_visible(self, on: bool):
        """统一开关拟合 / 预测相关的所有图形项。"""
        items = [self.curve_fit, self.curve_pred, self.curve_up,
                 self.curve_low, self.dot_pred]
        if self.band is not None:
            items.append(self.band)
        for item in items:
            item.setVisible(on)

    def _update_fit_labels(self, r: FitResult):
        """把拟合结果填到结果面板的各个指标上。"""
        self.res_labels["r2"].setText(f"{r.r2:.4f}")
        self.res_labels["adj"].setText(f"{r.adj_r2:.4f}")
        self.res_labels["rmse"].setText(f"{r.rmse:,.2f}")
        self.res_labels["mae"].setText(f"{r.mae:,.2f}")
        self.res_labels["mape"].setText(f"{r.mape:.2f}%")
        self.res_labels["sigma"].setText(f"{r.sigma:,.2f}")
        self.res_labels["samples"].setText(f"{r.n_points}")
        color = C_UP if r.next_change_pct >= 0 else C_DOWN   # 预测正负分色
        self.res_labels["next"].setText(
            f'<span style="color:{color}">{r.next_value:,.2f} '
            f'({r.next_change_pct:+.2f}%)</span>')

    # ============================================================ NumPy 控制台
    def _numpy_env(self) -> Dict[str, object]:
        """构建表达式求值的沙箱环境：把数据按变量名暴露给用户表达式。"""
        close = np.asarray([c.close for c in self.candles], dtype=float)
        env = {
            "np": np, "numpy": np,
            "close": close,
            "open": np.asarray([c.open for c in self.candles], dtype=float),
            "high": np.asarray([c.high for c in self.candles], dtype=float),
            "low": np.asarray([c.low for c in self.candles], dtype=float),
            "volume": np.asarray([c.volume for c in self.candles], dtype=float),
            "ts": np.asarray([c.ts for c in self.candles], dtype=float) / 1000.0,  # 秒级时间
            "ret": np.diff(close) / np.maximum(close[:-1], 1e-9),   # 简单收益率
            "logret": np.diff(np.log(np.maximum(close, 1e-9))),      # 对数收益率
            "idx": np.arange(close.size, dtype=float),
            "n": int(close.size),
            "tf": self.tf.key,                                       # 当前周期 key
            "periods_per_year": float(self.tf.periods_per_year),     # 年化期数
        }
        return env

    def _use_expr(self, expr: str):
        self.ed_expr.setText(expr)
        self.run_expression()

    def run_expression(self):
        """把当前输入框表达式放到后台线程求值（受控沙箱）。"""
        expr = self.ed_expr.text().strip()
        if not expr:
            return
        if not self.candles:
            self.lbl_status.setText("请先刷新数据")
            return
        self._expr_rid += 1
        self._run_worker(ExprWorker(self._expr_rid, expr, self._numpy_env()),
                         self._on_expr_ok, self._on_expr_err)

    def _on_expr_ok(self, rid: int, expr: str, result: str):
        if rid != self._expr_rid:
            return
        log_rule("NumPy 表达式求值")
        print(f"  >>> {expr}\n  {result}", flush=True)
        self.txt_expr.appendPlainText(f">>> {expr}\n{result}\n")     # 同时追加到控件台界面
        self.lbl_status.setText("表达式求值完成（结果已打印到控制台）")

    def _on_expr_err(self, rid: int, expr: str, msg: str):
        if rid != self._expr_rid:
            return
        log(f"表达式错误 {expr} → {msg}")
        self.txt_expr.appendPlainText(f">>> {expr}\n✗ {msg}\n")

    # ============================================================ 视图
    def _sync_view(self):
        """根据勾选状态切换蜡烛图 / 收盘价折线，并控制十字光标显隐。"""
        candle_on = self.chk_candle.isChecked() and not self.chk_log_y.isChecked()
        self.item_candles.setVisible(candle_on)        # 蜡烛（对数坐标下不画蜡烛）
        self.curve_close.setVisible(not candle_on)     # 折线作为替代
        self.vline.setVisible(self.chk_cross.isChecked())
        self.hline.setVisible(self.chk_cross.isChecked())
        if not candle_on and self.candles:
            close = np.asarray([c.close for c in self.candles], dtype=float)
            self.curve_close.setData(self._xs, close,
                                     fillLevel=max(float(close.min()) * 0.97, 1e-9),
                                     brush=pg.mkBrush(247, 147, 26, 26))

    def _on_log_y_changed(self, on: bool):
        """切换 Y 轴对数模式：对数坐标下蜡烛图被折线替代。"""
        self.chk_candle.setEnabled(not on)
        if on:
            self.chk_candle.setChecked(False)
        self.plot.setLogMode(False, on)
        self._sync_view()
        if self.candles:
            self.plot.autoRange(padding=0.05)

    def _on_mouse_move(self, evt):
        """鼠标悬停时定位到最近一根 K 线并显示明细。"""
        if not self.candles or not self.chk_cross.isChecked():
            return
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        x = float(self.plot.getViewBox().mapSceneToView(pos).x())   # 场景坐标 → 数据坐标
        i = int(np.clip(np.searchsorted(self._xs, x), 0, len(self._xs) - 1))  # 二分找最近下标
        if i > 0 and abs(self._xs[i - 1] - x) < abs(self._xs[i] - x):
            i -= 1                                                  # 再比较相邻，取更近者
        c = self.candles[i]
        dt = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        fmt = "%Y-%m-%d %H:%M" if self.tf.key in ("1h", "4h") else "%Y-%m-%d"
        chg = (c.close / c.open - 1) * 100 if c.open else 0.0       # 当根涨跌幅
        col = C_UP if chg >= 0 else C_DOWN
        self.vline.setPos(self._xs[i])
        self.hline.setPos(c.close)
        self.lbl_cursor.setText(
            f'<b>{dt.strftime(fmt)}</b>　开 <b>{c.open:,.2f}</b>　高 <b>{c.high:,.2f}</b>　'
            f'低 <b>{c.low:,.2f}</b>　收 <b>{c.close:,.2f}</b>　'
            f'<span style="color:{col}">{chg:+.2f}%</span>　量 {c.volume:,.0f}')

    # ============================================================ 关闭
    def closeEvent(self, event):
        """退出前停止所有后台线程，避免残留 QThread 拖住进程。"""
        for t in list(self._threads):
            t.quit()
            t.wait(2000)
        self._threads.clear()
        self._workers.clear()
        super().closeEvent(event)


# ============================================================================
# 10. 入口
# ============================================================================
def main() -> int:
    """程序入口：设置编码容错 → 建应用 → 弹主窗口。"""
    # Windows 控制台默认 GBK，打印 “²”/“³” 等字符会抛 UnicodeEncodeError。
    # 不改编码（保持中文正常显示），只把无法编码的字符替换为 ? 而不是崩溃。
    # getattr 是为了兼容 IDLE 等没有 reconfigure 的伪 stdout。
    getattr(sys.stdout, "reconfigure", lambda **_: None)(errors="replace")
    getattr(sys.stderr, "reconfigure", lambda **_: None)(errors="replace")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(FONT_STACK)
    font.setPointSize(10)
    app.setFont(font)

    print("=" * 74)
    print("  BTC 行情实验室 v2.2 · PyQt6 + pyqtgraph + NumPy")
    print(f"  pyqtgraph 版本: {getattr(pg, '__version__', '未知')}")
    print("  网络策略顺序: HTTP 10808 → HTTP 10809 → SOCKS5 10808 → 直连")
    print("  所有统计与表达式结果都会打印到此控制台")
    print("=" * 74, flush=True)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
