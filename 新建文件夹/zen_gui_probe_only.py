#!/usr/bin/env python3
# -*- coding: utf-8 -*-
print("加油!")
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import requests
from typing import List, Dict, Any, Optional, Iterator

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QEvent
from PyQt6.QtGui import QFont, QFontMetrics, QColor, QPalette, QTextCursor, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QScrollArea,
    QFrame,
    QLabel,
    QTextEdit,
    QSizePolicy,
    QSpacerItem,
    QPushButton,
    QMenu,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_URL = "https://opencode.ai/zen/v1/chat/completions"
DEFAULT_MODEL = "big-pickle"
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
MASK_48 = (1 << 48) - 1

FREE_MODELS = [
    "big-pickle",
    "exo-free",
    "space-bunny-free",
    "jev-1.13-free",
    "deepseek-v4-flash-free",
    "muse-spark-1.3-contributor-free",
    "muse-spark-1.2-contributor-free",
    "mimo-v2.6-flash-free",
    "longcat-2.5-preview-free",
    "mimo-v2.5-free",
    "ling-3.0-flash-fin-free",
    "ling-3.1-flash-fin-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
    "fledge-alpha-free",
    "ling-3.1-flash-free",
]


def mint_id(prefix: str, descending: bool, timestamp_ms: int, counter: int, rand_part: str) -> str:
    n = timestamp_ms * 0x1000 + counter
    n = ((~n) & MASK_48) if descending else (n & MASK_48)
    time_hex = format(n, "012x")[-12:]
    return f"{prefix}_{time_hex}{rand_part}"


def client_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    ts = int(time.time() * 1000)
    rand = lambda: "".join(random.choices(BASE62, k=14))
    return {
        "Content-Type": "application/json",
        "User-Agent": "opencode/1.18.34",
        "Authorization": "Bearer " + (api_key if api_key else "public"),
        "x-opencode-client": "cli",
        "x-opencode-project": "global",
        "x-opencode-session": mint_id("ses", True, ts, 1, rand()),
        "x-opencode-request": mint_id("msg", False, ts, 2, rand()),
    }


def placeholder_tool(name: str) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} is not available in this minimal client. It must still be declared for Zen free-tier validation.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def build_body(model: str, messages: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": messages,
        "stream": True,
        "tools": [placeholder_tool("bash"), placeholder_tool("read")],
    }


PROXY_CANDIDATES = (
    "socks5://127.0.0.1:9150",
    "http://127.0.0.1:10808",
)


def _probe(proxy: str, timeout: float = 8.0) -> bool:
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get("https://opencode.ai", proxies=proxies, timeout=timeout, allow_redirects=True)
        return r.status_code < 600
    except Exception:
        return False


def detect_proxy() -> str:
    for proxy in PROXY_CANDIDATES:
        if _probe(proxy):
            return proxy
    return ""


def stream_chat(
    model: str,
    messages: List[Dict[str, str]],
    api_key: Optional[str] = None,
    timeout: int = 180,
    proxy: str = "",
) -> Iterator[str]:
    body = json.dumps(build_body(model, messages)).encode("utf-8")
    headers = client_headers(api_key)
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    try:
        resp = requests.post(
            BASE_URL,
            data=body,
            headers=headers,
            proxies=proxies,
            stream=True,
            timeout=timeout,
        )
    except Exception as e:
        raise RuntimeError(f"网络错误: {e}")
    if resp.status_code != 200:
        try:
            detail = resp.text[:800]
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
    for raw in resp.iter_lines(decode_unicode=False):
        if not raw:
            continue
        try:
            line = raw.decode("utf-8", "replace").strip()
        except Exception:
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if chunk.get("type") == "error":
            raise RuntimeError(str(chunk))
        err = chunk.get("error")
        if err:
            raise RuntimeError(str(err))
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content


