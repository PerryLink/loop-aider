"""
tests/test_lock.py —— FileLock 锁模块单元测试。

覆盖:
    - File lock acquisition and release
    - Lock timeout behavior
    - Dead lock detection
    - Zombie lock cleanup
    - Concurrent lock attempts
    - Convenience functions (acquire_lock, is_locked)
    - Context manager protocol
    - Force release of stale locks
"""

from __future__ import annotations

import json
import os
import time
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from loop_aider.lock import FileLock, acquire_lock, is_locked


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_dir():
    """Create a temporary directory for lock file tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def lock_path(temp_dir):
    """Return a lock file path within a temporary directory."""
    return os.path.join(temp_dir, ".lock")


# =============================================================================
# Lock Acquisition and Release
# =============================================================================

class TestLockAcquireRelease:
    """File lock acquisition and release tests."""

    def test_acquire_creates_lock_file(self, lock_path):
        """验证: acquire() 成功后锁文件存在且内容包含 PID。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        result = lock.acquire()
        assert result is True
        assert os.path.exists(lock_path)

        # 验证锁文件内容
        content = Path(lock_path).read_text(encoding="utf-8")
        data = json.loads(content)
        assert "pid" in data
        assert data["pid"] == os.getpid()
        assert "acquired_at" in data

        lock.release()

    def test_release_removes_lock_file(self, lock_path):
        """验证: release() 后锁文件被删除。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        lock.acquire()
        assert os.path.exists(lock_path)

        lock.release()
        assert not os.path.exists(lock_path)

    def test_is_acquired_returns_correct_state(self, lock_path):
        """验证: is_acquired() 正确反映锁的状态。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        assert lock.is_acquired() is False

        lock.acquire()
        assert lock.is_acquired() is True

        lock.release()
        assert lock.is_acquired() is False

    def test_release_idempotent(self, lock_path):
        """验证: release() 可安全多次调用而不抛异常。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        lock.acquire()
        lock.release()
        # 第二次 release 不应抛异常
        lock.release()
        assert lock.is_acquired() is False

    def test_acquire_creates_parent_directory(self, temp_dir):
        """验证: acquire() 自动创建不存在的父目录。"""
        nested_path = os.path.join(temp_dir, "deep", "nested", ".lock")
        lock = FileLock(nested_path, timeout_seconds=1.0)
        result = lock.acquire()
        assert result is True
        assert os.path.exists(nested_path)
        lock.release()

    def test_acquire_writes_lock_info_correctly(self, lock_path):
        """验证: lock info 包含正确的 JSON 字段。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        lock.acquire()

        data = json.loads(Path(lock_path).read_text(encoding="utf-8"))
        assert isinstance(data["pid"], int)
        assert "acquired_at" in data
        # 时间戳应为有效的 ISO 8601 格式
        acquired_dt = datetime.fromisoformat(data["acquired_at"])
        assert abs((datetime.now(timezone.utc) - acquired_dt).total_seconds()) < 10

        lock.release()


# =============================================================================
# Lock Timeout Behavior
# =============================================================================

class TestLockTimeout:
    """Lock timeout behavior tests."""

    def test_acquire_times_out_when_locked_by_another(self, lock_path):
        """验证: 锁被占用时 acquire() 在超时后返回 False。"""
        lock1 = FileLock(lock_path, timeout_seconds=0.1)
        lock2 = FileLock(lock_path, timeout_seconds=1.0)

        lock1.acquire()
        try:
            result = lock2.acquire()
            assert result is False
            assert lock2.is_acquired() is False
        finally:
            lock1.release()

    def test_acquire_retries_with_backoff(self, lock_path):
        """验证: acquire() 在锁被占用时进行指数退避重试。"""
        # 使用较短的 timeout 以加快测试
        lock1 = FileLock(lock_path, timeout_seconds=0.1)
        lock2 = FileLock(lock_path, timeout_seconds=2.0)

        lock1.acquire()
        try:
            start = time.monotonic()
            result = lock2.acquire()
            elapsed = time.monotonic() - start
            assert result is False
            # 应该在 timeout_seconds 附近超时
            assert elapsed >= 1.5
        finally:
            lock1.release()

    def test_default_timeout_values(self, lock_path):
        """验证: FileLock 默认超时值设置正确。"""
        lock = FileLock(lock_path)
        assert lock.timeout_seconds == 30.0
        assert lock.lock_timeout_minutes == 15
        assert lock.retry_interval == 0.1

    def test_custom_timeout_constructor(self, lock_path):
        """验证: 自定义超时参数正确赋值。"""
        lock = FileLock(
            lock_path,
            timeout_seconds=5.0,
            lock_timeout_minutes=30,
            retry_interval=0.5,
        )
        assert lock.timeout_seconds == 5.0
        assert lock.lock_timeout_minutes == 30
        assert lock.retry_interval == 0.5


