#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zen GUI 启动入口：导入编译好的 zen_gui_probe_only.pyd 并启动主窗口。"""

import sys

import zen_gui_probe_only

if __name__ == "__main__":
    sys.exit(zen_gui_probe_only.main())
