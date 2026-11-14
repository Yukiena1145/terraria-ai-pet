"""Windows 游戏窗口定位：按标题 + 可执行文件路径双重匹配。

双开场景下，Steam 版和 F 盘副本窗口标题可能一样。只按标题会控制错客户端，
所以这里优先用窗口 PID 对应的 exe 路径过滤。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

try:
    import pygetwindow as gw
    import win32process
except Exception:  # pragma: no cover
    gw = None
    win32process = None

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


def _normalize(path: str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except Exception:
        return os.path.normcase(str(path))


def window_pid(hwnd: int) -> int:
    if win32process is None:
        return 0
    try:
        _, pid = win32process.GetWindowThreadProcessId(int(hwnd))
        return int(pid)
    except Exception:
        return 0


def window_exe(hwnd: int) -> str:
    pid = window_pid(hwnd)
    if not pid or psutil is None:
        return ""
    try:
        return psutil.Process(pid).exe()
    except Exception:
        return ""


def find_window(
    titles: Iterable[str],
    *,
    exe_path: str = "",
) -> object | None:
    """返回 pygetwindow 窗口对象；exe_path 非空时要求进程路径匹配。"""
    if gw is None:
        return None
    wanted = _normalize(exe_path) if exe_path else ""
    for title in titles:
        title = str(title).strip()
        if not title:
            continue
        try:
            matches = gw.getWindowsWithTitle(title)
        except Exception:
            continue
        for window in matches:
            try:
                if not window.visible:
                    continue
                hwnd = int(getattr(window, "_hWnd", 0) or 0)
                if not hwnd:
                    continue
                if wanted:
                    actual = window_exe(hwnd)
                    if not actual or _normalize(actual) != wanted:
                        logger.debug(
                            "跳过窗口 %r：exe=%r，期望=%r",
                            getattr(window, "title", ""),
                            actual,
                            exe_path,
                        )
                        continue
                return window
            except Exception:
                continue
    return None


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """返回客户区屏幕坐标和尺寸；最大化/全屏时不会带标题栏偏移。"""
    try:
        import ctypes
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetClientRect(int(hwnd), ctypes.byref(rect))
        point = ctypes.wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(int(hwnd), ctypes.byref(point))
        return int(point.x), int(point.y), int(rect.right), int(rect.bottom)
    except Exception:
        return 0, 0, 0, 0


def force_foreground(hwnd: int) -> bool:
    """强激活窗口：pygetwindow 之外再用 AttachThreadInput 兜底。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        foreground = user32.GetForegroundWindow()
        if foreground == hwnd:
            return True
        current_thread = kernel32.GetCurrentThreadId()
        fg_thread = (
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        )
        attached = False
        if fg_thread and fg_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, fg_thread, True))
        try:
            user32.ShowWindow(hwnd, 9)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, fg_thread, False)
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        logger.exception("强制激活窗口失败")
        return False


__all__ = ["client_rect", "find_window", "force_foreground", "window_exe", "window_pid"]
