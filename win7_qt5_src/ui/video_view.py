"""视频播放视图：QMediaPlayer + QVideoWidget + 控制条。

Qt5 与 Qt6 的差异在这里集中处理：Qt5 没有 QAudioOutput（音量/静音直接在 player 上）、
用 QMediaContent 装载媒体、循环要靠 QMediaPlaylist。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtMultimedia import QMediaContent, QMediaPlaylist, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

SEEK_STEP = 5000  # 毫秒
CONTROL_HIDE_DELAY = 2500  # 毫秒
STREAM_SCHEMES = {
    "rtsp", "rtspt", "rtmp", "rtmps", "rtp", "udp", "srt",
    "http", "https", "mms", "mmsh", "mmst",
}


def is_stream_url(path: str) -> bool:
    """判断是否为网络流地址（rtsp / rtmp / http 等）。

    要求带 "://"，避免把 "C:\\xxx.mp4" 这类本地路径误判为流。
    """
    text = (path or "").strip()
    if "://" not in text:
        return False
    return QUrl(text).scheme().lower() in STREAM_SCHEMES


def _format_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    total = ms // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class VideoView(QWidget):
    """带控制条的视频播放组件。"""

    fullscreenRequested = pyqtSignal()
    errorOccurred = pyqtSignal(str)
    playingChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Qt5 必须用 VideoSurface 标记，画面才会渲染到 QVideoWidget
        self.player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
        self.playlist = QMediaPlaylist(self)
        self.player.setPlaylist(self.playlist)
        self.playlist.setPlaybackMode(QMediaPlaylist.CurrentItemOnce)

        self.setStyleSheet("background:#000000;")  # 视频比例与窗口不一致时的黑边

        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background:#000000;")
        self.player.setVideoOutput(self.video_widget)

        self._live = False
        self._auto_hide = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(CONTROL_HIDE_DELAY)
        self._hide_timer.timeout.connect(lambda: self.controls.setVisible(False))

        self.controls = self._build_controls()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video_widget, 1)
        layout.addWidget(self.controls)

        self.setMouseTracking(True)
        self.video_widget.setMouseTracking(True)

        self.player.stateChanged.connect(self._on_state_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.error.connect(self._on_error)

    # ---------- 控制条 ----------
    def _build_controls(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet("background:#1b1b1e;color:#e8e8ea;")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        self.play_btn = QPushButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.setFixedSize(32, 28)
        self.play_btn.clicked.connect(self.toggle_play)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setMinimumWidth(110)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setTracking(False)
        self.slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.slider.sliderMoved.connect(self.player.setPosition)

        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.volume.valueChanged.connect(self.player.setVolume)

        self.mute_btn = QPushButton("静音")
        self.mute_btn.setFixedWidth(48)
        self.mute_btn.clicked.connect(self.toggle_mute)

        self.loop_btn = QPushButton("循环")
        self.loop_btn.setCheckable(True)
        self.loop_btn.setFixedWidth(48)
        self.loop_btn.setStyleSheet(
            "QPushButton:checked{background:#2f6fed;color:white;border:none;border-radius:4px;}"
        )
        self.loop_btn.toggled.connect(self._on_loop_toggled)

        self.full_btn = QPushButton("全屏")
        self.full_btn.setFixedWidth(48)
        self.full_btn.clicked.connect(self.fullscreenRequested.emit)

        for widget in (self.play_btn, self.slider, self.volume, self.mute_btn,
                       self.loop_btn, self.full_btn):
            widget.setFocusPolicy(Qt.NoFocus)

        layout.addWidget(self.play_btn)
        layout.addWidget(self.time_label)
        layout.addWidget(self.slider, 1)
        layout.addWidget(QLabel("音量"))
        layout.addWidget(self.volume)
        layout.addWidget(self.mute_btn)
        layout.addWidget(self.loop_btn)
        layout.addWidget(self.full_btn)
        self.player.setVolume(80)
        return box

    # ---------- 播放控制 ----------
    def load(self, path: str, autoplay: bool = True) -> None:
        self.player.stop()
        self._live = is_stream_url(path)  # 直播流没有时长，进度条要禁用
        source = QUrl(path.strip()) if self._live else QUrl.fromLocalFile(path)
        self.playlist.clear()
        self.playlist.addMedia(QMediaContent(source))
        self.playlist.setCurrentIndex(0)
        self._on_loop_toggled(self.loop_btn.isChecked())
        self.slider.setValue(0)
        if autoplay:
            self.play()
        else:
            self.player.setPosition(0)

    def play(self) -> None:
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def position(self) -> int:
        return int(self.player.position())

    def duration(self) -> int:
        return int(self.player.duration())

    def set_position(self, ms: int) -> None:
        """跳到绝对位置（毫秒）。"""
        total = self.player.duration()
        target = max(0, int(ms))
        if total and total > 0:
            target = min(target, int(total))
        self.player.setPosition(target)

    def toggle_play(self) -> None:
        if self.player.state() == QMediaPlayer.PlayingState:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self.player.stop()

    def close_media(self) -> None:
        """停止并释放当前媒体文件（否则 Windows 上文件被占用，删不掉）。"""
        self.player.stop()
        self.playlist.clear()

    def seek(self, offset_ms: int) -> None:
        target = self.player.position() + offset_ms
        target = max(0, min(self.player.duration() or 0, target))
        self.player.setPosition(target)

    def change_volume(self, delta: int) -> None:
        self.volume.setValue(max(0, min(100, self.volume.value() + delta)))

    def toggle_mute(self) -> None:
        muted = not self.player.isMuted()
        self.player.setMuted(muted)
        self.mute_btn.setText("取消" if muted else "静音")

    def toggle_loop(self) -> None:
        self.loop_btn.setChecked(not self.loop_btn.isChecked())

    def _on_loop_toggled(self, enabled: bool) -> None:
        self.playlist.setPlaybackMode(
            QMediaPlaylist.Loop if enabled else QMediaPlaylist.CurrentItemOnce
        )

    @property
    def is_playing(self) -> bool:
        return self.player.state() == QMediaPlayer.PlayingState

    # ---------- 全屏时的控制条自动隐藏 ----------
    def set_fullscreen(self, enabled: bool) -> None:
        """进入/退出全屏：控制条改为自动隐藏，按钮文案同步。"""
        self.full_btn.setText("退出" if enabled else "全屏")
        self.set_controls_auto_hide(enabled)

    def set_controls_auto_hide(self, enabled: bool) -> None:
        self._auto_hide = enabled
        if not enabled:
            self._hide_timer.stop()
            self.controls.setVisible(True)
        else:
            self._hide_timer.start()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._auto_hide:
            self.controls.setVisible(True)
            self._hide_timer.start()
        super().mouseMoveEvent(event)

    # ---------- 信号槽 ----------
    def _on_state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        icon = QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay
        self.play_btn.setIcon(self.style().standardIcon(icon))
        self.playingChanged.emit(playing)

    def _on_position_changed(self, position: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self._update_time(position, self.player.duration())

    def _on_duration_changed(self, duration: int) -> None:
        self.slider.setRange(0, duration if duration > 0 else 0)
        self.slider.setEnabled(duration > 0 or not self._live)
        self._update_time(self.player.position(), duration)

    def _on_error(self, error) -> None:
        self.errorOccurred.emit(self.player.errorString() or str(error))

    def _update_time(self, position: int, duration: int) -> None:
        total = "直播" if (self._live and duration <= 0) else _format_time(duration)
        self.time_label.setText(f"{_format_time(position)} / {total}")
