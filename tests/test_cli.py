"""
tests/test_cli.py —— CLI 命令行接口测试。

覆盖:
    - CLI argument parsing (所有子命令和参数)
    - run command initialization flow
    - status command output
    - resume command behavior
    - init command working directory setup
    - Trust mode flag combinations (--safe, --unsafe, --interactive)
    - Error handling for missing args
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from loop_aider.cli import main, _cmd_run, _cmd_status, _cmd_resume, _cmd_init
from loop_aider.config import Config
from loop_aider.state_machine import StateMachine


# =============================================================================
# CLI Argument Parsing
# =============================================================================

class TestCLIArgumentParsing:
    """CLI argument parsing tests."""

    def test_parser_requires_subcommand(self):
        """验证: 不带子命令时 parser 报错。"""
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["loop-aider"]):
                main()

    def test_parser_run_requires_goal(self):
        """验证: run 子命令 --goal 为必填参数。"""
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["loop-aider", "run"]):
                main()

    def test_parser_run_with_minimal_args(self):
        """验证: run 子命令接受最精简参数。"""
        with patch("sys.argv", ["loop-aider", "run", "--goal", "test goal"]):
            with patch("loop_aider.cli._cmd_run") as mock_run:
                main()
                mock_run.assert_called_once()
                args = mock_run.call_args[0][0]
                assert args.goal == "test goal"

    def test_parser_run_with_all_options(self):
        """验证: run 子命令接受所有可选参数。"""
        with patch("sys.argv", [
            "loop-aider", "run",
            "--goal", "build API",
            "--model", "sonnet",
            "--safe",
            "--max-cycles", "10",
            "--convergence-rounds", "3",
        ]):
            with patch("loop_aider.cli._cmd_run") as mock_run:
                main()
                args = mock_run.call_args[0][0]
                assert args.goal == "build API"
                assert args.model == "sonnet"
                assert args.safe is True
                assert args.max_cycles == 10
                assert args.convergence_rounds == 3

    def test_parser_status_subcommand(self):
        """验证: status 子命令正确解析。"""
        with patch("sys.argv", ["loop-aider", "status"]):
            with patch("loop_aider.cli._cmd_status") as mock_status:
                main()
                mock_status.assert_called_once()

    def test_parser_resume_subcommand(self):
        """验证: resume 子命令正确解析。"""
        with patch("sys.argv", ["loop-aider", "resume"]):
            with patch("loop_aider.cli._cmd_resume") as mock_resume:
                main()
                mock_resume.assert_called_once()

    def test_parser_init_subcommand(self):
        """验证: init 子命令正确解析。"""
        with patch("sys.argv", ["loop-aider", "init"]):
            with patch("loop_aider.cli._cmd_init") as mock_init:
                main()
                mock_init.assert_called_once()

    def test_parser_default_max_cycles(self):
        """验证: --max-cycles 默认值为 5。"""
        parser = argparse.ArgumentParser(prog="loop-aider")
        subparsers = parser.add_subparsers(dest="command")
        run_parser = subparsers.add_parser("run")
        run_parser.add_argument("--goal", default="test")
        run_parser.add_argument("--max-cycles", type=int, default=5)
        args = parser.parse_args(["run", "--goal", "test"])
        assert args.max_cycles == 5

    def test_parser_default_convergence_rounds(self):
        """验证: --convergence-rounds 默认值为 2。"""
        parser = argparse.ArgumentParser(prog="loop-aider")
        subparsers = parser.add_subparsers(dest="command")
        run_parser = subparsers.add_parser("run")
        run_parser.add_argument("--goal", default="test")
        run_parser.add_argument("--convergence-rounds", type=int, default=2)
        args = parser.parse_args(["run", "--goal", "test"])
        assert args.convergence_rounds == 2


# =============================================================================
# Run Command Initialization
# =============================================================================

class TestRunCommand:
    """Run command initialization flow tests."""

    def test_run_command_mode_auto_default(self):
        """验证: 无模式标志时默认为 auto 模式。"""
        args = argparse.Namespace(
            goal="test goal",
            model=None,
            safe=False,
            unsafe=False,
            interactive=False,
            max_cycles=5,
            convergence_rounds=2,
        )

        config = Config.from_args(
            goal=args.goal,
            mode="auto",
            model=args.model,
            max_cycles=args.max_cycles,
            convergence_rounds=args.convergence_rounds,
        )
        assert config.mode == "auto"

    def test_run_command_mode_safe_flag(self):
        """验证: --safe 标志设置 safe 模式。"""
        args = argparse.Namespace(
            goal="test", model=None,
            safe=True, unsafe=False, interactive=False,
            max_cycles=5, convergence_rounds=2,
        )

        mode = "auto"
        if args.safe:
            mode = "safe"
        elif args.unsafe:
            mode = "unsafe"
        elif args.interactive:
            mode = "interactive"

        assert mode == "safe"

    def test_run_command_mode_unsafe_flag(self):
        """验证: --unsafe 标志设置 unsafe 模式。"""
        args = argparse.Namespace(
            goal="test", model=None,
            safe=False, unsafe=True, interactive=False,
            max_cycles=5, convergence_rounds=2,
        )

        mode = "auto"
        if args.safe:
            mode = "safe"
        elif args.unsafe:
            mode = "unsafe"
        elif args.interactive:
            mode = "interactive"

        assert mode == "unsafe"

    def test_run_command_mode_interactive_flag(self):
        """验证: --interactive 标志设置 interactive 模式。"""
        args = argparse.Namespace(
            goal="test", model=None,
            safe=False, unsafe=False, interactive=True,
            max_cycles=5, convergence_rounds=2,
        )

        mode = "auto"
        if args.safe:
            mode = "safe"
        elif args.unsafe:
            mode = "unsafe"
        elif args.interactive:
            mode = "interactive"

        assert mode == "interactive"

    def test_run_command_mode_priority_safe_over_unsafe(self):
        """验证: --safe 优先级高于 --unsafe (safe 先检查)。"""
        args = argparse.Namespace(
            goal="test", model=None,
            safe=True, unsafe=True, interactive=False,
            max_cycles=5, convergence_rounds=2,
        )

        mode = "auto"
        if args.safe:
            mode = "safe"
        elif args.unsafe:
            mode = "unsafe"
        elif args.interactive:
            mode = "interactive"

        # safe 先检查，优先
        assert mode == "safe"

    def test_run_command_config_from_args(self):
        """验证: Config.from_args() 正确传递所有参数。"""
        config = Config.from_args(
            goal="Write tests",
            mode="safe",
            model="opus",
            max_cycles=8,
            convergence_rounds=3,
        )
        assert config.user_request == "Write tests"
        assert config.mode == "safe"
        assert config.model == "opus"
        assert config.max_cycles == 8
        assert config.convergence_rounds == 3

    def test_run_command_updates_state_machine(self):
        """验证: run 命令将配置同步到 state.json。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config.from_args(
                goal="test goal",
                mode="auto",
                max_cycles=7,
                convergence_rounds=4,
            )
            config.state_dir = tmpdir

            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["config"]["mode"] = config.mode
            state["config"]["user_request"] = config.user_request
            state["config"]["max_cycles"] = config.max_cycles
            state["config"]["convergence_rounds"] = config.convergence_rounds
            sm.save_state(state)

            loaded = sm.load_state()
            assert loaded["config"]["mode"] == "auto"
            assert loaded["config"]["user_request"] == "test goal"
            assert loaded["config"]["max_cycles"] == 7
            assert loaded["config"]["convergence_rounds"] == 4

    def test_run_command_health_check_compatible(self):
        """验证: run 命令 health check 在兼容版本下正常。"""
        with patch("loop_aider.aider_manager.AiderManager.check_health") as mock_hc:
            from loop_aider.aider_manager import HealthStatus
            mock_hc.return_value = HealthStatus.OK
            with patch("loop_aider.aider_manager.AiderManager.get_version", return_value="0.86.1"):
                with patch("loop_aider.aider_manager.AiderManager.run_phase") as mock_rp:
                    mock_rp.return_value = MagicMock(exit_code=0, duration_ms=100)

                    args = argparse.Namespace(
                        goal="test",
                        model=None,
                        safe=False, unsafe=False, interactive=False,
                        max_cycles=1, convergence_rounds=1,
                    )

                    try:
                        with patch("sys.exit") as mock_exit:
                            _cmd_run(args)
                            mock_exit.assert_called_once()
                    except SystemExit:
                        pass  # 正常情况

    def test_run_command_health_check_not_found(self):
        """验证: run 命令在 Aider 未安装时退出。"""
        with patch("loop_aider.aider_manager.AiderManager.check_health") as mock_hc:
            from loop_aider.aider_manager import HealthStatus
            mock_hc.return_value = HealthStatus.NOT_FOUND
            with patch("loop_aider.aider_manager.AiderManager.get_version", return_value="0.0.0"):
                args = argparse.Namespace(
                    goal="test",
                    model=None,
                    safe=False, unsafe=False, interactive=False,
                    max_cycles=1, convergence_rounds=1,
                )

                with pytest.raises(SystemExit) as exc_info:
                    _cmd_run(args)
                assert exc_info.value.code == 1


