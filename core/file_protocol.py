"""文件收发接口的自定义协议（TCP 长连接，纯标准库实现）。

帧格式（请求与响应一致）：
    | 4 字节 头部长度（大端 uint32） | 头部 JSON（UTF-8） | 数据体（可选） |

命令：
    ping    心跳，响应 msg="pong"
    push    上传文件，头部带 name / size，数据体为文件内容；收完自动通知播放器打开
    play    播放已上传的文件，头部带 name
    list    列出服务端已保存的文件，响应数据体为 JSON 数组
    get     下载文件，头部带 name，响应数据体为文件内容

响应头部固定带 ok / cmd / msg，出错时 ok=false 且 msg 为错误原因。
"""

from __future__ import annotations

import json
import os
import socket
import struct

DEFAULT_PORT = int(os.environ.get("MEDIAPLAYER_FILE_PORT", "50517"))
MAX_HEADER_SIZE = 64 * 1024  # 头部上限
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 单文件上限 2GB
CHUNK = 64 * 1024  # 收发分片大小


class ProtocolError(Exception):
    """协议错误，会作为 ok=false 的响应返回给对端。"""


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """收满 size 字节，连接断开则抛 ProtocolError。"""
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise ProtocolError("连接已断开")
        buffer += chunk
    return bytes(buffer)


def read_header(sock: socket.socket) -> dict:
    raw = recv_exact(sock, 4)
    (length,) = struct.unpack(">I", raw)
    if length <= 0 or length > MAX_HEADER_SIZE:
        raise ProtocolError("头部长度非法")
    return json.loads(recv_exact(sock, length).decode("utf-8"))


def send_header(sock: socket.socket, header: dict) -> None:
    data = json.dumps(header, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def send_frame(sock: socket.socket, header: dict, payload: bytes = b"") -> None:
    send_header(sock, header)
    if payload:
        sock.sendall(payload)


def safe_name(name: str | None) -> str:
    """只保留文件名部分，防止 ../ 之类的路径穿越。"""
    cleaned = os.path.basename((name or "").replace("\\", "/"))
    if not cleaned or cleaned in (".", ".."):
        raise ProtocolError("文件名非法")
    return cleaned
