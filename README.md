# 📈 BTC Market Lab · BTC 行情实验室 · BTC マーケットラボ

> 用 PyQt6 + pyqtgraph + NumPy 打造的桌面版比特币 **统计 · 预测 · 回测**实验室（v2.2）。
> A desktop Bitcoin **statistics · forecast · backtesting** lab built with PyQt6 + pyqtgraph + NumPy.
> PyQt6 + pyqtgraph + NumPy で作られたビットコインの**統計分析 · 予測 · バックテスト**ツール（v2.2）。

## 📂 结构 · Structure · 構成

每个语言版本**完全分离**在独立目录中，各目录内只含本语言内容。

```
项目4/
├── README.md          ← 本索引 · Index
├── zh/                ← 🇨🇳 中文版（全部中文）
│   ├── 预测工具.py
│   └── README.md
├── en/                ← 🇬🇧 English version（pure English，零中文）
│   ├── BTC_Market_Lab.py
│   └── README.md
└── jp/                ← 🇯🇵 日本語（全部日文）
    ├── BTCマーケットラボ.py
    └── README.md
```

## 🌍 文档入口 · Documentation · ドキュメント

| 语言 | 目录 / 入口 | 说明 |
|:---:|:---|:---|
| 🇨🇳 简体中文 | [zh/README.md](zh/README.md) | 中文版完整文档 |
| 🇬🇧 English | [en/README.md](en/README.md) | English documentation |
| 🇯🇵 日本語 | [jp/README.md](jp/README.md) | 日本語ドキュメント |

## 🚀 启动 · Run

```bash
cd zh && python 预测工具.py          # 🇨🇳 中文版（Chinese UI）
cd en && python BTC_Market_Lab.py   # 🇬🇧 English version
cd jp && python BTCマーケットラボ.py # 🇯🇵 日本語版（Japanese UI）
```

> ✅ 三个语言版本同源：功能与数值完全一致，仅界面语言不同。

> [!CAUTION]
> **仅供学习与研究使用，不构成投资建议。** 加密货币市场波动极大，请自行承担风险。
> **For education and research only — not financial advice.**
> **教育・研究目的のみ。投資助言ではありません。**