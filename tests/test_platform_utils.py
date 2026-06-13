"""
tests/test_platform_utils.py —— 跨平台工具测试。

测试平台检测、子进程命令构建、原子文件操作等平台适配功能。
"""

from loop_aider.platform_utils import (
    is_windows,
    is_posix,
    build_subprocess_cmd,
)


def test_is_windows_type():
    """测试: is_windows() 返回布尔类型。"""
    assert isinstance(is_windows(), bool)


def test_is_posix_type():
    """测试: is_posix() 返回布尔类型。"""
    assert isinstance(is_posix(), bool)


def test_is_windows_and_posix_mutually_exclusive():
    """测试: Windows 和 POSIX 互斥。"""
    assert is_windows() != is_posix()


def test_build_subprocess_cmd_posix_style():
    """测试: 命令构建结果包含所有参数。"""
    cmd = ["aider", "--yes", "--message", "hello"]
    result = build_subprocess_cmd(cmd)
    if is_posix():
        assert result == cmd
    else:
        assert isinstance(result, str)
        assert "aider" in result
        assert "--yes" in result
        assert "hello" in result


def test_build_subprocess_cmd_with_model_flag():
    """测试: 含 --model 标志的命令构建。"""
    cmd = ["aider", "--yes", "--message", "test", "--model", "sonnet"]
    result = build_subprocess_cmd(cmd)
    if is_posix():
        assert result == cmd
    else:
        assert "--model" in result
        assert "sonnet" in result
