"""主窗口：打开文件、切换 PDF / 幻灯片 / 视频视图、全屏与快捷键。"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

from PyQt6.QtCore import (
    QBuffer,
    QIODevice,
    QProcess,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QGuiApplication, QImage, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.audio_capture import AudioCapture
from core.control_client import ControlClient, ControlListener
from core.control_protocol import (
    DEFAULT_CONTROL_HOST,
    DEFAULT_CONTROL_PORT,
    DEFAULT_FRAME_WIDTH,
    DEFAULT_LISTEN_PORT,
    DEFAULT_SCHEDULE_FILE,
    MAX_FPS,
    SYNC_CORRECT_INTERVAL,
    SYNC_TOLERANCE_MS,
)
from core.converter import PRESENTATION_EXTENSIONS, ConversionError, convert_to_pdf
from core.document import Document, DocumentError
from core.file_server import FileTransferServer
from ui.document_view import DEFAULT_INTERVAL_MS, DocumentView
from ui.schedule_dialog import ScheduleDialog
from ui.video_view import VideoView, is_stream_url

SCHEDULE_FILE = Path(DEFAULT_SCHEDULE_FILE)  # 定时播放计划（服务端下发的内容也写在这里）
SCHEDULE_CHECK_MS = 15_000  # 定时检查间隔


# 时段判断与计划读写放在 core/schedule.py（安卓版也用同一份）
from core.schedule import in_window as _in_window
from core.schedule import load_schedule as _read_schedule
from core.schedule import parse_hhmm as _parse_hhmm
from core.schedule import save_schedule as _write_schedule
from core.schedule import today as _today
from core.schedule import window_key as _window_key

PDF_EXTENSIONS = {".pdf"}
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".m4v", ".3gp", ".ts",
}
ALL_EXTENSIONS = sorted(PDF_EXTENSIONS | PRESENTATION_EXTENSIONS | VIDEO_EXTENSIONS)
JPEG_QUALITY = 55  # 实时画面帧压缩质量（兼顾清晰度与带宽）
FILE_FILTER = (
    "所有支持的文件 (" + " ".join(f"*{ext}" for ext in ALL_EXTENSIONS) + ");;"
    "PDF 文档 (*.pdf);;"
    "演示文稿 (*.ppt *.pptx *.pps *.ppsx *.odp);;"
    "视频文件 (" + " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS)) + ");;"
    "所有文件 (*.*)"
)
FILE_ARG = "--file"  # 启动时自动打开的文件的参数名（重启恢复用）


class ConvertWorker(QThread):
    """后台把演示文稿转成 PDF，避免界面卡住。"""

    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, src: str, parent=None):
        super().__init__(parent)
        self.src = src

    def run(self):  # noqa: D102
        try:
            self.finished_ok.emit(convert_to_pdf(self.src))
        except ConversionError as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("多媒体播放器")
        self.resize(1200, 780)

        self.current_path: str | None = None
        self.current_kind: str | None = None  # "document" / "video"
        self._worker: ConvertWorker | None = None

        self.doc_view = DocumentView()
        self.video_view = VideoView()
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_welcome())
        self._stack.addWidget(self.doc_view)
        self._stack.addWidget(self.video_view)
        self.setCentralWidget(self._stack)

        # 去掉堆叠布局的默认边距，并把底色设成黑色，避免全屏时四周露出白边
        stack_layout = self._stack.layout()
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(0)
        self.setContentsMargins(0, 0, 0, 0)
        # 只给中央区域设黑底（写在 QMainWindow 上会把调色板传给菜单栏，导致菜单变黑）
        self.setStyleSheet("QStackedWidget{background:#000000;}")

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self._connect_signals()
        self._start_file_server()
        self._start_control_client()
        self._init_schedule()
        self._init_sync()
        self.statusBar().showMessage("就绪")
        self._update_controls()

    # ---------- 界面搭建 ----------
    def _build_welcome(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)
        page.setStyleSheet("background:#202124;color:#c8c8cc;")

        title = QLabel("多媒体播放器")
        title.setStyleSheet("font-size:26px;font-weight:bold;color:#f0f0f2;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        hint = QLabel("支持 PDF、PPT/PPTX 演示文稿和常见视频格式\n点击下面的按钮或按 Ctrl+O 选择文件")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        open_btn = QPushButton("打开文件")
        open_btn.setFixedSize(160, 38)
        open_btn.setStyleSheet(
            "QPushButton{background:#2f6fed;color:white;border:none;border-radius:4px;font-size:14px;}"
            "QPushButton:hover{background:#3d7bf5;}"
        )
        open_btn.clicked.connect(self.open_dialog)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        return page

    def _create_actions(self) -> None:
        self.act_open = QAction("打开(&O)", self)
        self.act_open.setShortcut(QKeySequence("Ctrl+O"))
        self.act_open.triggered.connect(self.open_dialog)

        self.act_open_url = QAction("打开网络流(&U)", self)
        self.act_open_url.setShortcut(QKeySequence("Ctrl+U"))
        self.act_open_url.triggered.connect(self.open_url_dialog)

        self.act_quit = QAction("退出(&X)", self)
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.act_quit.triggered.connect(self.close)

        self.act_fullscreen = QAction("全屏播放(&F)", self)
        self.act_fullscreen.setShortcut(QKeySequence("F11"))
        self.act_fullscreen.triggered.connect(self.toggle_fullscreen)

        self.act_prev = QAction("上一页", self)
        self.act_prev.setShortcut(QKeySequence("Left"))
        self.act_prev.triggered.connect(self.doc_view.prev_page)

        self.act_next = QAction("下一页", self)
        self.act_next.setShortcut(QKeySequence("Right"))
        self.act_next.triggered.connect(self.doc_view.next_page)

        self.act_autoplay = QAction("自动放映(&A)", self)
        self.act_autoplay.setCheckable(True)
        self.act_autoplay.triggered.connect(self.doc_view.set_autoplay)

        self.act_fit_page = QAction("适应页面", self)
        self.act_fit_page.triggered.connect(lambda: self.doc_view.set_fit_mode("page"))

        self.act_fit_width = QAction("适应宽度", self)
        self.act_fit_width.triggered.connect(lambda: self.doc_view.set_fit_mode("width"))

        self.act_zoom_in = QAction("放大", self)
        self.act_zoom_in.setShortcut(QKeySequence("Ctrl+="))
        self.act_zoom_in.triggered.connect(self.doc_view.zoom_in)

        self.act_zoom_out = QAction("缩小", self)
        self.act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        self.act_zoom_out.triggered.connect(self.doc_view.zoom_out)

        self.act_zoom_reset = QAction("重置缩放", self)
        self.act_zoom_reset.setShortcut(QKeySequence("Ctrl+0"))
        self.act_zoom_reset.triggered.connect(self.doc_view.reset_zoom)

        self.act_schedule = QAction("定时播放计划(&S)", self)
        self.act_schedule.triggered.connect(self.edit_schedule)

        self.act_about = QAction("关于", self)
        self.act_about.triggered.connect(self.show_about)

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_open_url)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction(self.act_fullscreen)
        view_menu.addAction(self.act_autoplay)
        view_menu.addSeparator()
        view_menu.addAction(self.act_prev)
        view_menu.addAction(self.act_next)
        view_menu.addSeparator()
        view_menu.addAction(self.act_fit_page)
        view_menu.addAction(self.act_fit_width)
        view_menu.addAction(self.act_zoom_in)
        view_menu.addAction(self.act_zoom_out)
        view_menu.addAction(self.act_zoom_reset)

        tool_menu = self.menuBar().addMenu("工具(&T)")
        tool_menu.addAction(self.act_schedule)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        help_menu.addAction(self.act_about)

    def _create_toolbar(self) -> None:
        bar = QToolBar("主工具栏")
        self._toolbar = bar
        bar.setMovable(False)
        bar.setStyleSheet("QToolBar{background:#2b2b30;color:#e8e8ea;spacing:6px;padding:4px;}")
        self.addToolBar(bar)

        bar.addAction(self.act_open)
        bar.addAction(self.act_open_url)

        self.btn_prev = QPushButton("上一页")
        self.btn_next = QPushButton("下一页")
        self.btn_prev.setFixedWidth(64)
        self.btn_next.setFixedWidth(64)
        self.btn_prev.clicked.connect(self.doc_view.prev_page)
        self.btn_next.clicked.connect(self.doc_view.next_page)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)

        self.page_label = QLabel("0 / 0")
        self.page_label.setMinimumWidth(80)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.page_label)

        self.fit_combo = QComboBox()
        self.fit_combo.addItem("适应页面", "page")
        self.fit_combo.addItem("适应宽度", "width")
        self.fit_combo.setFixedWidth(96)
        self.fit_combo.setCurrentIndex(self.fit_combo.findData("width"))
        self.fit_combo.currentIndexChanged.connect(
            lambda: self.doc_view.set_fit_mode(self.fit_combo.currentData())
        )
        bar.addWidget(self.fit_combo)

        self.btn_autoplay = QPushButton("自动放映")
        self.btn_autoplay.setCheckable(True)
        self.btn_autoplay.setFixedWidth(72)
        self.btn_autoplay.setStyleSheet(
            "QPushButton:checked{background:#2f6fed;color:white;border:none;border-radius:4px;}"
        )
        self.btn_autoplay.toggled.connect(self.doc_view.set_autoplay)
        bar.addWidget(self.btn_autoplay)

        self.btn_doc_loop = QPushButton("循环")
        self.btn_doc_loop.setCheckable(True)
        self.btn_doc_loop.setChecked(True)
        self.btn_doc_loop.setFixedWidth(48)
        self.btn_doc_loop.setToolTip("放映到末页后回到首页循环")
        self.btn_doc_loop.setStyleSheet(
            "QPushButton:checked{background:#2f6fed;color:white;border:none;border-radius:4px;}"
        )
        self.btn_doc_loop.toggled.connect(self.doc_view.set_loop)
        bar.addWidget(self.btn_doc_loop)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 3600)
        self.interval_spin.setValue(DEFAULT_INTERVAL_MS // 1000)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setFixedWidth(76)
        self.interval_spin.setToolTip("自动放映每页停留时间")
        self.interval_spin.valueChanged.connect(self.doc_view.set_interval)
        bar.addWidget(self.interval_spin)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        self.btn_zoom_out = QPushButton("-")
        self.btn_zoom_in = QPushButton("+")
        self.zoom_label = QLabel("100%")
        self.btn_zoom_out.setFixedWidth(28)
        self.btn_zoom_in.setFixedWidth(28)
        self.zoom_label.setFixedWidth(56)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_zoom_out.clicked.connect(self.doc_view.zoom_out)
        self.btn_zoom_in.clicked.connect(self.doc_view.zoom_in)
        bar.addWidget(self.btn_zoom_out)
        bar.addWidget(self.zoom_label)
        bar.addWidget(self.btn_zoom_in)

        self.btn_fullscreen = QPushButton("全屏 (F11)")
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        bar.addWidget(self.btn_fullscreen)

        self.btn_restart = QPushButton("重启软件")
        self.btn_restart.setFixedWidth(72)
        self.btn_restart.setToolTip("关闭并重新启动本程序")
        self.btn_restart.clicked.connect(self.restart)
        bar.addWidget(self.btn_restart)

    def _connect_signals(self) -> None:
        self.doc_view.pageChanged.connect(self._on_page_changed)
        self.doc_view.autoplayChanged.connect(self.btn_autoplay.setChecked)
        self.doc_view.loopChanged.connect(self.btn_doc_loop.setChecked)
        self.doc_view.autoplayChanged.connect(self.act_autoplay.setChecked)
        self.doc_view.zoomChanged.connect(self._on_zoom_changed)
        self.doc_view.renderFailed.connect(lambda msg: self._warn(msg))
        self.video_view.fullscreenRequested.connect(self.toggle_fullscreen)
        self.video_view.playingChanged.connect(self._on_video_playing_changed)
        self.video_view.errorOccurred.connect(lambda msg: self._warn(f"视频播放失败：{msg}"))

    # ---------- 文件收发服务 ----------
    def _start_file_server(self) -> None:
        self._file_server = FileTransferServer(parent=self)
        self._file_server.fileReceived.connect(self.open_path)
        self._file_server.statusChanged.connect(self.statusBar().showMessage)
        self._file_server.start()

    # ---------- 远程受控 ----------
    def _start_control_client(self) -> None:
        """受控入口：既监听服务端主动连入，也支持通过环境变量主动连出。"""
        self._shot_timer = QTimer(self)
        self._shot_timer.timeout.connect(self._send_autoshot)
        self._record_timer = QTimer(self)
        self._record_timer.timeout.connect(self._send_record_frame)
        self._record_session: ControlClient | None = None
        self._record_width = DEFAULT_FRAME_WIDTH
        self._audio_capture: AudioCapture | None = None
        self._control_sessions: list[ControlClient] = []
        self._autoshot_session: ControlClient | None = None

        # 1) 监听：服务端"添加客户端"后主动连进来（播放器无需任何配置）
        self._control_listener = ControlListener(port=DEFAULT_LISTEN_PORT, parent=self)
        self._control_listener.connectionAccepted.connect(self._on_control_connection)
        self._control_listener.statusChanged.connect(self.statusBar().showMessage)
        self._control_listener.start()

        # 2) 主动连出：设置了 MEDIAPLAYER_CONTROL_HOST 才启用
        if DEFAULT_CONTROL_HOST:
            client = ControlClient(DEFAULT_CONTROL_HOST, DEFAULT_CONTROL_PORT,
                                   storage=self._file_server.storage, parent=self)
            self._wire_session(client)
            self._control_sessions.append(client)
            client.start()

    def _wire_session(self, session: ControlClient) -> None:
        session.fileReceived.connect(self.open_path)
        session.controlReceived.connect(self.apply_control)
        session.screenshotRequested.connect(self._send_screenshot)
        session.autoshotChanged.connect(
            lambda seconds, s=session: self._set_autoshot(s, seconds))
        session.recordChanged.connect(self._set_record)
        session.audioChanged.connect(self._set_audio_capture)
        session.fileDeleteRequested.connect(self._delete_media_file)
        session.statusChanged.connect(self.statusBar().showMessage)

    def _on_control_connection(self, sock) -> None:
        """控制服务端主动连入：为这条连接开一个受控会话。"""
        session = ControlClient(self._control_listener.host, DEFAULT_LISTEN_PORT,
                                storage=self._file_server.storage, parent=self,
                                sock=sock, reconnect=False)
        self._wire_session(session)
        self._control_sessions.append(session)
        session.start()

    def _set_record(self, session: ControlClient, fps: int, width: int) -> None:
        """服务端要求实时画面：按 fps 持续抓取本软件窗口回传（fps<=0 停止）。"""
        if fps <= 0:
            if self._record_session is session or self._record_session is None:
                self._record_timer.stop()
                self._record_session = None
            return
        self._record_session = session
        self._record_width = max(160, int(width))
        self._record_timer.setInterval(max(33, int(1000 / min(fps, MAX_FPS))))
        self._record_timer.start()

    def _set_audio_capture(self, session: ControlClient, enabled: int) -> None:
        """服务端要录声音：开启 / 停止系统声音采集并回传 PCM。"""
        if not enabled:
            capture, self._audio_capture = self._audio_capture, None
            if capture is not None:
                capture.stop()
            return
        if self._audio_capture is not None:  # 已在采集
            return
        try:
            capture = AudioCapture(parent=self)
        except Exception as exc:  # 依赖缺失等
            session.send_event(f"声音采集不可用：{exc}")
            return
        capture.chunkReady.connect(
            lambda pcm, s=session, c=capture: s.send_audio_frame(pcm, c.rate, c.channels))
        capture.failed.connect(
            lambda msg, s=session: (s.send_event(f"声音采集失败：{msg}"),
                                    self.statusBar().showMessage(f"声音采集失败：{msg}", 5000)))
        self._audio_capture = capture
        capture.start()

    def _send_record_frame(self) -> None:
        session = self._record_session
        if session is None:
            self._record_timer.stop()
            return
        shot = self._capture_window()
        if shot.width() > self._record_width:
            shot = shot.scaledToWidth(self._record_width,
                                      Qt.TransformationMode.SmoothTransformation)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        shot.save(buffer, "JPG", JPEG_QUALITY)
        buffer.close()
        session.send_video_frame(bytes(buffer.data()), shot.width(), shot.height())

    def _stop_control_sessions(self) -> None:
        self._shot_timer.stop()
        self._record_timer.stop()
        self._schedule_timer.stop()
        self._sync_timer.stop()
        if self._audio_capture is not None:  # 停掉系统声音采集
            self._audio_capture.stop()
            self._audio_capture = None
        if self._control_listener is not None:
            self._control_listener.stop()
        for session in self._control_sessions:
            session.stop()
        self._control_sessions.clear()

    def _set_autoshot(self, session: ControlClient, seconds: int) -> None:
        """服务端设置的自动截屏间隔（秒，0 关闭）。"""
        if seconds > 0:
            self._autoshot_session = session
            self._shot_timer.setInterval(seconds * 1000)
            self._shot_timer.start()
        else:
            self._shot_timer.stop()
            self._autoshot_session = None

    def _send_autoshot(self) -> None:
        if self._autoshot_session is not None:
            self._send_screenshot(self._autoshot_session)

    def _capture_window(self) -> QImage:
        """抓取本软件窗口在屏幕上的真实画面。

        QWidget.grab() 只抓窗口自身的绘制，抓不到 QVideoWidget 的原生渲染层，
        视频区域会是黑的；这里改为抓屏幕后按窗口位置裁剪，视频内容才会出现。
        """
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return self.grab().toImage()
        image = screen.grabWindow(0).toImage()  # 抓整屏（Qt6 会忽略区域参数）
        ratio = screen.devicePixelRatio()
        origin = screen.geometry().topLeft()
        geo = self.geometry()  # 顶层窗口的客户区，坐标为屏幕坐标
        x = max(0, int((geo.x() - origin.x()) * ratio))
        y = max(0, int((geo.y() - origin.y()) * ratio))
        width = max(1, min(int(geo.width() * ratio), image.width() - x))
        height = max(1, min(int(geo.height() * ratio), image.height() - y))
        return image.copy(x, y, width, height)

    def _send_screenshot(self, session: ControlClient | None = None) -> None:
        """抓取本软件窗口并回传给指定的控制服务端。"""
        if session is None:
            session = self._autoshot_session or (
                self._control_sessions[-1] if self._control_sessions else None)
        if session is None:
            return
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self._capture_window().save(buffer, "PNG")
        buffer.close()
        session.send_screenshot(bytes(buffer.data()))

    def _release_media_file(self, path: Path) -> None:
        """要删的正是当前在播的文件时先释放它，否则系统里文件被占用删不掉。"""
        current = Path(self.current_path) if self.current_path else None
        if current is None or current.name != Path(path).name:
            return
        if self.current_kind == "video":
            self.video_view.close_media()
        else:
            self.doc_view.close_document()
        self.current_path = None
        self.current_kind = None
        self._stack.setCurrentIndex(0)  # 回到欢迎页
        self._update_controls()

    def _delete_media_file(self, name: str, ticket) -> None:
        """执行服务端发来的删除请求，结果填进回执单并通知等待中的会话线程。"""
        ok, msg = False, ""
        try:
            path = self._file_server.storage / Path(name).name
            if not path.is_file():
                msg = f"文件不存在：{name}"
            else:
                self._release_media_file(path)
                try:
                    path.unlink()
                except PermissionError:  # 仍被占用：再释放一次后重试
                    self._release_media_file(path)
                    path.unlink()
                ok, msg = True, f"已删除 {name}"
                self.statusBar().showMessage(msg, 5000)
        except OSError as exc:
            msg = f"删除失败：{exc}"
        finally:
            ticket.ok = ok
            ticket.msg = msg
            ticket.done.set()

    def _resolve_media_path(self, path: str) -> str:
        """服务端可能只给文件名（客户端接收目录里的文件），这里补全成本地路径。"""
        if os.path.isabs(path) or Path(path).is_file():
            return path
        candidate = self._file_server.storage / path
        if candidate.is_file():
            return str(candidate)
        return path

    # ---------- 定时播放 ----------
    def _init_schedule(self) -> None:
        self._schedule: list[dict] = self._load_schedule()
        self._schedule_active: dict[str, str] = {}  # 正在播放的时段 → 当天日期
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(SCHEDULE_CHECK_MS)
        self._schedule_timer.timeout.connect(self._check_schedule)
        self._schedule_timer.start()

    def _load_schedule(self) -> list[dict]:
        return _read_schedule(SCHEDULE_FILE)

    def _save_schedule(self) -> None:
        try:
            _write_schedule(SCHEDULE_FILE, self._schedule)
        except OSError as exc:
            self.statusBar().showMessage(f"定时计划保存失败：{exc}")

    def _check_schedule(self) -> None:
        """按播放时段执行计划：进入时段就开始播放，过了结束时间就停止。"""
        minutes = _parse_hhmm(time.strftime("%H:%M")) or 0
        day = _today()
        inside: set[str] = set()

        for item in self._schedule:
            if not item.get("enabled", True):
                continue
            start = _parse_hhmm(str(item.get("start") or item.get("time") or ""))
            if start is None:
                continue
            end = _parse_hhmm(str(item.get("end") or ""))  # 不填结束时间表示播到当天结束
            target = str(item.get("file") or item.get("name"))
            key = _window_key(day, start, end, target)
            if not _in_window(minutes, start, end):
                continue
            inside.add(key)
            if key in self._schedule_active:
                continue  # 该时段已在播放，不重复打开
            self._schedule_active[key] = day
            self._start_scheduled_item(item, target)

        for key in list(self._schedule_active):  # 已过结束时间 → 停止播放
            if key not in inside:
                del self._schedule_active[key]
                self._stop_scheduled_item()

    def _start_scheduled_item(self, item: dict, target: str) -> None:
        path = self._resolve_media_path(target)
        self.open_path(path)
        loop = bool(item.get("loop", True))
        if self.current_kind == "video":
            if self.video_view.loop_btn.isChecked() != loop:
                self.video_view.toggle_loop()
            self.video_view.play()
        else:
            self.doc_view.set_autoplay(True)
            self.doc_view.set_loop(loop)
        self.statusBar().showMessage(f"定时播放：{os.path.basename(path)}", 5000)

    def _stop_scheduled_item(self) -> None:
        """定时时段结束：暂停并保留当前画面。

        用 pause() 而不是 stop()——stop() 会清空画面变成黑屏；
        暂停后画面停在最后一帧，文档则停在当前页。
        """
        if self.current_kind == "video":
            self.video_view.pause()
        else:
            self.doc_view.set_autoplay(False)
        self.statusBar().showMessage("定时播放时段已结束，已暂停（保留当前画面）", 5000)

    def apply_schedule(self, items: list[dict]) -> None:
        """接收服务端下发的定时计划（同时写进本地配置，重启后仍然生效）。"""
        self._schedule = [dict(item) for item in items
                          if isinstance(item, dict)
                          and (item.get("start") or item.get("time"))
                          and (item.get("file") or item.get("name"))]
        self._schedule_active.clear()
        self._save_schedule()
        self.statusBar().showMessage(f"已更新定时播放计划：{len(self._schedule)} 项")

    def edit_schedule(self) -> None:
        """在本机编辑定时播放计划。"""
        dialog = ScheduleDialog(self._schedule, self)
        dialog.exec()
        self._schedule = dialog.items()
        self._schedule_active.clear()
        self._save_schedule()
        self.statusBar().showMessage(f"定时播放计划已保存：{len(self._schedule)} 项")

    # ---------- 同步播放 ----------
    def _init_sync(self) -> None:
        self._sync_state: dict | None = None
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(int(SYNC_CORRECT_INTERVAL * 1000))
        self._sync_timer.timeout.connect(self._sync_correct)

    def _sync_play(self, args: dict) -> None:
        """与其他客户端在同一时刻开始播放同一内容（start_at 用服务端时钟）。"""
        name = args.get("name") or args.get("path") or args.get("file")
        if not name:
            return
        start_at = float(args.get("start_at", 0) or 0)
        server_time = float(args.get("server_time", 0) or 0) or time.time()
        offset = server_time - time.time()  # 服务端时钟 − 本机时钟
        path = self._resolve_media_path(str(name))

        self.open_path(path)
        if self.current_kind == "video":
            self.video_view.pause()
            self.video_view.set_position(0)
            if args.get("loop") is not None and (
                    self.video_view.loop_btn.isChecked() != bool(args.get("loop"))):
                self.video_view.toggle_loop()
        else:
            self.doc_view.goto_page(0)

        self._sync_state = {"start_at": start_at, "offset": offset,
                            "kind": self.current_kind,
                            "file": os.path.basename(path)}
        delay_ms = max(0, int((start_at - offset - time.time()) * 1000))
        QTimer.singleShot(delay_ms, self._sync_start)
        self.statusBar().showMessage(
            f"同步播放：{self._sync_state['file']}（{delay_ms / 1000:.1f} 秒后起播）")

    def _sync_start(self) -> None:
        state = self._sync_state
        if state is None:
            return
        if state["kind"] == "video":
            self.video_view.set_position(0)
            self.video_view.play()
        else:
            self.doc_view.goto_page(0)
            self.doc_view.set_autoplay(True)
        self._sync_timer.start()

    def _sync_correct(self) -> None:
        """按服务端时钟定期校正进度，避免多机长时间播放后越走越偏。"""
        state = self._sync_state
        if state is None or state["kind"] != "video":
            self._sync_timer.stop()
            return
        if self.video_view.duration() <= 0:
            return  # 还在加载
        expected_ms = int((time.time() + state["offset"] - state["start_at"]) * 1000)
        total = self.video_view.duration()
        if total > 0:
            expected_ms %= total
        if abs(expected_ms - self.video_view.position()) > SYNC_TOLERANCE_MS:
            self.video_view.set_position(expected_ms)

    def _stop_sync(self) -> None:
        self._sync_state = None
        self._sync_timer.stop()

    def apply_control(self, action: str, args: dict) -> None:
        """执行服务端下发的远程操作。"""
        video, doc = self.video_view, self.doc_view
        if action == "open":
            path = args.get("path") or args.get("name")
            if path:
                self.open_path(self._resolve_media_path(str(path)))
        elif action == "play":
            video.play()
        elif action == "pause":
            video.pause()
        elif action == "toggle":
            video.toggle_play()
        elif action == "stop":
            video.stop()
        elif action == "seek":
            video.seek(int(args.get("ms", 0)))
        elif action == "volume":
            video.volume.setValue(max(0, min(100, int(args.get("value", 50)))))
        elif action == "mute":
            if video.audio.isMuted() != bool(args.get("on", True)):
                video.toggle_mute()
        elif action == "loop":
            if video.loop_btn.isChecked() != bool(args.get("on", True)):
                video.toggle_loop()
        elif action == "fullscreen":
            if self.isFullScreen() != bool(args.get("on", True)):
                self.toggle_fullscreen()
        elif action == "next":
            doc.next_page()
        elif action == "prev":
            doc.prev_page()
        elif action == "page":
            doc.goto_page(max(0, int(args.get("index", 1)) - 1))
        elif action == "autoplay":
            doc.set_autoplay(bool(args.get("on", True)))
        elif action == "interval":
            self.interval_spin.setValue(max(1, int(args.get("seconds", 5))))
        elif action == "docloop":
            doc.set_loop(bool(args.get("on", True)))
        elif action == "fit":
            doc.set_fit_mode(str(args.get("mode", "width")))
        elif action == "schedule":
            self.apply_schedule(list(args.get("items") or []))
        elif action == "sync":
            self._sync_play(args)
        elif action == "syncoff":
            self._stop_sync()
        elif action == "restart":
            self.restart()
        elif action == "quit":
            self.close()
            app = QApplication.instance()
            if app is not None:
                app.quit()
        else:
            self.statusBar().showMessage(f"未知控制指令：{action}")

    def open_default_file(self) -> bool:
        """启动时默认播放接收目录里的第一个可播放文件（最近收到的排在最前）。"""
        storage = self._file_server.storage
        for item in self._file_server.list_files():
            if os.path.splitext(item["name"])[1].lower() not in ALL_EXTENSIONS:
                continue
            self.open_path(str(storage / item["name"]))
            return True
        self.statusBar().showMessage("接收目录暂无可播放的文件")
        return False

    # ---------- 打开文件 ----------
    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要播放的文件", str(Path.home()), FILE_FILTER
        )
        if path:
            self.open_path(path)

    def open_url_dialog(self) -> None:
        url, ok = QInputDialog.getText(
            self,
            "打开网络流",
            "输入流地址（rtsp://、rtmp://、http:// 等）：\n"
            "例：rtsp://192.168.1.10:554/live",
            text="rtsp://",
        )
        if ok and url.strip():
            self.open_path(url.strip())

    def open_path(self, path: str) -> None:
        if is_stream_url(path):
            self._open_video(path)
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in PDF_EXTENSIONS:
            self._open_pdf(path)
        elif ext in PRESENTATION_EXTENSIONS:
            self._open_presentation(path)
        elif ext in VIDEO_EXTENSIONS:
            self._open_video(path)
        else:
            self._warn(f"暂不支持的文件类型：{ext or '未知'}")

    def _open_pdf(self, path: str) -> None:
        try:
            document = Document(path)
        except DocumentError as exc:
            self._warn(str(exc))
            return
        self.doc_view.set_document(document)
        self._switch_to("document", path)

    def _open_presentation(self, path: str) -> None:
        self.statusBar().showMessage(f"正在转换 {os.path.basename(path)} …")
        self.setCursor(Qt.CursorShape.BusyCursor)
        self._worker = ConvertWorker(path, self)
        self._worker.finished_ok.connect(lambda pdf: self._on_converted(pdf, path))
        self._worker.failed.connect(lambda msg: self._warn(msg))
        self._worker.start()

    def _on_converted(self, pdf_path: str, src_path: str) -> None:
        self.unsetCursor()
        try:
            document = Document(pdf_path)
        except DocumentError as exc:
            self._warn(str(exc))
            return
        self.doc_view.set_document(document)
        self._switch_to("document", src_path)
        self.statusBar().showMessage(
            f"已转换并播放：{os.path.basename(src_path)}（共 {document.page_count} 页）", 5000
        )

    def _open_video(self, path: str) -> None:
        self._switch_to("video", path)
        self.video_view.load(path)

    def _switch_to(self, kind: str, path: str) -> None:
        if self.current_kind == "video" and kind != "video":
            self.video_view.stop()
        self.current_kind = kind
        self.current_path = path
        self._stack.setCurrentIndex(1 if kind == "document" else 2)
        name = path if is_stream_url(path) else os.path.basename(path)
        self.setWindowTitle(f"多媒体播放器 - {name}")
        self.statusBar().showMessage(path)
        self._update_controls()
        if kind == "document" and not self.isFullScreen():
            self.toggle_fullscreen()  # 打开文档即全屏放映

    # ---------- 全屏 ----------
    def toggle_fullscreen(self) -> None:
        entering = not self.isFullScreen()
        if entering:
            # 全屏时隐藏所有界面控件：菜单栏、工具栏、状态栏
            self.menuBar().hide()
            self._toolbar.hide()
            self.statusBar().hide()
            self.showFullScreen()
            self._disable_dwm_border()
        else:
            self.showNormal()
            self.menuBar().show()
            self._toolbar.show()
            self.statusBar().show()

        self.video_view.set_fullscreen(entering)
        self.btn_fullscreen.setText("退出全屏 (F11)" if entering else "全屏 (F11)")
        self._update_controls()

    def _disable_dwm_border(self) -> None:
        """Windows 11 会给全屏窗口绘制 1px 边框和圆角，四周露出桌面底色（白边）。

        这里通过 DWM 关闭圆角并把边框颜色设为无；旧系统不支持时调用静默失败。
        """
        if sys.platform != "win32":
            return
        try:
            dwm = ctypes.windll.dwmapi
            preference = ctypes.c_int(1)  # DWMWCP_DONOTROUND
            dwm.DwmSetWindowAttribute(
                int(self.winId()), 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
            color = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
            dwm.DwmSetWindowAttribute(
                int(self.winId()), 34, ctypes.byref(color), ctypes.sizeof(color)
            )
        except OSError:
            pass

    def _on_video_playing_changed(self, playing: bool) -> None:
        """播放即全屏：视频一进入播放状态就自动全屏。"""
        if playing and self.current_kind == "video" and not self.isFullScreen():
            self.toggle_fullscreen()

    # ---------- 快捷键 ----------
    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        modifiers = event.modifiers()

        if key == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            event.accept()
            return
        if key == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
            event.accept()
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_O:
                self.open_dialog()
                event.accept()
                return
            if key == Qt.Key.Key_U:
                self.open_url_dialog()
                event.accept()
                return
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.doc_view.zoom_in()
                event.accept()
                return
            if key == Qt.Key.Key_Minus:
                self.doc_view.zoom_out()
                event.accept()
                return
            if key == Qt.Key.Key_0:
                self.doc_view.reset_zoom()
                event.accept()
                return

        if self.current_kind == "document":
            if key == Qt.Key.Key_A and modifiers == Qt.KeyboardModifier.NoModifier:
                self.doc_view.toggle_autoplay()
                event.accept()
                return
            if key == Qt.Key.Key_L and modifiers == Qt.KeyboardModifier.NoModifier:
                self.doc_view.toggle_loop()
                event.accept()
                return
            if key in (Qt.Key.Key_Space, Qt.Key.Key_Right, Qt.Key.Key_Down,
                       Qt.Key.Key_PageDown, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.doc_view.next_page()
                event.accept()
                return
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Backspace):
                self.doc_view.prev_page()
                event.accept()
                return
            if key == Qt.Key.Key_Home:
                self.doc_view.first_page()
                event.accept()
                return
            if key == Qt.Key.Key_End:
                self.doc_view.last_page()
                event.accept()
                return
        elif self.current_kind == "video":
            if key == Qt.Key.Key_Space:
                self.video_view.toggle_play()
                event.accept()
                return
            if key == Qt.Key.Key_Right:
                self.video_view.seek(5000)
                event.accept()
                return
            if key == Qt.Key.Key_Left:
                self.video_view.seek(-5000)
                event.accept()
                return
            if key == Qt.Key.Key_Up:
                self.video_view.change_volume(5)
                event.accept()
                return
            if key == Qt.Key.Key_Down:
                self.video_view.change_volume(-5)
                event.accept()
                return
            if key == Qt.Key.Key_M:
                self.video_view.toggle_mute()
                event.accept()
                return
            if key == Qt.Key.Key_L:
                self.video_view.toggle_loop()
                event.accept()
                return

        super().keyPressEvent(event)

    # ---------- 状态更新 ----------
    def _on_page_changed(self, current: int, total: int) -> None:
        self.page_label.setText(f"{current} / {total}")

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{int(round(zoom * 100))}%")

    def _update_controls(self) -> None:
        is_doc = self.current_kind == "document"
        for widget in (self.btn_prev, self.btn_next, self.page_label,
                       self.fit_combo, self.btn_zoom_in, self.btn_zoom_out, self.zoom_label,
                       self.btn_autoplay, self.btn_doc_loop, self.interval_spin):
            widget.setVisible(is_doc)

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于",
            "多媒体播放器\n\n"
            "支持 PDF、PPT/PPTX 演示文稿（通过 LibreOffice 转 PDF 渲染）和常见视频格式。\n\n"
            "快捷键：\n"
            "  Ctrl+O 打开文件    Ctrl+U 打开网络流    F11 全屏    Esc 退出全屏\n"
            "  空格 / → / PageDown 下一页    ← / PageUp 上一页\n"
            "  PDF 自动放映：A 键开关，L 键末页循环开关，工具栏可设置每页间隔秒数\n"
            "  Home / End 首尾页    Ctrl+滚轮或 Ctrl +/- 缩放\n"
            "  视频：空格播放暂停    ← / → 快退快进 5 秒    ↑ / ↓ 音量    M 静音    L 循环播放\n\n"
            f"文件收发服务：TCP {self._file_server.port} 端口"
            f"（接收目录 {self._file_server.storage}）\n\n"
            + f"远程受控：监听 {DEFAULT_LISTEN_PORT} 端口，等待控制服务端连接"
            + (f"；并已连出到 {DEFAULT_CONTROL_HOST}:{DEFAULT_CONTROL_PORT}"
               if DEFAULT_CONTROL_HOST else ""),
        )

    def _warn(self, message: str) -> None:
        self.unsetCursor()
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "提示", message)

    # ---------- 重启 ----------
    def _restart_args(self) -> list[str]:
        base = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        args: list[str] = []
        skip_value = False
        for item in base:
            if skip_value:
                skip_value = False
                continue
            if item == FILE_ARG:  # 去掉旧的文件参数，换成当前文件
                skip_value = True
                continue
            args.append(item)
        if self.current_path:
            args += [FILE_ARG, self.current_path]
        return args

    def restart(self) -> None:
        """重启软件：拉起新进程并恢复上次打开的文件，再退出当前进程。"""
        if not QProcess.startDetached(sys.executable, self._restart_args()):
            self._warn("重启失败：无法启动新的进程")
            return
        self._file_server.stop()  # 先释放端口，避免新进程抢不到
        self._stop_control_sessions()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event):  # noqa: N802
        self._file_server.stop()
        self._stop_control_sessions()
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(1000)
        self.video_view.stop()
        self.doc_view.close_document()
        super().closeEvent(event)
