"""安卓版图片轮播（桌面版的图片播放由 DocumentView 承担，这里单独做一个轻量的）。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
DEFAULT_INTERVAL_MS = 5_000


class ImageSlideshow(QLabel):
    """打开一张图就把它所在文件夹里的图片都当成一个播放列表。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#000000;")
        self._files: list[str] = []
        self._index = 0
        self._autoplay = False
        self._loop = True
        self._interval = DEFAULT_INTERVAL_MS
        self.timer = QTimer(self)
        self.timer.setInterval(self._interval)
        self.timer.timeout.connect(self.next_image)

    # ---------- 载入 ----------
    def set_path(self, path: str) -> bool:
        target = Path(str(path))
        folder = target if target.is_dir() else target.parent
        files = sorted(str(p) for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS) \
            if folder.is_dir() else []
        if not files:
            self._files, self._index = [], 0
            self.clear()
            return False
        self._files = files
        self._index = files.index(str(target)) if str(target) in files else 0
        self._show_current()
        self._apply_timer()
        return True

    def close_document(self) -> None:
        self.timer.stop()
        self._files = []
        self.clear()

    # ---------- 翻页 ----------
    @property
    def page_count(self) -> int:
        return len(self._files)

    @property
    def current_page(self) -> int:
        return self._index

    def goto_page(self, index: int) -> None:
        if not self._files:
            return
        self._index = max(0, min(len(self._files) - 1, int(index)))
        self._show_current()

    def next_image(self) -> None:
        if not self._files:
            return
        if self._index + 1 >= len(self._files):
            if self._loop:
                self.goto_page(0)
            else:
                self.timer.stop()
            return
        self.goto_page(self._index + 1)

    def prev_image(self) -> None:
        self.goto_page(self._index - 1)

    # 与 PdfView / DocumentView 对齐的翻页接口（远程控制指令统一调 next_page/prev_page）
    def next_page(self) -> None:
        self.next_image()

    def prev_page(self) -> None:
        self.prev_image()

    # ---------- 播放控制 ----------
    def set_autoplay(self, enabled: bool) -> None:
        self._autoplay = bool(enabled)
        self._apply_timer()

    def set_loop(self, enabled: bool) -> None:
        self._loop = bool(enabled)

    def set_interval(self, seconds: float | int) -> None:
        self._interval = max(500, int(float(seconds) * 1000))
        self.timer.setInterval(self._interval)
        self._apply_timer()

    def set_fit_mode(self, mode: str) -> None:  # 图片恒按窗口缩放，保留接口对齐
        self._show_current()

    def zoom_in(self) -> None:  # 图片无需缩放接口，保留空实现以免调用方报错
        pass

    def zoom_out(self) -> None:
        pass

    def reset_zoom(self) -> None:
        pass

    # ---------- 内部 ----------
    def _apply_timer(self) -> None:
        if self._autoplay and len(self._files) > 1:
            self.timer.start()
        else:
            self.timer.stop()

    def _show_current(self) -> None:
        if not self._files:
            self.clear()
            return
        pixmap = QPixmap(self._files[self._index])
        if pixmap.isNull():
            self.setText(f"无法打开图片：{Path(self._files[self._index]).name}")
            return
        self.setPixmap(pixmap.scaled(self.size(),
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802
        self._show_current()
        super().resizeEvent(event)
