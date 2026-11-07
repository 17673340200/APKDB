"""控制通道协议（TCP 长连接，复用 file_protocol 的帧格式）。

帧格式（双向一致）：
    | 4 字节 头部长度（大端 uint32） | 头部 JSON（UTF-8） | 数据体（可选） |

服务端 → 播放器（客户端）：
    welcome     注册回执，带 client_id
    control     远程操作，头部带 action / args
    push        推送文件，头部带 name / size，数据体为文件内容
    shot        请求客户端截屏并回传
    list        索取客户端接收目录的文件列表
    delete      删除客户端接收目录里的文件（客户端本地同步删除）
    autoshot    设置客户端自动截屏间隔（秒，0 表示关闭）
    ping        心跳

播放器（客户端）→ 服务端：
    hello       上线注册，头部带 name
    event       状态上报，头部带 msg
    files       接收目录的文件列表（回应 list）
    deleted     删除结果回执（回应 delete）
    shot_data   截屏回传，头部带 name / size，数据体为 PNG 图片
    pong        心跳应答

control 的 action 与 args 见 README「远程控制服务端」一节。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONTROL_PORT = int(os.environ.get("MEDIAPLAYER_CONTROL_PORT", "50518"))
DEFAULT_LISTEN_PORT = int(os.environ.get("MEDIAPLAYER_LISTEN_PORT", "50519"))
DEFAULT_CONTROL_HOST = os.environ.get("MEDIAPLAYER_CONTROL_HOST", "").strip()

# 实时画面（录屏）默认参数
DEFAULT_FRAME_WIDTH = int(os.environ.get("MEDIAPLAYER_FRAME_WIDTH", "960"))
DEFAULT_FPS = 8
MAX_FPS = 30

# 画面保存目录：与播放器的接收目录 received 同级（不存在时自动创建）
_DEFAULT_SAVES_DIR = Path(__file__).resolve().parent.parent / "saves"
DEFAULT_SHOT_DIR = Path(os.environ.get("MEDIAPLAYER_SHOT_DIR", str(_DEFAULT_SAVES_DIR)))
RECONNECT_INTERVAL = 5.0  # 断线重连间隔（秒）
SOCKET_TIMEOUT = 30.0  # 读写超时（秒），超时仅用于让读循环可响应退出

# 定时播放：计划保存在配置文件里（服务端下发的计划也会写到这里，重启后仍生效）
DEFAULT_SCHEDULE_FILE = Path(__file__).resolve().parent.parent / "schedule.json"

# 同步播放参数
SYNC_LEAD_SECONDS = 3.0  # 下发指令后预留的准备时间
SYNC_CORRECT_INTERVAL = 30.0  # 播放过程中校正进度的间隔（秒）
SYNC_TOLERANCE_MS = 500  # 与理想进度相差超过该值才校正