# =============================================================================
# Dead Lock Detection
# =============================================================================

class TestDeadLockDetection:
    """Dead lock detection and stale lock cleanup tests."""

    def test_detect_stale_lock_by_age(self, lock_path):
        """验证: 锁文件超过 lock_timeout_minutes 后被标记为过期。"""
        # 创建旧的锁文件（时间戳在过去）
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        old_data = {
            "pid": 12345,
            "acquired_at": "2020-01-01T00:00:00+00:00",
            "hostname": "test",
        }
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(old_data, f)

        lock = FileLock(lock_path, lock_timeout_minutes=15)
        assert lock._is_lock_stale() is True

    def test_detect_stale_lock_missing_file(self, lock_path):
        """验证: 锁文件不存在时返回过期。"""
        lock = FileLock(lock_path, lock_timeout_minutes=15)
        assert lock._is_lock_stale() is True

    def test_detect_stale_lock_invalid_json(self, lock_path):
        """验证: 锁文件内容损坏时返回过期。"""
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        Path(lock_path).write_text("not valid json", encoding="utf-8")

        lock = FileLock(lock_path, lock_timeout_minutes=15)
        assert lock._is_lock_stale() is True

    def test_detect_stale_lock_no_acquired_at(self, lock_path):
        """验证: 锁文件缺少 acquired_at 字段时返回过期。"""
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        Path(lock_path).write_text(
            json.dumps({"pid": 12345, "hostname": "test"}),
            encoding="utf-8",
        )

        lock = FileLock(lock_path, lock_timeout_minutes=15)
        assert lock._is_lock_stale() is True

    def test_detect_stale_lock_empty_acquired_at(self, lock_path):
        """验证: acquired_at 为空字符串时返回过期。"""
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        Path(lock_path).write_text(
            json.dumps({"pid": 12345, "acquired_at": ""}),
            encoding="utf-8",
        )

        lock = FileLock(lock_path, lock_timeout_minutes=15)
        assert lock._is_lock_stale() is True

    def test_fresh_lock_not_stale(self, lock_path):
        """验证: 刚刚创建的锁不被标记为过期。"""
        lock = FileLock(lock_path, lock_timeout_minutes=60)
        lock.acquire()
        assert lock._is_lock_stale() is False
        lock.release()


# =============================================================================
# Zombie Lock Cleanup
# =============================================================================

