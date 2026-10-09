#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zen GUI 构建脚本（GNU 工具链，不依赖 MSVC）

流程：
  1. Cython 把 zen_gui_probe_only.py 编译成 zen_gui_probe_only.c
  2. MinGW gcc 把 .c 编译成 zen_gui_probe_only.pyd
  3. PyInstaller -F 把 启动.py + .pyd + 依赖打成单文件 exe

依赖：
  - Python 3.14（含头文件 include/ 与 libs/python314.lib）
  - pip install cython pyinstaller
  - pip install PyQt6 requests
  - MinGW-w64 gcc（本机：C:\\Users\\WWW\\DevTools\\mingw64\\bin\\gcc.exe）

用法：
  python setup.py            # 完整构建，产物在 dist/ 下
  python setup.py build_ext  # 只编译 .pyd
"""

import os
import shutil
import subprocess
import sys

SRC = "zen_gui_probe_only.py"
LAUNCHER = "启动.py"
MODULE = "zen_gui_probe_only"

# MinGW gcc 与 Python 安装路径（按本机环境修改）
GCC = r"C:\Users\WWW\DevTools\mingw64\bin\gcc.exe"
PY_PREFIX = sys.base_prefix  # 例如 C:\Users\WWW\AppData\Local\Programs\Python\Python314


def sh(cmd, **kw):
    print(">", " ".join(str(c) for c in cmd))
    subprocess.check_call([str(c) for c in cmd], **kw)


def build_c():
    """步骤 1：Cython 生成 .c"""
    sh([sys.executable, "-m", "cython", "-3", SRC])
    assert os.path.exists(MODULE + ".c"), "Cython 未生成 .c"


def build_pyd():
    """步骤 2：gcc 编译 .pyd"""
    if not os.path.exists(MODULE + ".c"):
        build_c()
    sh([
        GCC, "-shared", "-O2", "-mthreads",
        "-I" + os.path.join(PY_PREFIX, "include"),
        MODULE + ".c",
        "-o", MODULE + ".pyd",
        "-L" + os.path.join(PY_PREFIX, "libs"),
        "-lpython314",
    ])
    assert os.path.exists(MODULE + ".pyd"), "gcc 未生成 .pyd"


def build_exe():
    """步骤 3：PyInstaller -F 打包单文件 exe"""
    if not os.path.exists(MODULE + ".pyd"):
        build_pyd()
    sh([
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm", "-F",
        "--name", "zen_gui_probe_only",
        "--distpath", "dist",
        "--workpath", "build",
        LAUNCHER,
    ])
    print("完成: " + os.path.join("dist", "zen_gui_probe_only.exe"))


def clean():
    """删除编译中间产物（保留 .py 原文与 dist/ 下的 exe）"""
    for d in ("build", "dist"):
        shutil.rmtree(d, ignore_ok=True)
    for f in (MODULE + ".c", MODULE + ".pyd", MODULE + ".spec"):
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "all"
    if action == "build_ext":
        build_pyd()
    elif action == "clean":
        clean()
    elif action == "all":
        build_exe()
    else:
        raise SystemExit("未知参数: " + action)
