"""控制服务端图形界面（PyQt5）。

    python control_server_ui.py                 # 默认 0.0.0.0:50518
    python control_server_ui.py --port 6000 --shots D:\\shots

左侧选择播放器客户端，右侧分区远程控制；截屏回传后直接在界面预览。
"""

from __future__ import annotations
import argparse
import sys
import threading
import time
from os import system as open_folders
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QImage, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from control_server import ControlServer
from core.control_protocol import (
    DEFAULT_CONTROL_PORT,
    DEFAULT_FPS,
    DEFAULT_FRAME_WIDTH,
    DEFAULT_LISTEN_PORT,
    DEFAULT_SHOT_DIR,
    MAX_FPS,
)


class AddClientDialog(QDialog):
    """添加客户端：填名称、地址、端口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加客户端")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("可留空，默认用地址")
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("如 192.168.1.20")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(DEFAULT_LISTEN_PORT)

        form = QFormLayout(self)
        form.addRow("名称：", self.name_edit)
        form.addRow("地址：", self.host_edit)
        form.addRow("端口：", self.port_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, str, int]:
        return (self.name_edit.text().strip(), self.host_edit.text().strip(),
                self.port_spin.value())


class _Bridge(QObject):
    """把服务端工作线程里的回调转成信号，安全地在界面线程里更新 UI。"""

    logReceived = pyqtSignal(str)
    shotReceived = pyqtSignal(str)
    frameReceived = pyqtSignal(int, bytes)  # 客户端编号、JPEG 画面帧
    filesReceived = pyqtSignal(int, object)  # 客户端编号、文件列表


class ServerWindow(QMainWindow):
    """控制服务端主窗口。"""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_CONTROL_PORT,
                 shot_dir: str | Path = DEFAULT_SHOT_DIR, book: str | Path = "clients.json"):
        super().__init__()
        self.setWindowTitle(f"播放器控制服务端 - {host}:{port}")
        self.resize(1080, 720)

        self._bridge = _Bridge()
        self._bridge.logReceived.connect(self._append_log)
        self._bridge.shotReceived.connect(self._show_shot)
        self._bridge.frameReceived.connect(self._show_frame)
        self._bridge.filesReceived.connect(self._show_files)

        self._server = ControlServer(host=host, port=port, shot_dir=shot_dir, book=book,
                                     log=self._bridge.logReceived.emit)
        self._server.on_shot = lambda path: self._bridge.shotReceived.emit(str(path))
        self._server.on_frame = lambda cid, data: self._bridge.frameReceived.emit(cid, data)
        self._server.on_files = lambda cid, files: self._bridge.filesReceived.emit(cid, files)
        self._current_id: int | None = None
        self._last_frame: bytes = b""

        self._build_ui()
        self._server.start_background()
        self._append_log(f"监听 {host}:{port}，画面保存目录 {self._server.shot_dir}")

        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._refresh_clients)
        self._poll.start()

    # ---------- 界面搭建 ----------
    def _build_ui(self) -> None:
        self.client_list = QListWidget()
        self.client_list.setFixedWidth(240)
        self.client_list.setSelectionMode(
            QAbstractItemView.ExtendedSelection)  # 可多选，批量推送
        self.client_list.currentItemChanged.connect(self._on_client_selected)
        self.client_list.itemSelectionChanged.connect(self._on_selection_changed)

        self.btn_add = QPushButton("添加客户端")
        self.btn_add.clicked.connect(self._add_client)
        self.btn_remove = QPushButton("移除选中")
        self.btn_remove.clicked.connect(self._remove_client)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.btn_add)
        left_layout.addWidget(self.btn_remove)
        left_layout.addWidget(self.client_list)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(140)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self._build_client_box())
        right_layout.addLayout(self._build_video_box())
        right_layout.addLayout(self._build_doc_box())
        right_layout.addWidget(self._build_file_box())
        right_layout.addWidget(self._build_remote_files_box())
        right_layout.addWidget(self._build_batch_box())
        right_layout.addWidget(self._build_shot_box())
        right_layout.addStretch(1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(splitter)
        layout.addWidget(QLabel("日志"))
        layout.addWidget(self.log_view)
        self.setCentralWidget(central)

    def _build_client_box(self) -> QGroupBox:
        box = QGroupBox("客户端")
        layout = QHBoxLayout(box)
        self.btn_restart = QPushButton("重启客户端")
        self.btn_quit = QPushButton("退出客户端")
        self.btn_restart.clicked.connect(lambda: self._control("restart"))
        self.btn_quit.clicked.connect(lambda: self._control("quit"))
        layout.addWidget(self.btn_restart)
        layout.addWidget(self.btn_quit)
        layout.addStretch(1)
        return box

    def _build_video_box(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        play_box = QGroupBox("播放控制")
        play = QHBoxLayout(play_box)
        for text, action in (("播放", "play"), ("暂停", "pause"), ("停止", "stop")):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action: self._control(a))
            play.addWidget(button)
        back = QPushButton("快退 5s")
        back.clicked.connect(lambda: self._control("seek", ms=-5000))
        forward = QPushButton("快进 5s")
        forward.clicked.connect(lambda: self._control("seek", ms=5000))
        play.addWidget(back)
        play.addWidget(forward)

        self.chk_mute = QCheckBox("静音")
        self.chk_mute.toggled.connect(lambda on: self._control("mute", on=on))
        self.chk_loop = QCheckBox("循环")
        self.chk_loop.toggled.connect(lambda on: self._control("loop", on=on))
        self.chk_full = QCheckBox("全屏")
        self.chk_full.toggled.connect(lambda on: self._control("fullscreen", on=on))
        play.addWidget(self.chk_mute)
        play.addWidget(self.chk_loop)
        play.addWidget(self.chk_full)

        vol_box = QGroupBox("音量")
        vol = QHBoxLayout(vol_box)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(50)
        self.volume.setFixedWidth(120)
        self.volume.sliderReleased.connect(
            lambda: self._control("volume", value=self.volume.value()))
        self.volume_label = QLabel("50")
        self.volume.valueChanged.connect(lambda v: self.volume_label.setText(str(v)))
        vol.addWidget(self.volume)
        vol.addWidget(self.volume_label)

        layout.addWidget(play_box)
        layout.addWidget(vol_box)
        layout.addStretch(1)
        return layout

    def _build_doc_box(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        page_box = QGroupBox("文档翻页")
        page = QHBoxLayout(page_box)
        prev = QPushButton("上一页")
        prev.clicked.connect(lambda: self._control("prev"))
        next_ = QPushButton("下一页")
        next_.clicked.connect(lambda: self._control("next"))
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 9999)
        self.page_spin.setFixedWidth(70)
        goto = QPushButton("跳页")
        goto.clicked.connect(lambda: self._control("page", index=self.page_spin.value()))
        page.addWidget(prev)
        page.addWidget(next_)
        page.addWidget(QLabel("页码"))
        page.addWidget(self.page_spin)
        page.addWidget(goto)

        show_box = QGroupBox("自动放映")
        show = QHBoxLayout(show_box)
        self.chk_autoplay = QCheckBox("放映")
        self.chk_autoplay.toggled.connect(lambda on: self._control("autoplay", on=on))
        self.chk_docloop = QCheckBox("末页循环")
        self.chk_docloop.toggled.connect(lambda on: self._control("docloop", on=on))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 3600)
        self.interval_spin.setValue(5)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setFixedWidth(80)
        apply_interval = QPushButton("设置间隔")
        apply_interval.clicked.connect(
            lambda: self._control("interval", seconds=self.interval_spin.value()))
        show.addWidget(self.chk_autoplay)
        show.addWidget(self.chk_docloop)
        show.addWidget(self.interval_spin)
        show.addWidget(apply_interval)

        fit_box = QGroupBox("显示")
        fit = QHBoxLayout(fit_box)
        self.fit_combo = QComboBox()
        self.fit_combo.addItem("适应宽度", "width")
        self.fit_combo.addItem("整页可见", "page")
        self.fit_combo.currentIndexChanged.connect(
            lambda: self._control("fit", mode=self.fit_combo.currentData()))
        fit.addWidget(self.fit_combo)

        layout.addWidget(page_box)
        layout.addWidget(show_box)
        layout.addWidget(fit_box)
        layout.addStretch(1)
        return layout

    def _build_file_box(self) -> QGroupBox:
        box = QGroupBox("文件与流")
        layout = QHBoxLayout(box)
        push = QPushButton("推送文件…")
        push.clicked.connect(self._push_file)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("本地文件路径 或 rtsp:// / http:// 流地址")
        open_btn = QPushButton("让客户端打开")
        open_btn.clicked.connect(self._open_media)
        layout.addWidget(push)
        layout.addWidget(self.path_edit, 1)
        layout.addWidget(open_btn)
        return box

    def _build_remote_files_box(self) -> QGroupBox:
        """客户端接收目录里的文件：列出来，选一个让客户端播放。"""
        box = QGroupBox("客户端文件（接收目录）")
        layout = QHBoxLayout(box)

        self.remote_files = QListWidget()
        self.remote_files.setFixedHeight(120)
        self.remote_files.itemDoubleClicked.connect(lambda _: self._play_remote_file())

        buttons = QVBoxLayout()
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self._refresh_remote_files)
        play = QPushButton("播放选中")
        play.clicked.connect(self._play_remote_file)
        delete = QPushButton("删除选中")
        delete.setToolTip("删除客户端接收目录里的该文件（客户端本地文件同步删除）")
        delete.clicked.connect(self._delete_remote_file)
        buttons.addWidget(refresh)
        buttons.addWidget(play)
        buttons.addWidget(delete)
        buttons.addStretch(1)

        layout.addWidget(self.remote_files, 1)
        layout.addLayout(buttons)
        return box

    def _refresh_remote_files(self) -> None:
        cid = self._current_id
        if cid is None:
            self._append_log("请先选择一个客户端")
            return

        def work() -> None:
            try:
                files = self._server.request_file_list(cid)
            except (KeyError, OSError) as exc:
                self._bridge.logReceived.emit(f"获取文件列表失败：{exc}")
                return
            self._bridge.filesReceived.emit(cid, files)

        threading.Thread(target=work, daemon=True).start()

    def _show_files(self, cid: int, files: list) -> None:
        if self._current_id is not None and cid != self._current_id:
            return
        self.remote_files.clear()
        for item in files:
            size = int(item.get("size", 0))
            text = f"{item.get('name')}  （{size / 1024:.1f} KB）"
            entry = QListWidgetItem(text)
            entry.setData(Qt.UserRole, item.get("name"))
            self.remote_files.addItem(entry)
        self._append_log(f"#{cid} 接收目录共 {len(files)} 个文件")

    def _play_remote_file(self) -> None:
        item = self.remote_files.currentItem()
        if item is None:
            self._append_log("请先在文件列表里选中一个文件")
            return
        if self._current_id is None:
            self._append_log("请先选择一个客户端")
            return
        try:
            self._server.play_file(self._current_id, str(item.data(Qt.UserRole)))
            self._append_log(f"→ #{self._current_id} 播放 {item.text()}")
        except (KeyError, OSError) as exc:
            self._append_log(f"播放失败：{exc}")

    def _delete_remote_file(self) -> None:
        """删除客户端接收目录里的选中文件（客户端本地文件也一起删），随后刷新列表。"""
        item = self.remote_files.currentItem()
        if item is None:
            self._append_log("请先在文件列表里选中一个文件")
            return
        cid = self._current_id
        if cid is None:
            self._append_log("请先选择一个客户端")
            return
        name = str(item.data(Qt.UserRole))
        answer = QMessageBox.question(
            self, "删除文件",
            f"确定删除客户端 #{cid} 接收目录里的《{name}》吗？\n"
            "该客户端本机上的这个文件也会被删除，且不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        def work() -> None:
            try:
                ok, msg = self._server.delete_file(cid, name)
            except (KeyError, OSError, ValueError) as exc:
                self._bridge.logReceived.emit(f"删除失败：{exc}")
                return
            self._bridge.logReceived.emit(f"#{cid} {msg}")  # msg 里已含成功/失败说明
            # 重新拉一次列表，界面与客户端实际内容保持一致
            try:
                self._bridge.filesReceived.emit(cid, self._server.request_file_list(cid))
            except (KeyError, OSError) as exc:
                self._bridge.logReceived.emit(f"刷新文件列表失败：{exc}")

        self._append_log(f"正在删除 #{cid} 的 {name} …")
        threading.Thread(target=work, daemon=True).start()

    def _build_batch_box(self) -> QGroupBox:
        """批量操作：一次推送给多个客户端、多机同步播放、下发定时播放计划。"""
        box = QGroupBox("批量 / 同步 / 定时（作用到所有选中的客户端）")
        layout = QVBoxLayout(box)

        row0 = QHBoxLayout()
        select_all = QPushButton("全选在线")
        select_all.clicked.connect(self._select_all_online)
        clear = QPushButton("清空选择")
        clear.clicked.connect(self.client_list.clearSelection)
        self.lbl_selected = QLabel("未选择客户端")
        row0.addWidget(select_all)
        row0.addWidget(clear)
        row0.addWidget(self.lbl_selected)
        row0.addStretch(1)

        row1 = QHBoxLayout()
        push_many = QPushButton("推送文件给选中…")
        push_many.clicked.connect(self._push_file_selected)
        self.sync_delay = QSpinBox()
        self.sync_delay.setRange(1, 60)
        self.sync_delay.setValue(3)
        self.sync_delay.setSuffix(" 秒后起播")
        sync_btn = QPushButton("同步播放")
        sync_btn.setToolTip("内容填本地文件则先推送给各端；也可填客户端接收目录里已有的文件名")
        sync_btn.clicked.connect(self._sync_play_selected)
        row1.addWidget(push_many)
        row1.addWidget(QLabel("同步："))
        row1.addWidget(self.sync_delay)
        row1.addWidget(sync_btn)
        row1.addStretch(1)

        row2 = QHBoxLayout()
        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm")
        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm")
        self.end_edit.setTime(self.end_edit.time().addSecs(3600))
        self.chk_end = QCheckBox("到时停止")
        self.chk_end.setChecked(True)
        self.chk_end.toggled.connect(self.end_edit.setEnabled)
        self.chk_loop = QCheckBox("循环")
        self.chk_loop.setChecked(True)
        add_item = QPushButton("加入定时计划")
        add_item.clicked.connect(self._add_schedule_item)
        row2.addWidget(QLabel("定时："))
        row2.addWidget(QLabel("开始"))
        row2.addWidget(self.start_edit)
        row2.addWidget(QLabel("结束"))
        row2.addWidget(self.end_edit)
        row2.addWidget(self.chk_end)
        row2.addWidget(self.chk_loop)
        row2.addWidget(add_item)
        row2.addStretch(1)

        row3 = QHBoxLayout()
        self.schedule_list = QListWidget()
        self.schedule_list.setFixedHeight(90)
        sched_buttons = QVBoxLayout()
        remove_item = QPushButton("删除选中项")
        remove_item.clicked.connect(self._remove_schedule_item)
        clear_sched = QPushButton("清空并下发")
        clear_sched.clicked.connect(self._clear_schedule)
        sched_buttons.addWidget(remove_item)
        sched_buttons.addWidget(clear_sched)
        sched_buttons.addStretch(1)
        row3.addWidget(self.schedule_list, 1)
        row3.addLayout(sched_buttons)

        layout.addLayout(row0)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        return box

    # ---------- 批量操作 ----------
    def _selected_ids(self) -> list[int]:
        """当前选中的在线客户端编号（没多选时退化为当前客户端）。"""
        ids: list[int] = []
        for item in self.client_list.selectedItems():
            data = item.data(Qt.UserRole)
            if isinstance(data, dict) and data.get("kind") == "online":
                ids.append(int(data["id"]))
        if not ids and self._current_id is not None:
            ids = [self._current_id]
        return ids

    def _select_all_online(self) -> None:
        for row in range(self.client_list.count()):
            item = self.client_list.item(row)
            data = item.data(Qt.UserRole)
            item.setSelected(isinstance(data, dict) and data.get("kind") == "online")

    def _on_selection_changed(self) -> None:
        ids = self._selected_ids()
        self.lbl_selected.setText(f"已选 {len(ids)} 个客户端" if ids else "未选择客户端")
        if ids and ids[-1] != self._current_id:  # 最后选中的作为"当前客户端"
            self._current_id = ids[-1]
            self._append_log(f"当前客户端 #{self._current_id}")
            self._refresh_remote_files()

    def _batch_content(self) -> str:
        """批量操作的内容：优先"文件与流"输入框，其次客户端文件列表里的选中项。"""
        text = self.path_edit.text().strip()
        if text:
            return text
        item = self.remote_files.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else ""

    def _push_file_selected(self) -> None:
        content = self._batch_content()
        source = Path(content)
        if not source.is_file():
            chosen, _ = QFileDialog.getOpenFileName(self, "选择要推送给多个客户端的文件")
            if not chosen:
                return
            source = Path(chosen)
            self.path_edit.setText(chosen)
        ids = self._selected_ids()
        if not ids:
            self._append_log("请先在左侧选择客户端")
            return

        def work() -> None:
            try:
                results = self._server.push_file_many(ids, source)
            except (FileNotFoundError, OSError, ValueError) as exc:
                self._bridge.logReceived.emit(f"批量推送失败：{exc}")
                return
            ok = [cid for cid, err in results.items() if not err]
            failed = [f"#{cid} {err}" for cid, err in results.items() if err]
            self._bridge.logReceived.emit(
                f"批量推送 {source.name} 完成：成功 {len(ok)}/{len(ids)}"
                + ("；失败 " + "，".join(failed) if failed else ""))

        self._append_log(f"正在推送 {source.name} 给 {len(ids)} 个客户端…")
        threading.Thread(target=work, daemon=True).start()

    def _sync_play_selected(self) -> None:
        ids = self._selected_ids()
        content = self._batch_content()
        if not ids or not content:
            self._append_log("请先选择客户端，并填写要同步播放的文件（本地路径或客户端已有文件名）")
            return
        delay = self.sync_delay.value()
        source = Path(content)

        def work() -> None:
            try:
                if source.is_file():
                    start_at = self._server.sync_play(ids, path=source, delay=delay)
                else:
                    start_at = self._server.sync_play(ids, name=content, delay=delay)
            except (FileNotFoundError, OSError, ValueError) as exc:
                self._bridge.logReceived.emit(f"同步播放失败：{exc}")
                return
            self._bridge.logReceived.emit(
                f"{len(ids)} 个客户端将在 "
                f"{time.strftime('%H:%M:%S', time.localtime(start_at))} 同步起播 {Path(content).name}")

        self._append_log(f"正在下发同步播放指令给 {len(ids)} 个客户端…")
        threading.Thread(target=work, daemon=True).start()

    def _add_schedule_item(self) -> None:
        """配置一个播放时段（开始 → 结束），加入计划并下发给选中的客户端。"""
        ids = self._selected_ids()
        content = self._batch_content()
        if not ids or not content:
            self._append_log("请先选择客户端，并填写要定时播放的文件")
            return
        source = Path(content)
        item = {
            "start": self.start_edit.time().toString("HH:mm"),
            "end": self.end_edit.time().toString("HH:mm") if self.chk_end.isChecked() else "",
            "file": source.name if source.is_file() else content,
            "loop": self.chk_loop.isChecked(),
            "enabled": True,
        }
        items = [entry for entry in self._server.schedule if not self._same_item(entry, item)]
        items.append(item)
        items.sort(key=lambda entry: str(entry.get("start")))
        self._server.set_schedule(ids, items)
        self._refresh_schedule_list()
        self._append_log(f"已向 {len(ids)} 个客户端下发定时计划：{item['start']} → "
                         f"{item['end'] or '当天结束'} 播放 {item['file']}（共 {len(items)} 项）")

    def _remove_schedule_item(self) -> None:
        row = self.schedule_list.currentRow()
        items = list(self._server.schedule)
        if not 0 <= row < len(items):
            self._append_log("请先在计划列表里选中一项")
            return
        removed = items.pop(row)
        ids = self._selected_ids()
        if not ids:
            self._append_log("请先在左侧选择客户端")
            return
        self._server.set_schedule(ids, items)
        self._refresh_schedule_list()
        self._append_log(f"已删除定时项 {removed.get('start')} → {removed.get('end') or '当天结束'}")

    def _clear_schedule(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._append_log("请先在左侧选择客户端")
            return
        self._server.set_schedule(ids, [])
        self._refresh_schedule_list()
        self._append_log(f"已清空 {len(ids)} 个客户端的定时计划")

    @staticmethod
    def _same_item(a: dict, b: dict) -> bool:
        return (str(a.get("start")) == str(b.get("start"))
                and str(a.get("end") or "") == str(b.get("end") or "")
                and str(a.get("file")) == str(b.get("file")))

    def _refresh_schedule_list(self) -> None:
        """显示当前下发的定时计划配置。"""
        self.schedule_list.clear()
        for item in self._server.schedule:
            end = item.get("end") or "当天结束"
            loop = "循环" if item.get("loop", True) else "单次"
            self.schedule_list.addItem(
                f"{item.get('start')} → {end}  {item.get('file')}  [{loop}]")

    def _build_shot_box(self) -> QGroupBox:
        box = QGroupBox("实时画面（客户端录屏）")
        layout = QHBoxLayout(box)

        controls = QVBoxLayout()
        row1 = QHBoxLayout()
        start = QPushButton("开始实时画面")
        start.clicked.connect(self._start_record)
        stop = QPushButton("停止")
        stop.clicked.connect(self._stop_record)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, MAX_FPS)
        self.fps_spin.setValue(DEFAULT_FPS)
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.setFixedWidth(80)
        row1.addWidget(start)
        row1.addWidget(stop)
        row1.addWidget(QLabel("帧率"))
        row1.addWidget(self.fps_spin)

        row2 = QHBoxLayout()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(320, 1920)
        self.width_spin.setSingleStep(80)
        self.width_spin.setValue(DEFAULT_FRAME_WIDTH)
        self.width_spin.setPrefix("宽 ")
        self.width_spin.setFixedWidth(90)
        save_frame = QPushButton("保存当前画面")
        save_frame.clicked.connect(self._save_current_frame)
        open_dir = QPushButton("打开目录")
        open_dir.setToolTip("打开本次录制的文件夹（未录制时打开 saves 目录）")
        open_dir.clicked.connect(self._open_record_dir)
        row2.addWidget(self.width_spin)
        row2.addWidget(save_frame)
        row2.addWidget(open_dir)

        row3 = QHBoxLayout()
        self.chk_audio = QCheckBox("录制系统声音")
        self.chk_audio.setToolTip(
            "把客户端正在播放的声音一起录进 MP4（客户端需装 soundcard：\n"
            "pip install soundcard）。在开始实时画面前勾选；中途改动需重启实时画面")
        row3.addWidget(self.chk_audio)
        row3.addStretch(1)

        row4 = QHBoxLayout()
        self.chk_save_video = QCheckBox("录制到文件夹（MP4 视频）")
        self.chk_save_video.setToolTip(
            "先点『开始实时画面』，再勾选：把收到的画面录成 record.mp4；\n"
            "取消勾选（或点『停止』）即生成视频，录制文件夹在 saves/ 下")
        self.chk_save_video.toggled.connect(self._toggle_recording)
        row4.addWidget(self.chk_save_video)
        row4.addStretch(1)

        controls.addLayout(row1)
        controls.addLayout(row2)
        controls.addLayout(row3)
        controls.addLayout(row4)
        controls.addStretch(1)
        controls.setSpacing(8)

        self.shot_preview = QLabel("实时画面\n点「开始实时画面」查看客户端屏幕")
        self.shot_preview.setFixedSize(480, 270)
        self.shot_preview.setAlignment(Qt.AlignCenter)
        self.shot_preview.setStyleSheet("background:#000000;color:#888888;")

        layout.addLayout(controls)
        layout.addWidget(self.shot_preview)
        layout.addStretch(1)
        return box

    # ---------- 客户端列表 ----------
    def _refresh_clients(self) -> None:
        clients = self._server.clients()
        signature = ([(item["id"], item["name"], item["addr"]) for item in clients],
                     [(e["host"], e["port"], e["name"]) for e in self._server.book])
        if signature == getattr(self, "_last_signature", None):
            return
        self._last_signature = signature

        self.client_list.blockSignals(True)
        keep = {int(item.data(Qt.UserRole)["id"])
                for item in self.client_list.selectedItems()
                if isinstance(item.data(Qt.UserRole), dict)
                and item.data(Qt.UserRole).get("kind") == "online"}
        self.client_list.clear()
        online_hosts = set()
        for item in clients:
            online_hosts.add(item["host"])
            if item["book_host"]:
                online_hosts.add(item["book_host"])
            entry = QListWidgetItem(f"#{item['id']}  {item['name']}  {item['addr']}  [在线]")
            entry.setData(Qt.UserRole, {"kind": "online", "id": item["id"]})
            self.client_list.addItem(entry)
        for item in self._server.book:  # 已登记但当前离线
            if item["host"] in online_hosts:
                continue
            entry = QListWidgetItem(
                f"{item['name']}  {item['host']}:{item['port']}  [离线]")
            entry.setData(Qt.UserRole,
                          {"kind": "book", "host": item["host"], "port": item["port"]})
            entry.setForeground(Qt.gray)
            self.client_list.addItem(entry)
        for row in range(self.client_list.count()):  # 恢复多选状态
            item = self.client_list.item(row)
            data = item.data(Qt.UserRole)
            if isinstance(data, dict) and data.get("kind") == "online" and data["id"] in keep:
                item.setSelected(True)
        self.client_list.blockSignals(False)
        self.lbl_selected.setText(
            f"已选 {len(keep)} 个客户端" if keep else "未选择客户端")

        if self._current_id not in {item["id"] for item in clients}:
            self._current_id = None
            for row in range(self.client_list.count()):
                data = self.client_list.item(row).data(Qt.UserRole)
                if isinstance(data, dict) and data["kind"] == "online":
                    self.client_list.setCurrentRow(row)
                    break

    def _on_client_selected(self, current, previous) -> None:  # noqa: D102
        if current is None:
            return
        data = current.data(Qt.UserRole)
        if not isinstance(data, dict):
            return
        if data["kind"] == "online":
            self._current_id = int(data["id"])
            self._append_log(f"已选择客户端 #{self._current_id}")
            self._refresh_remote_files()  # 顺带拉取该客户端接收目录的文件
        else:
            self._current_id = None
            self._append_log(f"{data['host']}:{data['port']} 当前离线，服务端会自动重连")

    def _add_client(self) -> None:
        dialog = AddClientDialog(self)
        if not dialog.exec_():
            return
        name, host, port = dialog.values()
        if not host:
            QMessageBox.warning(self, "提示", "请填写客户端地址")
            return
        try:
            connected = self._server.add_client(host, port, name)
        except ValueError as exc:
            QMessageBox.warning(self, "提示", str(exc))
            return
        self._append_log(
            f"已添加客户端 {name or host} {host}:{port}"
            + ("（已连接）" if connected else "（暂时连不上，稍后自动重试）"))
        self._refresh_clients()

    def _remove_client(self) -> None:
        item = self.client_list.currentItem()
        if item is None:
            return
        data = item.data(Qt.UserRole)
        if not isinstance(data, dict):
            return
        if data["kind"] == "book":
            host, port = data["host"], data["port"]
        else:
            online = next((c for c in self._server.clients() if c["id"] == data["id"]), None)
            if online is None:
                return
            host = online["book_host"] or online["host"]
            port = online["book_port"] or DEFAULT_LISTEN_PORT
        self._server.remove_client(host, port)
        self._append_log(f"已移除客户端 {host}:{port}")
        self._refresh_clients()

    # ---------- 指令 ----------
    def _control(self, action: str, **args) -> None:
        if self._current_id is None:
            self._append_log("请先在左侧选择一个客户端")
            return
        try:
            self._server.control(self._current_id, action, **args)
            self._append_log(f"→ #{self._current_id} {action} {args}".rstrip())
        except (KeyError, OSError, ValueError) as exc:
            self._append_log(f"指令失败 {action}：{exc}")

    def _set_autoshot(self, seconds: int) -> None:
        if self._current_id is None:
            self._append_log("请先选择一个客户端")
            return
        try:
            self._server.set_autoshot(self._current_id, seconds)
            self._append_log(f"→ #{self._current_id} 自动截屏间隔 {seconds} 秒")
        except (KeyError, OSError) as exc:
            self._append_log(f"设置失败：{exc}")

    def _start_record(self) -> None:
        if self._current_id is None:
            self._append_log("请先选择一个客户端")
            return
        try:
            self._server.start_record(self._current_id, self.fps_spin.value(),
                                      self.width_spin.value(),
                                      self.chk_audio.isChecked())
        except (KeyError, OSError) as exc:
            self._append_log(f"开始实时画面失败：{exc}")

    def _stop_record(self) -> None:
        if self._current_id is None:
            return
        try:
            self._server.stop_record(self._current_id)
        except (KeyError, OSError) as exc:
            self._append_log(f"停止实时画面失败：{exc}")
        if self._server.is_recording:  # 顺带收尾录像，否则视频一直不出片
            self._append_log("实时画面已停止，自动结束录制并生成视频")
            self.chk_save_video.setChecked(False)

    def _toggle_recording(self, enabled: bool) -> None:
        """勾选即开始把实时画面录成 MP4，取消勾选即成片。"""
        if not enabled:
            path = self._server.stop_recording()
            if path is None:
                self._append_log("录制已结束（未生成视频）")
            return
        prefix = f"client{self._current_id}_" if self._current_id else ""
        folder = self._server.start_recording(prefix, self.fps_spin.value(),
                                              self.chk_audio.isChecked())
        if folder is None:
            self.chk_save_video.setChecked(False)
            self._append_log("开始录制失败，请确认已安装 imageio-ffmpeg")

    def _open_record_dir(self) -> None:
        
        """打开录制文件夹（还没录制过时打开 saves 目录）。"""
        folder = self._server.record_dir or self._server.shot_dir
        #print(folder)#检测路径
        if not folder.exists():
            self._append_log(f"目录不存在：{folder}")
            return
        open_folders("explorer.exe %s" % folder)
        #QDesktopServices.openUrl(folder.as_uri())

    def _save_current_frame(self) -> None:
        if not self._last_frame:
            self._append_log("还没有收到画面")
            return
        name = f"frame_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        path = self._server.shot_dir / name
        path.write_bytes(self._last_frame)
        self._append_log(f"已保存当前画面：{path}")

    def _request_shot(self) -> None:
        if self._current_id is None:
            self._append_log("请先选择一个客户端")
            return
        try:
            self._server.request_screenshot(self._current_id)
            self._append_log(f"→ #{self._current_id} 请求截屏")
        except (KeyError, OSError) as exc:
            self._append_log(f"请求失败：{exc}")

    def _push_file(self) -> None:
        if self._current_id is None:
            self._append_log("请先选择一个客户端")
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择要推送的文件")
        if not path:
            return
        try:
            self._server.push_file(self._current_id, path)
            self._append_log(f"→ #{self._current_id} 已推送 {path}")
        except (KeyError, FileNotFoundError, OSError) as exc:
            self._append_log(f"推送失败：{exc}")

    def _open_media(self) -> None:
        text = self.path_edit.text().strip()
        if not text:
            self._append_log("请先填写文件路径或流地址")
            return
        if self._current_id is None:
            self._append_log("请先选择一个客户端")
            return
        try:
            self._server.open_media(self._current_id, text)
            self._append_log(f"→ #{self._current_id} 打开 {text}")
        except (KeyError, OSError) as exc:
            self._append_log(f"下发失败：{exc}")

    # ---------- 日志与截屏 ----------
    def _append_log(self, text: str) -> None:
        self.log_view.append(text)

    def _show_frame(self, cid: int, data: bytes) -> None:
        """收到客户端实时画面帧，更新预览。"""
        if self._current_id is not None and cid != self._current_id:
            return
        image = QImage()
        if not image.loadFromData(data, "JPG"):
            return
        self._last_frame = data
        self.shot_preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                self.shot_preview.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def _show_shot(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._append_log(f"截屏无法预览：{path}")
            return
        self.shot_preview.setPixmap(
            pixmap.scaled(self.shot_preview.size(), Qt.KeepAspectRatio,
                          Qt.SmoothTransformation))
        self._append_log(f"收到截屏：{Path(path).name}")

    # ---------- 退出 ----------
    def closeEvent(self, event):  # noqa: N802
        self._poll.stop()
        self._server.stop()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="播放器控制服务端图形界面")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_CONTROL_PORT,
                        help=f"监听端口，默认 {DEFAULT_CONTROL_PORT}")
    parser.add_argument("--shots", default=str(DEFAULT_SHOT_DIR), help="截屏保存目录")
    parser.add_argument("--book", default="clients.json", help="客户端地址簿文件")
    args = parser.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv)
    window = ServerWindow(host=args.host, port=args.port, shot_dir=args.shots,
                          book=args.book)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
