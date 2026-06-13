"""
tests/test_aider_manager.py —— AiderManager 单元测试。

测试 check_health() 版本检测、run_phase() 子进程调用、
--message-file 安全方案等核心功能。
"""

import tempfile
from unittest.mock import patch, MagicMock

import pytest

from loop_aider.aider_manager import AiderManager, HealthStatus, AiderResult


class TestAiderManagerHealthCheck:
    """AiderManager.check_health() 测试套件。"""

    def test_health_check_ok_for_modern_aider(self):
        """测试: 版本 >= 0.86.0 返回 OK。"""
        with patch("shutil.which", return_value="aider"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="0.86.1", stderr=""
                )
                mgr = AiderManager({"aider_timeout_seconds": 600})
                status = mgr.check_health()
                assert status == HealthStatus.OK

    def test_health_check_compatible_with_warnings(self):
        """测试: 版本 < 0.86.0 且 >= 0.77.0 返回警告。"""
        with patch("shutil.which", return_value="aider"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="0.77.0", stderr=""
                )
                mgr = AiderManager({"aider_timeout_seconds": 600})
                status = mgr.check_health()
                assert status == HealthStatus.COMPATIBLE_WITH_WARNINGS

    def test_health_check_incompatible_for_old_aider(self):
        """测试: 版本 < 0.77.0 返回不兼容。"""
        with patch("shutil.which", return_value="aider"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="0.65.0", stderr=""
                )
                mgr = AiderManager({"aider_timeout_seconds": 600})
                status = mgr.check_health()
                assert status == HealthStatus.INCOMPATIBLE

    def test_health_check_not_found(self):
        """测试: aider 不在 PATH 中返回 NOT_FOUND。"""
        with patch("shutil.which", return_value=None):
            mgr = AiderManager({"aider_timeout_seconds": 600})
            status = mgr.check_health()
            assert status == HealthStatus.NOT_FOUND


class TestAiderManagerCommand:
    """AiderManager 命令构建测试套件。"""

    def test_build_command_basic(self):
        """测试: 基本命令构建包含必要参数。"""
        mgr = AiderManager({"aider_timeout_seconds": 600})
        mgr._version = (0, 86, 0)
        cmd = mgr._build_command("hello test")
        assert "aider" in cmd[0]
        assert "--yes" in cmd
        assert "--no-auto-commits" in cmd

    def test_safe_message_file_creates_temp_file(self):
        """测试: --message-file 方案创建临时文件。"""
        mgr = AiderManager({"aider_timeout_seconds": 600})
        mgr._version = (0, 86, 0)  # 模拟 >= 0.77.0
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt = "secret content for testing"
            args, tmpfile = mgr._safe_message_arg(prompt, state_dir=tmpdir)
            assert "--message-file" in args
            assert tmpfile is not None

    def test_safe_message_fallback_for_old_aider(self):
        """测试: 旧版 Aider 回退到 --message。"""
        mgr = AiderManager({"aider_timeout_seconds": 600})
        mgr._version = (0, 76, 0)
        args, tmpfile = mgr._safe_message_arg("hello")
        assert "--message" in args
        assert tmpfile is None


class TestAiderManagerRunPhase:
    """AiderManager.run_phase() 测试套件（M1 范围）。"""

    def test_run_phase_refuses_incompatible_health(self):
        """测试: 不兼容时 run_phase 抛出 RuntimeError。"""
        mgr = AiderManager({"aider_timeout_seconds": 600})
        mgr._health = HealthStatus.INCOMPATIBLE
        with pytest.raises(RuntimeError, match="incompatible"):
            mgr.run_phase("part_1_1", {"goal": "test", "cycle": 1})

    def test_run_phase_refuses_not_found_health(self):
        """测试: Aider 未找到时 run_phase 抛出 RuntimeError。"""
        mgr = AiderManager({"aider_timeout_seconds": 600})
        mgr._health = HealthStatus.NOT_FOUND
        with pytest.raises(RuntimeError, match="not found"):
            mgr.run_phase("part_1_1", {"goal": "test", "cycle": 1})
