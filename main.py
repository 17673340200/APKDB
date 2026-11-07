"""多媒体播放器启动入口。"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from ui.main_window import FILE_ARG, MainWindow


def _startup_path(argv: list[str]) -> str | None:
    """取出 --file <路径> 传入的文件（重启恢复上次文件用）。"""
    if FILE_ARG in argv:
        index = argv.index(FILE_ARG)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MediaPlayer")
    app.setApplicationDisplayName("多媒体播放器")
    window = MainWindow()
    window.show()

    path = _startup_path(sys.argv)
    if path:
        window.open_path(path)
    else:
        window.open_default_file()  # 没指定文件时，默认播放接收目录里的第一个文件

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
