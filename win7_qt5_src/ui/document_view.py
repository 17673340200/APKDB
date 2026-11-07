"""文档（PDF / 幻灯片）显示与翻页视图。"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import QFrame, QLabel, QScrollArea

MIN_ZOOM = 0.1
MAX_ZOOM = 8.0
DEFAULT_INTERVAL_MS = 5000  # 自动放映默认间隔（毫秒）


def _clamp(value: float) -> float:
    return max(MIN_ZOOM, min(MAX_ZOOM, value))


class DocumentView(QScrollArea):
    """逐页渲染文档的滚动视图，支持适应页面 / 适应宽度 / 手动缩放。"""

    pageChanged = pyqtSignal(int, int)  # 当前页（1 基）、总页数
    zoomChanged = pyqtSignal(float)
    renderFailed = pyqtSignal(str)
    autoplayChanged = pyqtSignal(bool)  # 自动放映开启 / 关闭
    loopChanged = pyqtSignal(bool)  # 放映到末页后是否回到首页循环

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document = None
        self.index = 0
        self.fit_mode = "width"  # "width"（铺满宽度）或 "page"（整页可见）
        self.user_zoom: float | None = None
        self.autoplay = False
        self.loop = True  # 放映到末页后回到首页继续
        self.interval_ms = DEFAULT_INTERVAL_MS

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("background:#000000;")
        self.setWidget(self._label)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignCenter)  # 页面小于视口时居中，空白不堆在一侧
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QScrollArea{background:#202124;border:none;}")
        self.setFocusPolicy(Qt.NoFocus)
        self.viewport().setFocusPolicy(Qt.NoFocus)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)
        self._render_timer.timeout.connect(self._render)

        self._autoplay_timer = QTimer(self)
        self._autoplay_timer.setInterval(self.interval_ms)
        self._autoplay_timer.timeout.connect(self._autoplay_step)

    # ---------- 文档生命周期 ----------
    def set_document(self, document) -> None:
        self.close_document()
        self.document = document
        self.index = 0
        self.user_zoom = None
        self._render()
        self._emit_page()

    def close_document(self) -> None:
        self.set_autoplay(False)
        if self.document is not None:
            self.document.close()
            self.document = None
        self._label.setPixmap(QPixmap())
        self._label.resize(1, 1)

    @property
    def page_count(self) -> int:
        return self.document.page_count if self.document else 0

    @property
    def current_page(self) -> int:
        return self.index + 1 if self.document else 0

    # ---------- 翻页 ----------
    def goto_page(self, index: int) -> None:
        if not self.document:
            return
        target = max(0, min(self.page_count - 1, index))
        if target == self.index:
            return
        self.index = target
        self._render()
        if self.autoplay:  # 手动翻页后重新计时，保证每页停留完整间隔
            self._autoplay_timer.start()
        self._emit_page()

    def next_page(self) -> bool:
        if not self.document or self.index >= self.page_count - 1:
            return False
        self.goto_page(self.index + 1)
        return True

    def prev_page(self) -> bool:
        if not self.document or self.index <= 0:
            return False
        self.goto_page(self.index - 1)
        return True

    def first_page(self) -> None:
        self.goto_page(0)

    def last_page(self) -> None:
        self.goto_page(self.page_count - 1)

    # ---------- 自动放映 ----------
    def set_interval(self, seconds: int) -> None:
        """设置自动放映间隔（秒），播放中修改会立即生效。"""
        self.interval_ms = max(1, int(seconds)) * 1000
        self._autoplay_timer.setInterval(self.interval_ms)
        if self.autoplay:
            self._autoplay_timer.start()

    def set_autoplay(self, enabled: bool) -> None:
        self.autoplay = bool(enabled)
        if self.autoplay:
            self._autoplay_timer.setInterval(self.interval_ms)
            self._autoplay_timer.start()
        else:
            self._autoplay_timer.stop()
        self.autoplayChanged.emit(self.autoplay)

    def toggle_autoplay(self) -> None:
        self.set_autoplay(not self.autoplay)

    def set_loop(self, enabled: bool) -> None:
        """设置放映到末页后是否回到首页循环。"""
        self.loop = bool(enabled)
        self.loopChanged.emit(self.loop)

    def toggle_loop(self) -> None:
        self.set_loop(not self.loop)

    def _autoplay_step(self) -> None:
        if self.next_page():
            return
        if self.loop:
            self.goto_page(0)  # 末页回到首页，循环放映
        else:
            self.set_autoplay(False)  # 不循环时放映到末页停止

    # ---------- 缩放 ----------
    def zoom_in(self) -> None:
        self._set_user_zoom(self.current_zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_user_zoom(self.current_zoom / 1.2)

    def reset_zoom(self) -> None:
        self.user_zoom = None
        self._render()
        self.zoomChanged.emit(self.current_zoom)

    def set_fit_mode(self, mode: str) -> None:
        self.fit_mode = mode
        self.user_zoom = None
        self._render()
        self.zoomChanged.emit(self.current_zoom)

    def _set_user_zoom(self, zoom: float) -> None:
        if not self.document:
            return
        self.user_zoom = _clamp(zoom)
        self._render()
        self.zoomChanged.emit(self.user_zoom)

    @property
    def current_zoom(self) -> float:
        if self.user_zoom is not None:
            return self.user_zoom
        return self._fit_zoom()

    def _fit_zoom(self) -> float:
        if not self.document:
            return 1.0
        page_w, page_h = self.document.page_size(self.index)
        if page_w <= 0 or page_h <= 0:
            return 1.0
        view_w = max(self.viewport().width(), 50)
        view_h = max(self.viewport().height(), 50)
        if self.fit_mode == "width":
            zoom = view_w / page_w
            if page_h * zoom > view_h:
                # 高度放不下时退化为整页可见，否则滚动条会在右边占出一条黑边
                zoom = min(view_w / page_w, view_h / page_h)
            return _clamp(zoom)  # 宽度铺满视口
        return _clamp(min(view_w / page_w, view_h / page_h))

    # ---------- 渲染 ----------
    def _render(self) -> None:
        if not self.document:
            return
        zoom = self.current_zoom
        try:
            page = self.document.render_page(self.index, zoom)
            image = QImage(
                page.samples,
                page.width,
                page.height,
                page.stride,
                QImage.Format_RGB888,
            ).copy()
        except Exception as exc:
            self.renderFailed.emit(str(exc))
            return
        pixmap = QPixmap.fromImage(image)
        self._label.setPixmap(pixmap)
        self._label.resize(pixmap.size())
        self._center_scroll()

    def _center_scroll(self) -> None:
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.setValue(int((bar.minimum() + bar.maximum()) / 2))

    def _emit_page(self) -> None:
        self.pageChanged.emit(self.current_page, self.page_count)

    # ---------- 事件 ----------
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self.document:
            self._render_timer.start()

    def wheelEvent(self, event):  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self._set_user_zoom(self.current_zoom * (1.2 ** steps))
            event.accept()
            return
        super().wheelEvent(event)
