"""
loop_aider/lock.py —— 文件锁并发保护模块

提供跨平台的排他文件锁机制，确保同一 state_dir 下只有一个 loop-aider
进程在运行。基于 O_CREAT|O_EXCL 原子创建 + 指数退避重试 + 死锁检测。

Usage:
    from loop_aider.lock import FileLock

    lock = FileLock(".aider/loop-aider/.lock")
    if lock.acquire():
        try:
            # 临界区代码
            ...
        finally:
            lock.release()

    # 或使用上下文管理器
    with FileLock(".aider/loop-aider/.lock") as acquired:
        if acquired:
            ...
"""

from __future__ import annotations

import os
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class FileLock:
    """
    跨平台文件锁。

    使用 O_CREAT|O_EXCL 原子创建锁文件，保证只有第一个进程能获取锁。
    支持过期锁检测、强制释放和上下文管理器。

    Attributes:
        lock_path: 锁文件路径。
        timeout_seconds: 获取锁的最大等待时间（秒）。
        lock_timeout_minutes: 锁文件过期时间（分钟），超过视为死锁。
        retry_interval: 重试间隔基础值（秒），使用指数退避。
    """

    def __init__(
        self,
        lock_path: str,
        timeout_seconds: float = 30.0,
        lock_timeout_minutes: int = 15,
        retry_interval: float = 0.1,
    ):
        """初始化文件锁。

        Args:
            lock_path: 锁文件完整路径。
            timeout_seconds: 获取锁的最长等待时间。
            lock_timeout_minutes: 锁文件过期分钟数。
            retry_interval: 重试间隔基础秒数。
        """
        self.lock_path = Path(lock_path)
        self.timeout_seconds = timeout_seconds
        self.lock_timeout_minutes = lock_timeout_minutes
        self.retry_interval = retry_interval
        self._fd: Optional[int] = None
        self._acquired = False
        self.logger = logging.getLogger("loop_aider.lock")

    # ------------------------------------------------------------------
    # acquire / release
    # ------------------------------------------------------------------

    def acquire(self) -> bool:
        """尝试获取排他锁，使用指数退避重试。

        Returns:
            True 获取成功，False 超时失败。
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        start_time = time.monotonic()
        backoff = self.retry_interval

        while True:
            try:
                # O_CREAT|O_EXCL 保证原子性
                fd = os.open(
                    str(self.lock_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
                # 写入锁信息
                lock_info = {
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "hostname": os.uname().nodename if hasattr(os, 'uname') else "unknown",
                }
                with os.fdopen(fd, "w") as f:
                    json.dump(lock_info, f)
                    f.flush()
                    os.fsync(f.fileno())
                self._fd = fd
                self._acquired = True
                self.logger.debug("锁获取成功: %s (pid=%d)", self.lock_path, os.getpid())
                return True

            except FileExistsError:
                # 锁已被占用，检查是否过期
                if self._is_lock_stale():
                    self._force_release()
                    continue  # 立即重试

                # 超时检查
                elapsed = time.monotonic() - start_time
                if elapsed >= self.timeout_seconds:
                    self.logger.warning(
                        "锁获取超时: %s (%.1fs)", self.lock_path, elapsed
                    )
                    return False

                time.sleep(min(backoff, 5.0))
                backoff *= 2

            except OSError as exc:
                self.logger.error("锁文件创建失败: %s", exc)
                return False

    def release(self):
        """释放锁，关闭文件描述符并删除锁文件。

        即使锁文件已被手动删除，本方法也不会抛出异常。
        """
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        if self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass

        self._acquired = False
        self.logger.debug("锁已释放: %s", self.lock_path)

    def is_acquired(self) -> bool:
        """检查当前实例是否持有锁。

        Returns:
            True 表示持有锁。
        """
        return self._acquired and self.lock_path.exists()

    # ------------------------------------------------------------------
    # 死锁检测
    # ------------------------------------------------------------------

    def _is_lock_stale(self) -> bool:
        """检查锁文件是否已过期（死锁）。

        读取锁文件内容的 acquired_at 时间戳，与当前时间比较。

        Returns:
            True 表示锁已过期。
        """
        if not self.lock_path.exists():
            return True
        try:
            content = self.lock_path.read_text(encoding="utf-8")
            data = json.loads(content)
            acquired = data.get("acquired_at", "")
            if not acquired:
                return True
            acquired_dt = datetime.fromisoformat(acquired)
            age = (datetime.now(timezone.utc) - acquired_dt).total_seconds()
            return age > (self.lock_timeout_minutes * 60)
        except (json.JSONDecodeError, ValueError, OSError):
            return True

    def _force_release(self):
        """强制删除过期锁文件。"""
        try:
            self.lock_path.unlink(missing_ok=True)
            self.logger.warning("强制释放过期锁: %s", self.lock_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self) -> bool:
        """上下文管理器入口：获取锁。

        Returns:
            True 获取成功，False 获取失败。
        """
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口：释放锁。"""
        self.release()
        return False  # 不吞异常

    def __del__(self):
        """析构函数：确保锁被释放。"""
        if self._acquired:
            self.release()


# =============================================================================
# 便捷函数
# =============================================================================

def acquire_lock(
    lock_path: str,
    timeout_seconds: float = 30.0,
    lock_timeout_minutes: int = 15,
) -> Optional[FileLock]:
    """快速获取文件锁的便捷函数。

    Args:
        lock_path: 锁文件路径。
        timeout_seconds: 最大等待时间。
        lock_timeout_minutes: 锁过期时间。

    Returns:
        获取成功返回 FileLock 实例，失败返回 None。
    """
    lock = FileLock(lock_path, timeout_seconds, lock_timeout_minutes)
    if lock.acquire():
        return lock
    return None


def is_locked(lock_path: str) -> bool:
    """检查指定路径是否已被锁定。

    Args:
        lock_path: 锁文件路径。

    Returns:
        True 表示锁存在且未过期。
    """
    path = Path(lock_path)
    if not path.exists():
        return False
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        acquired = data.get("acquired_at", "")
        if not acquired:
            return False
        acquired_dt = datetime.fromisoformat(acquired)
        age = (datetime.now(timezone.utc) - acquired_dt).total_seconds()
        return age <= (15 * 60)  # 默认 15 分钟过期
    except Exception:
        return False
