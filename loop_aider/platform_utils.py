"""
loop_aider/platform_utils.py —— 跨平台工具模块。

提供跨 Windows / Linux / macOS 的子进程管理、文件系统原子操作和平台检测工具。

关键设计:
    - Windows: subprocess 必须使用 shell=True（Aider 通过批处理/shim 启动）。
    - POSIX:   shell=False，直接执行，性能更优。
    - 原子文件操作: Windows 调用 ReplaceFileW（事务性），POSIX 使用 os.replace。
    - PID 追踪: 支持子进程生命周期管理和超时强杀。

Usage:
    from loop_aider.platform_utils import run_aider_subprocess
    result = run_aider_subprocess(["aider", "--yes", "--message", "..."], timeout=600)
"""

from __future__ import annotations
import os
import signal
import subprocess
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# 平台检测
# ---------------------------------------------------------------------------

def is_windows() -> bool:
    """
    判断当前运行平台是否为 Windows。

    Returns:
        True 表示当前在 Windows 平台运行。
    """
    return sys.platform == "win32"


def is_posix() -> bool:
    """
    判断当前运行平台是否为 POSIX 兼容系统（Linux/macOS）。

    Returns:
        True 表示当前在 POSIX 兼容系统上运行。
    """
    return sys.platform != "win32"


# ---------------------------------------------------------------------------
# 子进程命令构建
# ---------------------------------------------------------------------------

def build_subprocess_cmd(cmd: list[str]) -> list[str] | str:
    """
    根据平台构建合适的 subprocess 命令参数。

    Windows 平台将 list 转换为命令行字符串（由 subprocess.list2cmdline 处理转义），
    POSIX 平台直接返回原始 list。

    Args:
        cmd: 命令行参数列表，如 ["aider", "--yes", "--message", "hello"]。

    Returns:
        Windows: 转义后的命令行字符串。
        POSIX:   原始 list。
    """
    if is_windows():
        return subprocess.list2cmdline(cmd)
    return cmd


# ---------------------------------------------------------------------------
# Aider 子进程执行
# ---------------------------------------------------------------------------

def run_aider_subprocess(
    cmd: list[str],
    timeout: int = 600,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """
    跨平台 Aider 子进程调用。

    封装 subprocess.run()，处理 Windows/Linux/macOS 的平台差异:
        - Windows: 使用 shell=True，cmd 转为字符串。
        - POSIX:   使用 shell=False，直接传 list。

    Args:
        cmd:     命令行参数列表。
        timeout: 超时时间（秒），默认 600。
        cwd:     子进程工作目录。

    Returns:
        subprocess.CompletedProcess 对象，含 stdout、stderr、returncode。

    Raises:
        subprocess.TimeoutExpired: 子进程超时。
    """
    if is_windows():
        cmd_str = subprocess.list2cmdline(cmd)
        return subprocess.run(
            cmd_str,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    else:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )


# ---------------------------------------------------------------------------
# 原子文件操作
# ---------------------------------------------------------------------------

def atomic_rename(tmp_path: str, target_path: str):
    """
    原子性地将临时文件替换为目标文件，跨平台实现。

    - Windows: 调用 kernel32.ReplaceFileW，支持备份和事务性替换。
    - POSIX:   使用 os.replace，原子性 rename。

    Args:
        tmp_path:    临时文件路径（源）。
        target_path: 目标文件路径（将被替换）。
    """
    if os.path.exists(target_path):
        if is_windows():
            import ctypes
            ctypes.windll.kernel32.ReplaceFileW(
                target_path, tmp_path, None, 0, None, None
            )
        else:
            os.replace(tmp_path, target_path)
    else:
        os.rename(tmp_path, target_path)


def fsync_path(file_path: str):
    """
    将文件缓冲区刷入磁盘，跨平台实现。

    以 r+b 模式打开已有文件或 wb 创建新文件，调用 os.fsync 确保数据落盘。

    Args:
        file_path: 需要 fsync 的文件路径。
    """
    mode = "r+b" if os.path.exists(file_path) else "wb"
    with open(file_path, mode) as f:
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# 超时与信号处理
# ---------------------------------------------------------------------------

def kill_process_tree(pid: int):
    """
    递归终止进程树（Windows 用 taskkill，POSIX 用 SIGTERM）。

    Args:
        pid: 根进程 PID。
    """
    if pid <= 0:
        return
    if is_windows():
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass


def get_process_pid(proc: subprocess.Popen) -> int:
    """
    获取 Popen 对象的进程 PID。

    Args:
        proc: subprocess.Popen 实例。

    Returns:
        进程 PID。
    """
    return proc.pid if proc.pid is not None else -1