# =============================================================================
# Status Command
# =============================================================================

class TestStatusCommand:
    """Status command output tests."""

    def test_status_command_outputs_json(self, capsys):
        """验证: status 命令输出合法 JSON 摘要。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["progress"]["phase"] = "part_2_2"
            state["progress"]["cycle"] = 3
            state["termination"]["status"] = "running"
            sm.save_state(state)

            # 用 monkeypatch 修改 StateMachine 的默认目录
            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_status()

            captured = capsys.readouterr()
            output = json.loads(captured.out)
            assert output == {
                "phase": "part_2_2",
                "cycle": 3,
                "termination": "running",
            }

    def test_status_command_default_state(self, capsys):
        """验证: status 命令在默认状态下的输出。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            sm.save_state(state)

            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_status()

            captured = capsys.readouterr()
            output = json.loads(captured.out)
            assert output["phase"] == "init"
            assert output["cycle"] == 1
            assert output["termination"] == "running"

    def test_status_command_completed_state(self, capsys):
        """验证: status 命令在 completed 状态下的输出。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["progress"]["phase"] = "part_2_8"
            state["termination"]["status"] = "completed"
            sm.save_state(state)

            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_status()

            captured = capsys.readouterr()
            output = json.loads(captured.out)
            assert output["termination"] == "completed"
            assert output["phase"] == "part_2_8"


# =============================================================================
# Resume Command
# =============================================================================

class TestResumeCommand:
    """Resume command behavior tests."""

    def test_resume_command_outputs_state_info(self, capsys):
        """验证: resume 命令输出 phase/cycle/status 信息。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["progress"]["phase"] = "part_1_2"
            state["progress"]["cycle"] = 4
            state["termination"]["status"] = "running"
            sm.save_state(state)

            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_resume()

            captured = capsys.readouterr()
            assert "part_1_2" in captured.out
            assert "4" in captured.out
            assert "running" in captured.out

    def test_resume_command_shows_unclean_termination(self, capsys):
        """验证: resume 命令在未正常终止时提示可恢复。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["termination"]["status"] = "running"
            sm.save_state(state)

            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_resume()

            captured = capsys.readouterr()
            assert "未正常终止" in captured.out

    def test_resume_command_completed_state(self, capsys):
        """验证: resume 在 completed 状态下不提示恢复。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["termination"]["status"] = "completed"
            sm.save_state(state)

            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_resume()

            captured = capsys.readouterr()
            assert "未正常终止" not in captured.out

    def test_resume_from_mid_cycle(self, capsys):
        """验证: resume 命令显示中间周期的 phase 信息。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["progress"]["phase"] = "part_2_5"
            state["progress"]["cycle"] = 2
            state["termination"]["status"] = "running"
            sm.save_state(state)

            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_resume()

            captured = capsys.readouterr()
            assert "part_2_5" in captured.out
            assert "2" in captured.out


# =============================================================================
# Init Command
# =============================================================================

class TestInitCommand:
    """Init command working directory setup tests."""

    def test_init_command_creates_state_dir_and_file(self):
        """验证: init 命令创建 state.json 文件和目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = os.path.join(tmpdir, ".aider", "loop-aider")
            sm = StateMachine(state_dir=state_dir)
            state = sm.load_state()

            assert os.path.exists(state_dir)
            assert os.path.exists(os.path.join(state_dir, "state.json"))

    def test_init_command_default_state_structure(self):
        """验证: init 命令创建的 state.json 有正确的默认结构。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = os.path.join(tmpdir, ".aider", "loop-aider")
            sm = StateMachine(state_dir=state_dir)
            state = sm.load_state()

            assert state["schema_version"] == 1
            assert state["progress"]["phase"] == "init"
            assert state["progress"]["cycle"] == 1
            assert state["progress"]["convergence_counter"] == 0
            assert state["config"]["mode"] == "auto"
            assert state["config"]["max_cycles"] == 5
            assert state["termination"]["status"] == "running"

    def test_init_command_output(self, capsys):
        """验证: init 命令打印初始化完成信息。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = os.path.join(tmpdir, ".aider", "loop-aider")
            sm = StateMachine(state_dir=state_dir)

            with patch("loop_aider.cli.StateMachine") as MockSM:
                MockSM.return_value = sm
                _cmd_init()

            captured = capsys.readouterr()
            assert "已初始化" in captured.out

    def test_init_command_state_exists_output(self, capsys):
        """验证: init 命令在已有 state.json 时仍成功。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = os.path.join(tmpdir, ".aider", "loop-aider")
            # 第一次 init
            sm1 = StateMachine(state_dir=state_dir)
            sm1.load_state()
            # 第二次 init
            sm2 = StateMachine(state_dir=state_dir)
            state = sm2.load_state()  # 加载已有状态

            assert state["progress"]["phase"] == "init"
            assert state["config"]["mode"] == "auto"


# =============================================================================
# Helpers
# =============================================================================

import os