class TestZombieLockCleanup:
    """Zombie lock (stale lock) cleanup tests."""

    def test_force_release_removes_stale_lock(self, lock_path):
        """验证: _force_release() 删除过期锁文件。"""
        # 手动创建一个过期锁文件
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        stale_data = {
            "pid": 99999,
            "acquired_at": "2020-01-01T00:00:00+00:00",
            "hostname": "test",
        }
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(stale_data, f)

        assert os.path.exists(lock_path)

        lock = FileLock(lock_path, lock_timeout_minutes=0)
        lock._force_release()
        assert not os.path.exists(lock_path)

    def test_acquire_cleans_stale_lock_and_succeeds(self, lock_path):
        """验证: acquire() 检测到过期锁时先清理再获取。"""
        # 创建过期锁
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        stale_data = {
            "pid": 99999,
            "acquired_at": "2020-01-01T00:00:00+00:00",
            "hostname": "test",
        }
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(stale_data, f)

        lock = FileLock(lock_path, lock_timeout_minutes=0, timeout_seconds=2.0)
        result = lock.acquire()
        assert result is True
        assert lock.is_acquired() is True

        # 验证锁文件内容已更新为当前进程
        content = Path(lock_path).read_text(encoding="utf-8")
        data = json.loads(content)
        assert data["pid"] == os.getpid()

        lock.release()

    def test_force_release_idempotent(self, lock_path):
        """验证: _force_release() 可安全多次调用。"""
        lock = FileLock(lock_path, lock_timeout_minutes=0)
        lock._force_release()  # 文件可能不存在
        lock._force_release()  # 第二次也应无异常

        # 创建后再删除
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        Path(lock_path).touch()
        lock._force_release()
        assert not os.path.exists(lock_path)


# =============================================================================
# Concurrent Lock Attempts
# =============================================================================

class TestConcurrentLockAttempts:
    """Concurrent lock attempt tests."""

    def test_only_one_lock_at_a_time(self, lock_path):
        """验证: 同一时刻只有一个进程能持有锁。"""
        lock1 = FileLock(lock_path, timeout_seconds=1.0)
        lock2 = FileLock(lock_path, timeout_seconds=0.1)

        assert lock1.acquire() is True
        try:
            assert lock2.acquire() is False
        finally:
            lock1.release()

    def test_second_acquire_after_release(self, lock_path):
        """验证: 锁释放后其他实例可以获取。"""
        lock1 = FileLock(lock_path, timeout_seconds=0.1)
        lock2 = FileLock(lock_path, timeout_seconds=1.0)

        lock1.acquire()
        lock1.release()

        result = lock2.acquire()
        assert result is True
        lock2.release()

    def test_multiple_sequential_acquisitions(self, lock_path):
        """验证: 同一实例多次 acquire/release 循环。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)

        for _ in range(3):
            assert lock.acquire() is True
            assert lock.is_acquired() is True
            lock.release()
            assert lock.is_acquired() is False

    def test_concurrent_lock_attempts_different_locks(self, temp_dir):
        """验证: 不同锁文件可以同时被不同实例持有。"""
        lock1_path = os.path.join(temp_dir, "lock1")
        lock2_path = os.path.join(temp_dir, "lock2")

        lock1 = FileLock(lock1_path, timeout_seconds=1.0)
        lock2 = FileLock(lock2_path, timeout_seconds=1.0)

        assert lock1.acquire() is True
        assert lock2.acquire() is True

        assert lock1.is_acquired() is True
        assert lock2.is_acquired() is True

        lock1.release()
        lock2.release()


# =============================================================================
# Context Manager Protocol
# =============================================================================

class TestContextManager:
    """Context manager protocol tests."""

    def test_context_manager_acquires_and_releases(self, lock_path):
        """验证: with 语句自动获取和释放锁。"""
        with FileLock(lock_path, timeout_seconds=1.0) as acquired:
            assert acquired is True
            assert os.path.exists(lock_path)
        # 离开上下文后锁应释放
        assert not os.path.exists(lock_path)

    def test_context_manager_on_timeout(self, lock_path):
        """验证: with 语句在超时时返回 False。"""
        lock1 = FileLock(lock_path, timeout_seconds=0.1)
        lock1.acquire()
        try:
            with FileLock(lock_path, timeout_seconds=0.1) as acquired:
                assert acquired is False
        finally:
            lock1.release()

    def test_context_manager_does_not_suppress_exceptions(self, lock_path):
        """验证: __exit__ 不吞异常。"""
        try:
            with FileLock(lock_path, timeout_seconds=1.0) as acquired:
                assert acquired is True
                raise ValueError("test error")
        except ValueError:
            pass  # 异常应该传播出来
        else:
            pytest.fail("Exception should have propagated")

        # 锁仍应被释放
        assert not os.path.exists(lock_path)


# =============================================================================
# Convenience Functions
# =============================================================================

class TestConvenienceFunctions:
    """Convenience functions: acquire_lock() and is_locked()."""

    def test_acquire_lock_returns_lock_on_success(self, lock_path):
        """验证: acquire_lock() 成功时返回 FileLock 实例。"""
        lock = acquire_lock(lock_path, timeout_seconds=1.0)
        assert lock is not None
        assert isinstance(lock, FileLock)
        assert lock.is_acquired() is True
        lock.release()

    def test_acquire_lock_returns_none_on_timeout(self, lock_path):
        """验证: acquire_lock() 超时时返回 None。"""
        lock1 = FileLock(lock_path, timeout_seconds=0.1)
        lock1.acquire()
        try:
            lock2 = acquire_lock(lock_path, timeout_seconds=0.1)
            assert lock2 is None
        finally:
            lock1.release()

    def test_acquire_lock_with_custom_timeout(self, lock_path):
        """验证: acquire_lock() 传递自定义参数。"""
        lock = acquire_lock(
            lock_path, timeout_seconds=2.0, lock_timeout_minutes=30
        )
        assert lock is not None
        assert lock.timeout_seconds == 2.0
        assert lock.lock_timeout_minutes == 30
        lock.release()

    def test_is_locked_returns_true_when_locked(self, lock_path):
        """验证: is_locked() 在有活跃锁时返回 True。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        lock.acquire()
        try:
            assert is_locked(lock_path) is True
        finally:
            lock.release()

    def test_is_locked_returns_false_when_unlocked(self, lock_path):
        """验证: is_locked() 在无锁时返回 False。"""
        assert is_locked(lock_path) is False

    def test_is_locked_returns_false_for_nonexistent_path(self):
        """验证: is_locked() 对不存在的路径返回 False。"""
        assert is_locked("/tmp/nonexistent/.lock-xyz") is False

    def test_is_locked_returns_false_for_corrupted_lock(self, lock_path):
        """验证: is_locked() 对损坏的锁文件返回 False。"""
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        Path(lock_path).write_text("corrupted json", encoding="utf-8")
        assert is_locked(lock_path) is False


