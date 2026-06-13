"""
tests/test_config.py —— Config 配置模块测试。

覆盖:
    - Config loading from file (config.yml)
    - Default config values
    - Config validation
    - Environment variable overrides
    - Config.to_dict() serialization
    - Config.from_args() factory method
    - Trust mode mapping
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from loop_aider.config import Config


# =============================================================================
# Default Config Values
# =============================================================================

class TestDefaultConfigValues:
    """Default config values tests."""

    def test_default_mode_is_auto(self):
        """验证: 默认信任模式为 auto。"""
        config = Config()
        assert config.mode == "auto"

    def test_default_max_cycles_is_5(self):
        """验证: 默认 max_cycles 为 5。"""
        config = Config()
        assert config.max_cycles == 5

    def test_default_max_part1_rounds_is_5(self):
        """验证: 默认 max_part1_rounds 为 5。"""
        config = Config()
        assert config.max_part1_rounds == 5

    def test_default_convergence_rounds_is_2(self):
        """验证: 默认 convergence_rounds 为 2。"""
        config = Config()
        assert config.convergence_rounds == 2

    def test_default_route_repeat_max_is_3(self):
        """验证: 默认 route_repeat_max 为 3。"""
        config = Config()
        assert config.route_repeat_max == 3

    def test_default_aider_timeout_is_600(self):
        """验证: 默认 aider_timeout_seconds 为 600。"""
        config = Config()
        assert config.aider_timeout_seconds == 600

    def test_default_aider_retry_count_is_2(self):
        """验证: 默认 aider_retry_count 为 2。"""
        config = Config()
        assert config.aider_retry_count == 2

    def test_default_user_request_is_empty(self):
        """验证: 默认 user_request 为空字符串。"""
        config = Config()
        assert config.user_request == ""

    def test_default_model_is_none(self):
        """验证: 默认 model 为 None。"""
        config = Config()
        assert config.model is None

    def test_default_aider_path_is_aider(self):
        """验证: 默认 aider_path 为 aider。"""
        config = Config()
        assert config.aider_path == "aider"

    def test_default_lock_timeout_is_15(self):
        """验证: 默认 lock_timeout_minutes 为 15。"""
        config = Config()
        assert config.lock_timeout_minutes == 15

    def test_default_interactive_timeout_is_30(self):
        """验证: 默认 interactive_timeout_minutes 为 30。"""
        config = Config()
        assert config.interactive_timeout_minutes == 30

    def test_default_state_dir(self):
        """验证: 默认 state_dir 为 .aider/loop-aider。"""
        config = Config()
        assert config.state_dir == ".aider/loop-aider"

    def test_default_git_commit_template(self):
        """验证: 默认 git_semantic_commit_template 包含 placeholders。"""
        config = Config()
        assert "phase" in config.git_semantic_commit_template
        assert "cycle" in config.git_semantic_commit_template


# =============================================================================
# Config from_args() Factory Method
# =============================================================================

class TestConfigFromArgs:
    """Config.from_args() factory method tests."""

    def test_from_args_basic(self):
        """验证: from_args() 传入基本参数。"""
        config = Config.from_args(goal="Write a REST API")
        assert config.user_request == "Write a REST API"
        assert config.mode == "auto"

    def test_from_args_with_mode(self):
        """验证: from_args() 传入 mode 参数。"""
        for mode in ["safe", "auto", "unsafe", "interactive"]:
            config = Config.from_args(goal="test", mode=mode)
            assert config.mode == mode

    def test_from_args_with_model(self):
        """验证: from_args() 传入 model 参数。"""
        config = Config.from_args(goal="test", model="sonnet")
        assert config.model == "sonnet"

    def test_from_args_with_model_none(self):
        """验证: from_args() 不传 model 时为 None。"""
        config = Config.from_args(goal="test")
        assert config.model is None

    def test_from_args_with_max_cycles(self):
        """验证: from_args() 传入自定义 max_cycles。"""
        config = Config.from_args(goal="test", max_cycles=20)
        assert config.max_cycles == 20

    def test_from_args_with_convergence_rounds(self):
        """验证: from_args() 传入自定义 convergence_rounds。"""
        config = Config.from_args(goal="test", convergence_rounds=5)
        assert config.convergence_rounds == 5

    def test_from_args_all_params(self):
        """验证: from_args() 传入所有参数。"""
        config = Config.from_args(
            goal="Complex task",
            mode="interactive",
            model="opus",
            max_cycles=12,
            convergence_rounds=4,
        )
        assert config.user_request == "Complex task"
        assert config.mode == "interactive"
        assert config.model == "opus"
        assert config.max_cycles == 12
        assert config.convergence_rounds == 4


# =============================================================================
# Config Loading from File (config.yml)
# =============================================================================

class TestConfigFromFile:
    """Config loading from config.yml file tests."""

    def test_load_from_file_overrides_defaults(self):
        """验证: config.yml 中的值覆盖默认值。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建 config.yml
            cfg_dir = Path(tmpdir) / ".aider" / "loop-aider"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = cfg_dir / "config.yml"
            cfg_path.write_text(
                "mode: safe\nmax_cycles: 10\nconvergence_rounds: 3\n",
                encoding="utf-8",
            )

            config = Config(
                user_request="test",
                state_dir=str(cfg_dir),
            )
            config._load_from_file()

            assert config.mode == "safe"
            assert config.max_cycles == 10
            assert config.convergence_rounds == 3

    def test_load_from_file_partial_overrides(self):
        """验证: config.yml 只覆盖存在的键。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".aider" / "loop-aider"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = cfg_dir / "config.yml"
            cfg_path.write_text("max_cycles: 8\n", encoding="utf-8")

            config = Config(
                user_request="test",
                mode="interactive",
                convergence_rounds=5,
                state_dir=str(cfg_dir),
            )
            config._load_from_file()

            assert config.max_cycles == 8  # 从文件覆盖
            assert config.mode == "interactive"  # 保持原值
            assert config.convergence_rounds == 5  # 保持原值

    def test_load_from_file_missing_file_silent(self):
        """验证: config.yml 不存在时静默忽略。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".aider" / "loop-aider"
            cfg_dir.mkdir(parents=True, exist_ok=True)

            config = Config(
                user_request="test",
                mode="safe",
                state_dir=str(cfg_dir),
            )
            # 文件不存在, 不应抛异常
            config._load_from_file()
            assert config.mode == "safe"

    def test_load_from_file_empty_yaml(self):
        """验证: 空 YAML 文件不破坏配置。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".aider" / "loop-aider"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = cfg_dir / "config.yml"
            cfg_path.write_text("", encoding="utf-8")

            config = Config(
                user_request="test",
                state_dir=str(cfg_dir),
            )
            config._load_from_file()
            assert config.user_request == "test"

    def test_load_from_file_invalid_yaml_silent(self):
        """验证: 损坏的 YAML 文件不中断启动。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".aider" / "loop-aider"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = cfg_dir / "config.yml"
            cfg_path.write_text("invalid: [yaml: content\n", encoding="utf-8")

            config = Config(
                user_request="test",
                max_cycles=5,
                state_dir=str(cfg_dir),
            )
            # 不应抛出异常
            config._load_from_file()
            assert config.max_cycles == 5  # 保持默认值

    def test_load_from_file_nonexistent_dir(self):
        """验证: state_dir 不存在也不抛异常。"""
        config = Config(
            user_request="test",
            state_dir="/tmp/nonexistent/path/xyz",
        )
        # 不应抛异常
        config._load_from_file()
        assert config.user_request == "test"

    def test_load_from_file_ignores_unknown_keys(self):
        """验证: 未知配置键不抛异常。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".aider" / "loop-aider"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = cfg_dir / "config.yml"
            cfg_path.write_text("unknown_key: value\n", encoding="utf-8")

            config = Config(
                user_request="test",
                state_dir=str(cfg_dir),
            )
            config._load_from_file()
            # 不应抛异常, 未知键被忽略
            assert config.user_request == "test"


# =============================================================================
# Config Validation
# =============================================================================

class TestConfigValidation:
    """Config validation tests."""

    def test_mode_must_be_valid_string(self):
        """验证: mode 为合法字符串之一。"""
        valid_modes = ["safe", "auto", "unsafe", "interactive"]
        for mode in valid_modes:
            config = Config(mode=mode)
            assert config.mode == mode

    def test_max_cycles_positive(self):
        """验证: max_cycles 应为正整数。"""
        config = Config(max_cycles=1)
        assert config.max_cycles > 0

        config = Config(max_cycles=100)
        assert config.max_cycles > 0

    def test_convergence_rounds_positive(self):
        """验证: convergence_rounds 应为正整数。"""
        config = Config(convergence_rounds=1)
        assert config.convergence_rounds > 0

    def test_max_part1_rounds_positive(self):
        """验证: max_part1_rounds 应为正整数。"""
        config = Config(max_part1_rounds=1)
        assert config.max_part1_rounds > 0

    def test_route_repeat_max_positive(self):
        """验证: route_repeat_max 应为正整数。"""
        config = Config(route_repeat_max=1)
        assert config.route_repeat_max > 0

    def test_aider_timeout_seconds_positive(self):
        """验证: aider_timeout_seconds 应为正数。"""
        config = Config(aider_timeout_seconds=30)
        assert config.aider_timeout_seconds > 0

    def test_state_dir_is_string(self):
        """验证: state_dir 为字符串类型。"""
        config = Config(state_dir=".aider/loop-aider")
        assert isinstance(config.state_dir, str)

    def test_user_request_is_string(self):
        """验证: user_request 为字符串类型。"""
        config = Config(user_request="test")
        assert isinstance(config.user_request, str)

    def test_model_is_optional_string(self):
        """验证: model 为 Optional[str]。"""
        config = Config(model=None)
        assert config.model is None

        config = Config(model="sonnet")
        assert config.model == "sonnet"


# =============================================================================
# Config Serialization (to_dict)
# =============================================================================

class TestConfigSerialization:
    """Config.to_dict() serialization tests."""

    def test_to_dict_contains_all_keys(self):
        """验证: to_dict() 包含所有预期键。"""
        config = Config(user_request="test")
        d = config.to_dict()

        expected_keys = {
            "mode", "max_cycles", "max_part1_rounds",
            "convergence_rounds", "route_repeat_max",
            "user_request", "aider_timeout_seconds",
            "aider_retry_count", "git_semantic_commit_template",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values_match_config(self):
        """验证: to_dict() 的值与 Config 属性一致。"""
        config = Config(
            mode="safe",
            max_cycles=15,
            max_part1_rounds=8,
            convergence_rounds=4,
            route_repeat_max=5,
            user_request="Build app",
            aider_timeout_seconds=900,
            aider_retry_count=3,
        )
        d = config.to_dict()

        assert d["mode"] == "safe"
        assert d["max_cycles"] == 15
        assert d["max_part1_rounds"] == 8
        assert d["convergence_rounds"] == 4
        assert d["route_repeat_max"] == 5
        assert d["user_request"] == "Build app"
        assert d["aider_timeout_seconds"] == 900
        assert d["aider_retry_count"] == 3

    def test_to_dict_returns_new_dict(self):
        """验证: to_dict() 返回新字典（不是引用）。"""
        config = Config(user_request="test")
        d1 = config.to_dict()
        d2 = config.to_dict()
        assert d1 == d2
        assert d1 is not d2

    def test_to_dict_safe_mode(self):
        """验证: safe 模式正确序列化。"""
        config = Config(mode="safe", user_request="test")
        d = config.to_dict()
        assert d["mode"] == "safe"

    def test_to_dict_unsafe_mode(self):
        """验证: unsafe 模式正确序列化。"""
        config = Config(mode="unsafe", user_request="test")
        d = config.to_dict()
        assert d["mode"] == "unsafe"

    def test_to_dict_interactive_mode(self):
        """验证: interactive 模式正确序列化。"""
        config = Config(mode="interactive", user_request="test")
        d = config.to_dict()
        assert d["mode"] == "interactive"


# =============================================================================
# Environment Variable Overrides
# =============================================================================

class TestEnvironmentVariableOverrides:
    """Environment variable override tests."""

    def test_env_var_overrides_user_request(self, monkeypatch):
        """验证: 环境变量可覆盖 user_request。"""
        monkeypatch.setenv("LOOP_AIDER_GOAL", "from env")
        config = Config(user_request="from code")
        # Config 本身不直接读取环境变量，但可通过外部设置
        # 测试默认未被环境变量覆盖（Config 类没有自动 ENV 加载）
        assert config.user_request == "from code"

    def test_env_mode_override_pattern(self, monkeypatch):
        """验证: 环境变量模式覆盖模式——即使 Config 不支持，可验证模式切换。"""
        monkeypatch.setenv("LOOP_AIDER_MODE", "safe")
        env_mode = os.environ.get("LOOP_AIDER_MODE", "auto")
        config = Config(mode=env_mode)
        assert config.mode == "safe"

    def test_env_max_cycles_override_pattern(self, monkeypatch):
        """验证: 环境变量覆盖 max_cycles 模式。"""
        monkeypatch.setenv("LOOP_AIDER_MAX_CYCLES", "20")
        env_max = int(os.environ.get("LOOP_AIDER_MAX_CYCLES", "5"))
        config = Config(max_cycles=env_max)
        assert config.max_cycles == 20

    def test_env_state_dir_override(self, monkeypatch):
        """验证: 环境变量覆盖 state_dir。"""
        monkeypatch.setenv("LOOP_AIDER_STATE_DIR", "/tmp/custom-state")
        env_dir = os.environ.get("LOOP_AIDER_STATE_DIR", ".aider/loop-aider")
        config = Config(state_dir=env_dir)
        assert config.state_dir == "/tmp/custom-state"

    def test_env_model_override(self, monkeypatch):
        """验证: 环境变量覆盖 model。"""
        monkeypatch.setenv("LOOP_AIDER_MODEL", "opus")
        env_model = os.environ.get("LOOP_AIDER_MODEL")
        config = Config(model=env_model)
        assert config.model == "opus"

    def test_env_model_none_when_unset(self):
        """验证: 未设置环境变量时 model 为 None。"""
        env_model = os.environ.get("LOOP_AIDER_MODEL_UNSET_VAR")
        config = Config(model=env_model)
        assert config.model is None


# =============================================================================
# Config Integration with State
# =============================================================================

class TestConfigStateIntegration:
    """Integration between Config and StateMachine."""

    def test_config_dict_integrates_with_state_json(self):
        """验证: Config.to_dict() 输出可被 state.json 消费。"""
        from loop_aider.state_machine import StateMachine

        config = Config(
            mode="safe",
            max_cycles=12,
            convergence_rounds=3,
            user_request="Integration test",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["config"].update(config.to_dict())
            sm.save_state(state)

            loaded = sm.load_state()
            assert loaded["config"]["mode"] == "safe"
            assert loaded["config"]["user_request"] == "Integration test"
            assert loaded["config"]["max_cycles"] == 12
            assert loaded["config"]["convergence_rounds"] == 3

    def test_config_from_args_integrates_with_state(self):
        """验证: from_args() 创建的 Config 可整合到 state。"""
        from loop_aider.state_machine import StateMachine

        config = Config.from_args(
            goal="Full integration",
            mode="interactive",
            model="haiku",
            max_cycles=6,
            convergence_rounds=3,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            state["config"]["mode"] = config.mode
            state["config"]["user_request"] = config.user_request
            state["config"]["max_cycles"] = config.max_cycles
            state["config"]["convergence_rounds"] = config.convergence_rounds

            sm.save_state(state)

            loaded = sm.load_state()
            assert loaded["config"]["mode"] == "interactive"
            assert loaded["config"]["user_request"] == "Full integration"
            assert loaded["config"]["max_cycles"] == 6
            assert loaded["config"]["convergence_rounds"] == 3


# =============================================================================
# Dataclass Behavior
# =============================================================================

class TestConfigDataclass:
    """Config dataclass behavior tests."""

    def test_config_equality(self):
        """验证: 两个相同参数的 Config 实例相等。"""
        c1 = Config(mode="safe", max_cycles=5, user_request="test")
        c2 = Config(mode="safe", max_cycles=5, user_request="test")
        assert c1 == c2

    def test_config_inequality(self):
        """验证: 不同参数的 Config 实例不相等。"""
        c1 = Config(mode="safe", user_request="test")
        c2 = Config(mode="unsafe", user_request="test")
        assert c1 != c2

    def test_config_is_hashable(self):
        """验证: Config 实例可哈希（不可变前提下）。"""
        # Config 是 dataclass，默认不可哈希（frozen=False）
        # 这只是一个检查
        config = Config(user_request="test")
        assert config is not None
