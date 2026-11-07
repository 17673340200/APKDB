"""采集本机正在播放的系统声音（Windows WASAPI 回环），转成 16bit PCM 供回传。

依赖 soundcard：pip install soundcard（仅客户端需要，用于录制时带声音）。
采集的是"扬声器里正在放的声音"，因此播放器播什么就录到什么。
"""

from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal

DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
CHUNK_MS = 100  # 每块时长（毫秒），越小越实时、包越多


class AudioCapture(QThread):
    """后台采集系统声音，按块发出 s16le PCM。"""

    chunkReady = pyqtSignal(bytes)  # 一段 PCM（16bit 小端）
    failed = pyqtSignal(str)  # 采集不可用 / 中断原因

    def __init__(self, rate: int = DEFAULT_RATE, channels: int = DEFAULT_CHANNELS,
                 parent=None):
        super().__init__(parent)
        self.rate = int(rate)
        self.channels = int(channels)
        self._running = True

    def stop(self) -> None:
        self._running = False
        self.wait(3000)

    def run(self) -> None:  # noqa: D102
        try:
            import numpy as np
            import soundcard
        except ImportError as exc:
            self.failed.emit(f"缺少声音采集依赖（pip install soundcard）：{exc}")
            return
        try:
            speaker = soundcard.default_speaker()
            if speaker is None:
                self.failed.emit("未找到扬声器设备，无法采集系统声音")
                return
            mic = soundcard.get_microphone(id=speaker.id, include_loopback=True)
        except Exception as exc:  # soundcard 抛多种异常，统一提示
            self.failed.emit(f"打开系统声音采集失败：{exc}")
            return

        frames = max(1, self.rate * CHUNK_MS // 1000)
        try:
            with mic.recorder(samplerate=self.rate, channels=self.channels) as recorder:
                while self._running:
                    data = recorder.record(numframes=frames)
                    if data is None or len(data) == 0:
                        continue
                    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    self.chunkReady.emit(pcm)
        except Exception as exc:
            if self._running:
                self.failed.emit(f"声音采集中断：{exc}")
