"""安卓版 PDF 视图。

桌面版用 PyMuPDF 渲染 PDF，但 PyMuPDF 没有安卓轮子，所以安卓改用 Qt 自带的
QtPdf（QPdfDocument + QPdfView）——纯 Qt，安卓上可直接用，和桌面版互不影响。
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

try:  # QtPdf 属于可选模块，缺了就退化成提示页，不让播放器起不来
    from PyQt6.QtPdf import QPdfDocument
    from PyQt6.QtPdfWidgets import QPdfView

    QPDF_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于 Qt 构建里有没有 QtPdf
    QPDF_AVAILABLE = False
    QPdfDocument = None  # type: ignore[assignment]
    QPdfView = None  # type: ignore[assignment]

DEFAULT_INTERVAL_MS = 5_000  # 自动翻页间隔


class PdfView(QWidget):
    """单页浏览 + 自动翻页 + 循环，接口对齐桌面版的 DocumentView。"""

    statusChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:#202124;color:#e8e8ea;")
        self._autoplay = False
        self._loop = True
        self._interval = DEFAULT_INTERVAL_MS
        self._zoom = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if QPDF_AVAILABLE:
            self.doc = QPdfDocument(self)
            self.view = QPdfView(self)
            self.view.setDocument(self.doc)
            self.view.setPageMode(QPdfView.PageMode.SinglePage)
            self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            layout.addWidget(self.view)
        else:
            self.doc = None
            self.view = None
            hint = QLabel("此设备不支持 PDF 渲染（缺少 QtPdf 模块）")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(hint)

        self.timer = QTimer(self)
        self.timer.setInterval(self._interval)
        self.timer.timeout.connect(self.next_page)

    # ---------- 文档 ----------
    def load(self, path: str) -> bool:
        """打开 PDF；QtPdf 没有就返回 False。"""
        if self.doc is None:
            self.statusChanged.emit("缺少 QtPdf 模块，无法打开 PDF")
            return False
        self.doc.load(str(path))
        self._zoom = 1.0
        self._apply_interval()
        return True

    def close_document(self) -> None:
        self.timer.stop()
        if self.doc is not None:
            self.doc.close()

    @property
    def page_count(self) -> int:
        return self.doc.pageCount() if self.doc is not None else 0

    @property
    def current_page(self) -> int:
        navigator = self.view.pageNavigator() if self.view is not None else None
        return navigator.currentPage() if navigator is not None else 0

    # ---------- 翻页 ----------
    def goto_page(self, index: int) -> None:
        if self.view is None or self.page_count <= 0:
            return
        navigator = self.view.pageNavigator()
        navigator.jump(max(0, min(self.page_count - 1, int(index))), QPointF(), 0)

    def next_page(self) -> None:
        if self.page_count <= 0:
            return
        if self.current_page + 1 >= self.page_count:
            if self._loop:
                self.goto_page(0)
            else:
                self.timer.stop()
            return
        self.goto_page(self.current_page + 1)

    def prev_page(self) -> None:
        self.goto_page(self.current_page - 1)

    # ---------- 播放控制 ----------
    def set_autoplay(self, enabled: bool) -> None:
        self._autoplay = bool(enabled)
        if self._autoplay and self.page_count > 1:
            self.timer.start()
        else:
            self.timer.stop()

    def set_loop(self, enabled: bool) -> None:
        self._loop = bool(enabled)

    def set_interval(self, seconds: float | int) -> None:
        self._interval = max(500, int(float(seconds) * 1000))
        self._apply_interval()

    def set_fit_mode(self, mode: str) -> None:
        if self.view is None:
            return
        fit = QPdfView.ZoomMode.FitInView if str(mode).lower() in ("page", "fit", "all") \
            else QPdfView.ZoomMode.FitToWidth
        self.view.setZoomMode(fit)

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.2)

    def reset_zoom(self) -> None:
        if self.view is not None:
            self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self._zoom = 1.0

    # ---------- 内部 ----------
    def _set_zoom(self, factor: float) -> None:
        if self.view is None:
            return
        self._zoom = max(0.25, min(5.0, factor))
        self.view.setZoomMode(QPdfView.ZoomMode.Custom)
        self.view.setZoomFactor(self._zoom)

    def _apply_interval(self) -> None:
        self.timer.setInterval(self._interval)
        if self._autoplay and self.page_count > 1:
            self.timer.start()