# =============================================================================
# Edge Cases
# =============================================================================

class TestEdgeCases:
    """Edge case tests for FileLock."""

    def test_acquire_on_readonly_dir_failure(self, temp_dir):
        """验证: 在只读目录创建锁时 acquire() 返回 False（模拟 OSError）。"""
        lock_path = os.path.join(temp_dir, ".lock")
        with patch("os.open", side_effect=OSError("Permission denied")):
            lock = FileLock(lock_path, timeout_seconds=0.1)
            result = lock.acquire()
            assert result is False

    def test_destructor_releases_lock(self, lock_path):
        """验证: __del__ 析构函数自动释放锁。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        lock.acquire()
        assert os.path.exists(lock_path)
        lock.__del__()
        assert not os.path.exists(lock_path)

    def test_release_handles_deleted_lock_file(self, lock_path):
        """验证: 锁文件被外部删除后 release() 不抛异常。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        lock.acquire()
        # 外部删除锁文件
        os.remove(lock_path)
        lock.release()  # 不应抛异常
        assert lock.is_acquired() is False

    def test_release_handles_invalid_fd(self, lock_path):
        """验证: 文件描述符无效时 release() 不抛异常。"""
        lock = FileLock(lock_path, timeout_seconds=1.0)
        lock.acquire()
        # 设置无效的 fd 值模拟已关闭的文件描述符
        lock._fd = -1
        lock.release()  # 不应抛异常

    def test_lock_path_with_special_characters(self, temp_dir):
        """验证: 锁文件路径包含特殊字符时正常工作。"""
        lock_path = os.path.join(temp_dir, ".lock with spaces #123")
        lock = FileLock(lock_path, timeout_seconds=1.0)
        result = lock.acquire()
        assert result is True
        assert os.path.exists(lock_path)
        lock.release()