class ChatBubble(QFrame):
    def __init__(self, text: str, is_user: bool, parent=None):
        super(ChatBubble, self).__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        font = QFont()
        font.setPointSize(10)
        self.label.setFont(font)

        if is_user:
            self.label.setStyleSheet(
                """
                QLabel {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                                stop:0 #ff5f2e, stop:1 #ff3b1f);
                    color: #ffffff;
                    border-radius: 12px;
                    padding: 8px 10px;
                }
                """
            )
            lay.addSpacerItem(QSpacerItem(40, 1, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))
            lay.addWidget(self.label)
        else:
            self.label.setStyleSheet(
                """
                QLabel {
                    background: #2a2a20;
                    color: #f5f5f0;
                    border-radius: 12px;
                    padding: 8px 10px;
                }
                """
            )
            lay.addWidget(self.label)
            lay.addSpacerItem(QSpacerItem(40, 1, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        self.setStyleSheet("ChatBubble { background: transparent; }")
        self.label.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self.label and ev.type() == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            QApplication.clipboard().setText(self.label.text())
            return True
        return super(ChatBubble, self).eventFilter(obj, ev)


class StreamWorker(QThread):
    token = pyqtSignal(str)
    done = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, model: str, messages: List[Dict[str, str]], api_key: Optional[str],                              proxy: str, timeout: int = 180):
        super(StreamWorker, self).__init__()
        self.model = model
        self.messages = messages
        self.api_key = api_key
        self.proxy = proxy
        self.timeout = timeout
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        try:
            full = []
            for t in stream_chat(self.model, self.messages, self.api_key, self.timeout, self.proxy):
                if self._stopped:
                    break
                full.append(t)
                self.token.emit(t)
            if not self._stopped:
                self.done.emit("".join(full))
        except Exception as e:
            self.error.emit(str(e))


class ModelProbe(QThread):
    finished = pyqtSignal(list)

    def __init__(self, api_key=None, proxy="", timeout=15):
        super(ModelProbe, self).__init__()
        self.api_key = api_key
        self.proxy = proxy
        self.timeout = timeout
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        available = []
        probe_msgs = [{"role": "user", "content": "hi"}]
        print(f"[探测] 开始，共 {len(FREE_MODELS)} 个模型，代理: {self.proxy or '直连'}", flush=True)
        for i, m in enumerate(FREE_MODELS, 1):
            if self._stopped:
                print("[探测] 已中止", flush=True)
                break
            ok = False
            err = ""
            try:
                for _ in stream_chat(m, probe_msgs, self.api_key, self.timeout, self.proxy):
                    ok = True
                    break
            except Exception as e:
                err = str(e)
            if ok:
                available.append(m)
                print(f"[探测] {i}/{len(FREE_MODELS)} {m}: 可用", flush=True)
            else:
                print(f"[探测] {i}/{len(FREE_MODELS)} {m}: 不可用 {err[:80]}", flush=True)
        if not self._stopped:
            print(f"[探测] 完成，可用 {len(available)} 个: {', '.join(available) or '无'}", flush=True)
            self.finished.emit(available)


class InputEdit(QTextEdit):
    def __init__(self, parent=None):
        super(InputEdit, self).__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(30)
        self.setMaximumHeight(140)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAcceptRichText(False)

        font = QFont()
        font.setPointSize(10)
        self.setFont(font)

        self.setStyleSheet(
            """
            QTextEdit {
                background: #262626;
                color: #f5f5f5;
                border: 1px solid #ff4f1f;
                border-radius: 8px;
                padding: 4px 6px;
            }
            QTextEdit:focus {
                border: 1px solid #ff7a45;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
            }
            QScrollBar::handle:vertical {
                background: #ff5f2e;
                border-radius: 3px;
            }
            """
        )

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() == Qt.Key.Key_Return and e.modifiers() == Qt.KeyboardModifier.ControlModifier:
            win = self.window()
            if hasattr(win, "send_requested"):
                win.send_requested()
            e.accept()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and e.modifiers() == Qt.KeyboardModifier.NoModifier:
            cursor = self.textCursor()
            cursor.insertText("\n")
            e.accept()
            return
        super(InputEdit, self).keyPressEvent(e)


class MainWin(QMainWindow):
    def __init__(self):
        super(MainWin, self).__init__()
        self.setWindowTitle("Zen GUI")
        self.resize(880, 680)

        self.api_key = os.environ.get("OPENCODE_API_KEY") or None
        self.proxy = detect_proxy()
        self.model = DEFAULT_MODEL
        self.available_models = list(FREE_MODELS)
        self.messages: List[Dict[str, str]] = []
        self.worker: Optional[StreamWorker] = None
        self.sending = False
        self.current_ai_bubble = None
        self.current_ai_text = ""
        self.font_size = 10

        QApplication.instance().installEventFilter(self)

        self._setup_theme()
        self._setup_ui()
        self._autosize_input()
        self.model_probe = ModelProbe(self.api_key, self.proxy)
        self.model_probe.finished.connect(self._on_model_probed)
        self.model_probe.start()

    def _setup_theme(self):
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor(20, 20, 20))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(245, 245, 245))
        pal.setColor(QPalette.ColorRole.Base, QColor(20, 20, 20))
        pal.setColor(QPalette.ColorRole.Text, QColor(245, 245, 245))
        self.setPalette(pal)
        self.setStyleSheet(
            """
            QMainWindow {
                background: #141414;
            }
            QScrollArea {
                background: #141414;
                border: none;
            }
            #chatContainer {
                background: #141414;
            }
            """
        )

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 6)
        root.setSpacing(6)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().setStyleSheet(
            """
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
            }
            QScrollBar::handle:vertical {
                background: #ff5f2e;
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            """
        )

        self.chat_wrap = QWidget()
        self.chat_wrap.setObjectName("chatContainer")
        self.chat_lay = QVBoxLayout(self.chat_wrap)
        self.chat_lay.setContentsMargins(6, 6, 6, 6)
        self.chat_lay.setSpacing(10)
        self.chat_lay.addStretch(1)

        self.scroll.setWidget(self.chat_wrap)
        self.chat_wrap.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_wrap.customContextMenuRequested.connect(self._chat_menu)
        root.addWidget(self.scroll)

        bottom = QFrame()
        bottom.setFrameShape(QFrame.Shape.NoFrame)
        bottom_lay = QHBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        bottom_lay.setSpacing(6)

        self.input = InputEdit(self)
        self.input.textChanged.connect(self._autosize_input)
        bottom_lay.addWidget(self.input)

        self.btn_model = QPushButton("模型")
        self.btn_model.setFixedHeight(30)
        self.btn_model.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.btn_model.setStyleSheet(
            """
            QPushButton {
                background: #ff5f2e;
                color: #fff;
                border: none;
                border-radius: 8px;
                padding: 0 12px;
            }
            QPushButton:hover { background: #ff7a45; }
            """
        )
        self.btn_model.clicked.connect(self._pick_model)
        bottom_lay.addWidget(self.btn_model, 0, Qt.AlignmentFlag.AlignBottom)

        bottom.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        root.addWidget(bottom)
        root.setStretch(0, 1)
        root.setStretch(1, 0)

    def _autosize_input(self):
        doc = self.input.document()
        h = doc.size().height() + 14
        h = max(30, min(h, 140))
        self.input.setFixedHeight(int(h))

    def _pick_model(self, checked: bool = False):
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background: #262626;
                color: #f5f5f5;
                border: 1px solid #ff4f1f;
                padding: 4px;
            }
            QMenu::item { padding: 4px 16px; }
            QMenu::item:selected { background: #ff5f2e; color: #fff; }
            """
        )
        for m in self.available_models:
            act = menu.addAction(m)
            act.setData(m)
        act = menu.exec(self.btn_model.mapToGlobal(self.btn_model.rect().topLeft()))
        if act:
            self.model = act.data()

    def _on_model_probed(self, available):
        # 不再自动把不可用模型从列表中删除，仅打印可用情况
        print(f"[探测] 可用模型 ({len(available)}): {', '.join(available) or '无'}", flush=True)
        # self.available_models = available
        # if self.available_models:
        #     if self.model not in self.available_models:
        #         self.model = self.available_models[0]
        #     self._append_info(f"模型探测完成，可用 {len(available)} 个")
        # else:
        #     self._append_info("模型探测完成，暂未检测到可用模型")

    def _chat_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background: #262626;
                color: #f5f5f5;
                border: 1px solid #ff4f1f;
                padding: 4px;
            }
            QMenu::item { padding: 4px 16px; }
            QMenu::item:selected { background: #ff5f2e; color: #fff; }
            """
        )
        act = menu.addAction("导出全部聊天记录 (JSON)")
        a = menu.exec(self.chat_wrap.mapToGlobal(pos))
        if a == act:
            self._export_chat()

    def _export_chat(self):
        from datetime import datetime
        ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")
        data = {
            "model": self.model,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "proxy": self.proxy,
            "messages": self.messages,
        }
        fname = f"chat_{ts_file}.json"
        try:
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _append_info(self, text: str):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #9b9b9b; padding: 2px 6px;")
        font = QFont()
        font.setPointSize(9)
        lbl.setFont(font)
        self.chat_lay.insertWidget(self.chat_lay.count() - 1, lbl)
        self._scroll_to_end()

    def _append_bubble(self, text: str, is_user: bool) -> ChatBubble:
        b = ChatBubble(text, is_user)
        self.chat_lay.insertWidget(self.chat_lay.count() - 1, b)
        self._scroll_to_end()
        return b

    def _scroll_to_end(self):
        QThread.msleep(10)
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def send_requested(self):
        if self.sending:
            return
        text = self.input.toPlainText().rstrip()
        if not text.strip():
            return
        self.input.clear()
        self._autosize_input()
        self._append_bubble(text, True)
        self.messages.append({"role": "user", "content": text})
        self._start_stream()

    def _start_stream(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1000)
        self.sending = True
        self.current_ai_text = ""
        self.current_ai_bubble = self._append_bubble("", False)
        self.worker = StreamWorker(self.model, self.messages, self.api_key, self.proxy)
        self.worker.token.connect(self._on_token)
        self.worker.done.connect(self._on_done)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_token(self, t: str):
        self.current_ai_text += t
        if self.current_ai_bubble:
            self.current_ai_bubble.label.setText(self.current_ai_text)
            self._scroll_to_end()

    def _on_done(self, full: str):
        self.sending = False
        self.messages.append({"role": "assistant", "content": full})
        self.worker = None

    def _on_error(self, err: str):
        self.sending = False
        if self.current_ai_bubble:
            self.current_ai_bubble.label.setText(self.current_ai_text + f"\n\n[错误] {err}")
            self.current_ai_bubble.label.setStyleSheet(
                self.current_ai_bubble.label.styleSheet() + " color: #ff9f85;"
            )
        if self.messages and self.messages[-1].get("role") == "user":
            try:
                self.messages.pop()
            except Exception:
                pass
        self.worker = None
        self._scroll_to_end()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.KeyPress and (ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
            t = ev.text()
            if t in ("+", "="):
                self._adjust_font_size(1)
                return True
            if t in ("-", "_"):
                self._adjust_font_size(-1)
                return True
        return super(MainWin, self).eventFilter(obj, ev)

    def _adjust_font_size(self, delta: int):
        self.font_size = max(8, min(24, self.font_size + delta))
        self._apply_font_size()

    def _apply_font_size(self):
        def _set(w):
            f = w.font()
            f.setPointSize(self.font_size)
            w.setFont(f)

        _set(self.input)
        _set(self.btn_model)
        for i in range(self.chat_lay.count()):
            w = self.chat_lay.itemAt(i).widget()
            if w is None:
                continue
            if isinstance(w, ChatBubble):
                _set(w.label)
            elif isinstance(w, QLabel):
                _set(w)

    def closeEvent(self, e):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        if hasattr(self, "model_probe") and self.model_probe and self.model_probe.isRunning():
            self.model_probe.stop()
            self.model_probe.wait(2000)
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Zen GUI")
    w = MainWin()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
