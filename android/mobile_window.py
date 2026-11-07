"""安卓版主窗口（移动端界面）。

与桌面版 MainWindow 的关系：
- **复用**：受控连接、收文件、远程控制、定时播放（core/ 下的模块 + ui/video_view.py）
- **替换**：PDF 用 QtPdf（PyMuPDF 没有安卓轮子）、图片用单独的轮播组件
- **没有**：系统声音采集（安卓禁止采集其他应用的音频）、实时画面录制（需投屏授权）；
  收到这两类指令时会回报服务端，其余功能不受影响
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QStandardPaths, Qt, QTimer
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from android.image_slideshow import IMAGE_EXTENSIONS, ImageSlideshow
from android.pdf_view import PdfView
from core.control_client import ControlClient, ControlListener
from core.control_protocol import (
    DEFAULT_CONTROL_HOST,
    DEFAULT_CONTROL_PORT,
    DEFAULT_LISTEN_PORT,
)
from core.file_server import FileTransferServer
from core.schedule import (
    in_window,
    load_schedule,
    now_minutes,
    parse_hhmm,
    save_schedule,
    today,
    window_key,
)
from ui.video_view import VideoView, is_stream_url

PDF_EXTENSIONS = {".pdf"}
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".m4v", ".3gp", ".ts",
}
SCHEDULE_CHECK_MS = 15_000  # 定时检查间隔


class MobileWindow(QMainWindow):
    """安卓端播放器窗口：触控为主，底部一条控制条。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("多媒体播放器")
        self.setStyleSheet("background:#121214;color:#e8e8ea;")

        base = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation))
        base.mkdir(parents=True, exist_ok=True)
        self.schedule_file = base / "schedule.json"
        self.current_path: str | None = None
        self.current_kind: str | None = None

        self.file_server = FileTransferServer(storage=base / "received", parent=self)
        self.file_server.fileReceived.connect(self.open_path)
        self.file_server.statusChanged.connect(self._status)
        self.file_server.start()

        self._build_ui()
        self._start_control_client()
        self._init_schedule()
        self._status("就绪")

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        self._title = QLabel("未打开文件")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setStyleSheet("padding:8px;font-size:16px;")

        self.video_view = VideoView(self)
        self.pdf_view = PdfView(self)
        self.image_view = ImageSlideshow(self)
        self.video_view.errorOccurred.connect(
            lambda msg: self._status(f"播放出错：{msg}"))

        welcome = QLabel("用下方『打开』选择文件，或由控制服务端推送")
        welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(welcome)      # 0
        self.stack.addWidget(self.video_view)   # 1
        self.stack.addWidget(self.pdf_view)     # 2
        self.stack.addWidget(self.image_view)   # 3

        self.btn_open = QPushButton("打开")
        self.btn_play = QPushButton("播放")
        self.btn_prev = QPushButton("上一页")
        self.btn_next = QPushButton("下一页")
        self.btn_loop = QPushButton("循环")
        self.btn_loop.setCheckable(True)
        self.btn_full = QPushButton("全屏")
        self.btn_quit = QPushButton("退出")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(120)
        self.volume.valueChanged.connect(
            lambda v: self.video_view.audio.setVolume(v / 100))

        for button in (self.btn_open, self.btn_play, self.btn_prev, self.btn_next,
                       self.btn_loop, self.btn_full, self.btn_quit):
            button.setMinimumHeight(44)  # 触控友好
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.btn_open.clicked.connect(self.pick_file)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_next.clicked.connect(self.next_page)
        self.btn_loop.toggled.connect(
            lambda on: self.video_view.loop_btn.setChecked(on))
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        self.btn_quit.clicked.connect(self.close)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        for widget in (self.btn_open, self.btn_play, self.btn_prev, self.btn_next,
                       self.btn_loop, QLabel("音量"), self.volume,
                       self.btn_full, self.btn_quit):
            bar.addWidget(widget)

        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self._title)
        layout.addWidget(self.stack, 1)
        layout.addLayout(bar)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _status(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)

    # ---------- 打开与播放 ----------
    def pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择媒体文件", "",
            "媒体文件 (*.pdf *.mp4 *.mkv *.avi *.mov *.wmv *.png *.jpg *.jpeg "
            "*.bmp *.gif *.webp);;所有文件 (*)")
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        text = str(path or "").strip()
        if not text:
            return
        if is_stream_url(text):
            self._show_video(text)
        else:
            suffix = Path(text).suffix.lower()
            if suffix in VIDEO_EXTENSIONS:
                self._show_video(text)
            elif suffix in PDF_EXTENSIONS:
                self.video_view.close_media()
                self.stack.setCurrentWidget(self.pdf_view)
                if not self.pdf_view.load(text):
                    self._status("PDF 打开失败")
                    return
                self.current_kind = "document"
            elif suffix in IMAGE_EXTENSIONS:
                self.video_view.close_media()
                self.stack.setCurrentWidget(self.image_view)
                if not self.image_view.set_path(text):
                    self._status("图片打开失败")
                    return
                self.current_kind = "document"
            else:
                self._status(f"不支持的文件类型：{suffix or '未知'}")
                return
        self.current_path = text
        self._title.setText(os.path.basename(text))
        self._update_controls()

    def _show_video(self, path: str) -> None:
        self.pdf_view.close_document()
        self.image_view.close_document()
        self.stack.setCurrentWidget(self.video_view)
        self.video_view.load(path, autoplay=True)
        self.current_kind = "video"

    def _update_controls(self) -> None:
        self.btn_play.setText("暂停" if self.video_view.is_playing else "播放")

    def toggle_play(self) -> None:
        if self.current_kind == "video":
            self.video_view.toggle_play()
        else:
            self._doc().set_autoplay(not self._is_doc_playing())
        self._update_controls()

    def next_page(self) -> None:
        self._doc().next_page() if self.current_kind != "video" else self.video_view.seek(5000)

    def prev_page(self) -> None:
        self._doc().prev_page() if self.current_kind != "video" else self.video_view.seek(-5000)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _doc(self):
        """当前正在显示的文档类视图（PDF / 图片）。"""
        return (self.pdf_view if self.stack.currentWidget() is self.pdf_view
                else self.image_view)

    def _is_doc_playing(self) -> bool:
        doc = self._doc()
        return bool(getattr(doc, "timer", None) and doc.timer.isActive())

    # ---------- 远程受控 ----------
    def _start_control_client(self) -> None:
        self._control_sessions: list[ControlClient] = []
        self._autoshot_session: ControlClient | None = None
        self._shot_timer = QTimer(self)
        self._shot_timer.timeout.connect(self._send_autoshot)

        self._control_listener = ControlListener(port=DEFAULT_LISTEN_PORT, parent=self)
        self._control_listener.connectionAccepted.connect(self._on_control_connection)
        self._control_listener.statusChanged.connect(self._status)
        self._control_listener.start()

        if DEFAULT_CONTROL_HOST:  # 设置了环境变量才主动连出
            client = ControlClient(DEFAULT_CONTROL_HOST, DEFAULT_CONTROL_PORT,
                                   storage=self.file_server.storage, parent=self)
            self._wire_session(client)
            self._control_sessions.append(client)
            client.start()

    def _wire_session(self, session: ControlClient) -> None:
        session.fileReceived.connect(self.open_path)
        session.controlReceived.connect(self.apply_control)
        session.screenshotRequested.connect(self._send_screenshot)
        session.autoshotChanged.connect(
            lambda seconds, s=session: self._set_autoshot(s, seconds))
        session.recordChanged.connect(self._on_record_requested)
        session.fileDeleteRequested.connect(self._delete_media_file)
        session.statusChanged.connect(self._status)

    def _on_control_connection(self, sock) -> None:
        session = ControlClient(self._control_listener.host, DEFAULT_LISTEN_PORT,
                                storage=self.file_server.storage, parent=self,
                                sock=sock, reconnect=False)
        self._wire_session(session)
        self._control_sessions.append(session)
        session.start()

    def _set_autoshot(self, session: ControlClient, seconds: int) -> None:
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

    def _send_screenshot(self, session: ControlClient | None = None) -> None:
        """回传本应用自己的画面（安卓抓不到别的 App，只能抓自己）。"""
        session = session or (self._control_sessions[-1]
                              if self._control_sessions else None)
        if session is None:
            return
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.grab().save(buffer, "PNG")
        buffer.close()
        session.send_screenshot(bytes(buffer.data()), os.path.basename(self.current_path or ""))

    def _on_record_requested(self, session: ControlClient, fps: int, width: int) -> None:
        """安卓没有实时画面/声音采集能力，明确回报，避免服务端空等。"""
        if fps > 0:
            session.send_event("安卓端不支持实时画面与系统声音采集")
        self._status("安卓端不支持实时画面录制")

    def _delete_media_file(self, name: str, ticket) -> None:
        ok, msg = False, ""
        try:
            path = Path(self.file_server.storage) / Path(name).name
            if not path.is_file():
                msg = f"文件不存在：{name}"
            else:
                if (self.current_path and Path(self.current_path).name == path.name):
                    if self.current_kind == "video":
                        self.video_view.close_media()
                    else:
                        self._doc().close_document()
                    self.current_path = None
                    self.current_kind = None
                    self.stack.setCurrentIndex(0)
                path.unlink()
                ok, msg = True, f"已删除 {name}"
                self._status(msg)
        except OSError as exc:
            msg = f"删除失败：{exc}"
        finally:
            ticket.ok = ok
            ticket.msg = msg
            ticket.done.set()

    def _resolve_media_path(self, path: str) -> str:
        if os.path.isabs(path) or Path(path).is_file():
            return path
        candidate = Path(self.file_server.storage) / path
        return str(candidate) if candidate.is_file() else path

    def apply_control(self, action: str, args: dict) -> None:
        """执行服务端下发的远程操作（桌面版 apply_control 的安卓子集）。"""
        video, doc = self.video_view, self._doc()
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
            self.volume.setValue(max(0, min(100, int(args.get("value", 50)))))
        elif action == "mute":
            if video.audio.isMuted() != bool(args.get("on", True)):
                video.toggle_mute()
        elif action == "loop":
            self.btn_loop.setChecked(bool(args.get("on", True)))
        elif action == "fullscreen":
            self.showFullScreen() if bool(args.get("on", True)) else self.showNormal()
        elif action == "next":
            doc.next_page()
        elif action == "prev":
            doc.prev_page()
        elif action == "page":
            doc.goto_page(max(0, int(args.get("index", 1)) - 1))
        elif action == "autoplay":
            doc.set_autoplay(bool(args.get("on", True)))
        elif action == "interval":
            doc.set_interval(float(args.get("seconds", 5)))
        elif action == "docloop":
            doc.set_loop(bool(args.get("on", True)))
        elif action == "fit":
            doc.set_fit_mode(str(args.get("mode", "width")))
        elif action == "schedule":
            self.apply_schedule(list(args.get("items") or []))
        elif action == "restart":
            self._status("安卓端不支持远程重启")
        elif action == "quit":
            self.close()
            app = self._app()
            if app is not None:
                app.quit()
        self._update_controls()

    def _app(self):
        from PyQt6.QtWidgets import QApplication

        return QApplication.instance()

    # ---------- 定时播放 ----------
    def _init_schedule(self) -> None:
        self._schedule: list[dict] = load_schedule(self.schedule_file)
        self._schedule_active: dict[str, str] = {}
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(SCHEDULE_CHECK_MS)
        self._schedule_timer.timeout.connect(self._check_schedule)
        self._schedule_timer.start()

    def apply_schedule(self, items: list[dict]) -> None:
        from core.schedule import is_valid_item

        self._schedule = [dict(item) for item in items if is_valid_item(item)]
        self._schedule_active.clear()
        try:
            save_schedule(self.schedule_file, self._schedule)
        except OSError as exc:
            self._status(f"定时计划保存失败：{exc}")
        self._status(f"已更新定时播放计划：{len(self._schedule)} 条")

    def _check_schedule(self) -> None:
        minutes = now_minutes()
        day = today()
        inside: set[str] = set()
        for item in self._schedule:
            if not item.get("enabled", True):
                continue
            start = parse_hhmm(str(item.get("start") or item.get("time") or ""))
            if start is None:
                continue
            end = parse_hhmm(str(item.get("end") or ""))
            target = str(item.get("file") or item.get("name"))
            key = window_key(day, start, end, target)
            if not in_window(minutes, start, end):
                continue
            inside.add(key)
            if key in self._schedule_active:
                continue
            self._schedule_active[key] = day
            self._start_scheduled_item(item, target)
        for key in list(self._schedule_active):
            if key not in inside:
                del self._schedule_active[key]
                self._stop_scheduled_item()

    def _start_scheduled_item(self, item: dict, target: str) -> None:
        self.open_path(self._resolve_media_path(target))
        loop = bool(item.get("loop", True))
        if self.current_kind == "video":
            self.btn_loop.setChecked(loop)
            self.video_view.play()
        else:
            doc = self._doc()
            doc.set_loop(loop)
            doc.set_autoplay(True)
        self._status(f"定时播放：{os.path.basename(target)}")

    def _stop_scheduled_item(self) -> None:
        if self.current_kind == "video":
            self.video_view.pause()
        else:
            self._doc().set_autoplay(False)
        self._status("定时播放时段已结束，已暂停")

    # ---------- 退出 ----------
    def closeEvent(self, event):  # noqa: N802
        self._schedule_timer.stop()
        self._shot_timer.stop()
        self._control_listener.stop()
        for session in self._control_sessions:
            session.stop()
        self._control_sessions.clear()
        self.file_server.stop()
        self.video_view.close_media()
        super().closeEvent(event)
