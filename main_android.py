"""安卓客户端入口（PySide6 / Qt for Android）。

桌面入口是 main.py（PyQt6 + PyMuPDF）；这个入口是安卓用的：
先装好 Qt 绑定兼容层（qtcompat），再启动移动端界面。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:  # 安卓环境里工作目录不一定是项目根
    sys.path.insert(0, ROOT)

import qtcompat  # noqa: E402  必须早于任何 PyQt6 导入

BINDING = qtcompat.install()  # 安卓上会把 PyQt6.* 映射到 PySide6.*


def main() -> int:
    from PyQt6.QtWidgets import QApplication

    from android.mobile_window import MobileWindow

    app = QApplication(sys.argv)
    window = MobileWindow()
    window.showFullScreen()  # 安卓上直接全屏播放
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
