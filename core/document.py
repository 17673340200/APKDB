"""PDF 文档模型：负责打开 PDF 并把页面渲染成位图数据。

PPT 经 LibreOffice 转换成 PDF 后同样使用该模型播放。
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf  # PyMuPDF（渲染引擎）


class DocumentError(Exception):
    """打开或渲染文档失败。"""


@dataclass(frozen=True)
class RenderedPage:
    """一页渲染结果（原始 RGB 像素，交由 UI 层转成图像）。"""

    samples: bytes
    width: int
    height: int
    stride: int


class Document:
    def __init__(self, path: str):
        try:
            self._doc = pymupdf.open(path)
        except Exception as exc:  # PyMuPDF 抛出多种异常，统一包装
            raise DocumentError(f"无法打开文件：{exc}") from exc
        self.path = path

    @property
    def page_count(self) -> int:
        return self._doc.page_count

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass

    def page_size(self, index: int) -> tuple[float, float]:
        """返回页面尺寸（单位：点，1 点 = 1/72 英寸）。"""
        page = self._doc.load_page(index)
        rect = page.rect
        return rect.width, rect.height

    def render_page(self, index: int, zoom: float) -> RenderedPage:
        """按给定缩放比例渲染指定页（0 基索引）。"""
        try:
            page = self._doc.load_page(index)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return RenderedPage(
                samples=bytes(pixmap.samples),
                width=pixmap.width,
                height=pixmap.height,
                stride=pixmap.stride,
            )
        except Exception as exc:
            raise DocumentError(f"渲染第 {index + 1} 页失败：{exc}") from exc
