"""Qt 绑定兼容层：安卓用 PySide6，Windows 桌面用 PyQt6。

PySide6 与 PyQt6 的 API 几乎一样，差别主要在信号这几个名字：

    PyQt6:  pyqtSignal / pyqtSlot / pyqtProperty
    PySide6: Signal     / Slot     / Property

PyQt6 官方不发安卓包，所以安卓版必须用 PySide6。这里在"只有 PySide6"的环境里
把 `PyQt6.*` 这组模块名映射到 PySide6，并补上 pyqtSignal 等别名，
这样 core/、ui/ 里现有的 `from PyQt6.QtCore import ...` 不用改就能在安卓上跑。
桌面环境里 PyQt6 是真实的，直接走原路径，不做任何映射。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types

# 需要映射的 Qt 子模块（PySide6 里没有的就跳过）
_SUBMODULES = (
    "QtCore",
    "QtGui",
    "QtWidgets",
    "QtMultimedia",
    "QtMultimediaWidgets",
    "QtNetwork",
    "QtPdf",
    "QtPdfWidgets",
)

# PyQt6 名字 → PySide6 名字
_SIGNAL_ALIASES = {
    "pyqtSignal": "Signal",
    "pyqtSlot": "Slot",
    "pyqtProperty": "Property",
    "pyqtBoundSignal": "SignalInstance",
}


def _make_alias(name: str, source: types.ModuleType) -> types.ModuleType:
    """复制 PySide6 模块的内容，生成一个同名（PyQt6.*）的模块对象。"""
    module = types.ModuleType(name)
    module.__dict__.update(source.__dict__)
    module.__doc__ = f"{name} → {source.__name__}（qtcompat 兼容层）"
    return module


def install() -> str:
    """建立兼容映射，返回实际使用的绑定名（"PyQt6" 或 "PySide6"）。

    已经装了 PyQt6 就直接返回，不改动任何东西（桌面路径零影响）。
    """
    if importlib.util.find_spec("PyQt6") is not None:
        return "PyQt6"
    if importlib.util.find_spec("PySide6") is None:
        raise RuntimeError("既没有 PyQt6 也没有 PySide6，无法运行")

    import PySide6

    package = types.ModuleType("PyQt6")
    package.__path__ = list(getattr(PySide6, "__path__", []))  # 兜底：允许子模块查找
    sys.modules["PyQt6"] = package

    for sub in _SUBMODULES:
        try:
            source = importlib.import_module(f"PySide6.{sub}")
        except ImportError:
            continue
        alias = _make_alias(f"PyQt6.{sub}", source)
        for qt5_name, pyside_name in _SIGNAL_ALIASES.items():
            if hasattr(source, pyside_name):
                setattr(alias, qt5_name, getattr(source, pyside_name))
        sys.modules[f"PyQt6.{sub}"] = alias
        setattr(package, sub, alias)

    return "PySide6"


def binding() -> str:
    """当前实际使用的绑定名。"""
    if importlib.util.find_spec("PySide6") is not None:
        return "PySide6"
    return "PyQt6"
