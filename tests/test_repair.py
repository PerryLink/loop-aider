"""
tests/test_repair.py —— Repair 系统单元测试。

测试 repair_context 生命周期（null → active → consumed）、
并行修复支持、修复策略生成。
"""

import pytest
from unittest.mock import MagicMock, patch

from loop_aider.repair import (
    RepairManager, RepairContext,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_state():
    """创建一个基础 state 字典。"""
    return {
        "progress": {
            "phase": "part_2_2",
            "cycle": 1,
            "repair_context": None,
            "convergence_counter": 0,
        },
        "config": {
            "user_request": "test goal",
            "repair_max_attempts": 3,
        },
    }


@pytest.fixture
def repair_mgr():
    """创建 RepairManager 实例。"""
    return RepairManager()


@pytest.fixture
def p2_issues():
    """P2 问题列表。"""
    return ["语法错误: 缺少冒号", "ImportError: 缺少依赖"]


# ---------------------------------------------------------------------------
# Test Cases: RepairContext Lifecycle
# ---------------------------------------------------------------------------

class TestRepairContextLifecycle:
    """repair_context 生命周期测试。"""

    def test_create_context_null_to_active(self, base_state, repair_mgr, p2_issues):
        """测试: 创建修复上下文 (null → active)。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)
        assert ctx.status == "active"
        assert ctx.is_active is True
        assert ctx.is_null is False
        assert len(ctx.issues) == 2

    def test_create_context_sets_state(self, base_state, repair_mgr, p2_issues):
        """测试: 创建上下文后 state 中的 repair_context 同步更新。"""
        repair_mgr.create_context(base_state, p2_issues)
        ctx_data = base_state["progress"]["repair_context"]
        assert ctx_data is not None
        assert ctx_data["status"] == "active"

    def test_consume_context_active_to_consumed(self, base_state, repair_mgr, p2_issues):
        """测试: 消费修复上下文 (active → consumed)。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)
        consumed = repair_mgr.consume_context(base_state, ctx, success=True)
        assert consumed.status == "consumed"
        assert consumed.is_consumed is True
        assert consumed.result == "success"

    def test_consume_failed_context(self, base_state, repair_mgr, p2_issues):
        """测试: 失败的修复上下文消费。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)
        ctx.attempt_count = 3
        consumed = repair_mgr.consume_context(base_state, ctx, success=False)
        assert consumed.status == "consumed"
        assert "failed" in consumed.result

    def test_reset_context_to_null(self, base_state, repair_mgr, p2_issues):
        """测试: 重置修复上下文为 null。"""
        repair_mgr.create_context(base_state, p2_issues)
        repair_mgr.reset_context(base_state)
        assert base_state["progress"]["repair_context"] is None

    def test_consume_auto_resets(self, base_state, repair_mgr, p2_issues):
        """测试: consume 后自动重置。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)
        repair_mgr.consume_context(base_state, ctx, success=True)
        assert base_state["progress"]["repair_context"] is None

    def test_is_repair_active(self, base_state, repair_mgr, p2_issues):
        """测试: is_repair_active 检测。"""
        assert repair_mgr.is_repair_active(base_state) is False
        repair_mgr.create_context(base_state, p2_issues)
        assert repair_mgr.is_repair_active(base_state) is True

    def test_get_context_returns_none_when_null(self, base_state, repair_mgr):
        """测试: 无上下文时 get_context 返回 None。"""
        ctx = repair_mgr.get_context(base_state)
        assert ctx is None

    def test_get_context_returns_object(self, base_state, repair_mgr, p2_issues):
        """测试: 有上下文时 get_context 返回 RepairContext。"""
        repair_mgr.create_context(base_state, p2_issues)
        ctx = repair_mgr.get_context(base_state)
        assert ctx is not None
        assert isinstance(ctx, RepairContext)
        assert ctx.is_active is True


# ---------------------------------------------------------------------------
# Test Cases: Repair Execution
# ---------------------------------------------------------------------------

class TestRepairExecution:
    """修复执行测试。"""

    def test_run_repair_success(self, base_state, repair_mgr, p2_issues):
        """测试: 修复成功。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)

        mock_aider_mgr = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.exit_code = 0
        mock_result.audit_issues = []
        mock_aider_mgr.run_phase.return_value = mock_result

        result = repair_mgr.run_repair(base_state, ctx, mock_aider_mgr)
        assert result is True
        assert ctx.attempt_count == 1

    def test_run_repair_failure_aider_error(self, base_state, repair_mgr, p2_issues):
        """测试: 修复失败（Aider 返回错误）。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)

        mock_aider_mgr = MagicMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.exit_code = 1
        mock_aider_mgr.run_phase.return_value = mock_result

        result = repair_mgr.run_repair(base_state, ctx, mock_aider_mgr)
        assert result is False
        assert ctx.attempt_count == 1

    def test_run_repair_exhausted_attempts(self, base_state, repair_mgr, p2_issues):
        """测试: 修复尝试次数耗尽。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)
        ctx.attempt_count = ctx.max_attempts  # 已达上限

        mock_aider_mgr = MagicMock()
        result = repair_mgr.run_repair(base_state, ctx, mock_aider_mgr)
        assert result is False

    def test_run_repair_handles_exception(self, base_state, repair_mgr, p2_issues):
        """测试: 修复过程中异常处理。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)

        mock_aider_mgr = MagicMock()
        mock_aider_mgr.run_phase.side_effect = RuntimeError("test error")

        result = repair_mgr.run_repair(base_state, ctx, mock_aider_mgr)
        assert result is False

    def test_run_repair_detects_new_p0_p1(self, base_state, repair_mgr, p2_issues):
        """测试: 修复引入新的 P0/P1 问题时失败。"""
        ctx = repair_mgr.create_context(base_state, p2_issues)

        mock_aider_mgr = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.exit_code = 0
        mock_p0_issue = MagicMock()
        mock_p0_issue.severity = "P0"
        mock_result.audit_issues = [mock_p0_issue]
        mock_aider_mgr.run_phase.return_value = mock_result

        result = repair_mgr.run_repair(base_state, ctx, mock_aider_mgr)
        assert result is False


# ---------------------------------------------------------------------------
# Test Cases: Parallel Repair
# ---------------------------------------------------------------------------

class TestParallelRepair:
    """并行修复支持测试。"""

    def test_can_parallelize_single_issue(self, repair_mgr):
        """测试: 单个问题不需要并行。"""
        result = repair_mgr.can_parallelize(["问题1"])
        assert result is False

    def test_can_parallelize_few_issues(self, repair_mgr):
        """测试: 少量问题可以并行。"""
        result = repair_mgr.can_parallelize(["问题1", "问题2"])
        assert result is True

    def test_can_parallelize_many_issues(self, repair_mgr):
        """测试: 过多问题时仍可尝试并行。"""
        result = repair_mgr.can_parallelize(["a", "b", "c", "d"])
        assert result is False  # > 3 时不并行

    def test_split_into_batches(self, repair_mgr):
        """测试: 问题批次拆分。"""
        issues = ["问题1", "问题2", "问题3", "问题4", "问题5"]
        batches = repair_mgr.split_issues_into_batches(issues, max_per_batch=2)
        assert len(batches) == 3
        assert len(batches[0]) == 2

    def test_split_single_batch(self, repair_mgr):
        """测试: 单个批次。"""
        issues = ["问题1"]
        batches = repair_mgr.split_issues_into_batches(issues)
        assert len(batches) == 1


# ---------------------------------------------------------------------------
# Test Cases: Fix Strategy Generation
# ---------------------------------------------------------------------------

class TestFixStrategy:
    """修复策略生成测试。"""

    def test_syntax_error_strategy(self, repair_mgr):
        """测试: 语法错误修复策略。"""
        strategy = repair_mgr._generate_fix_strategy("syntax error")
        assert "语法" in strategy or "syntax" in strategy.lower()

    def test_import_error_strategy(self, repair_mgr):
        """测试: 导入错误修复策略。"""
        strategy = repair_mgr._generate_fix_strategy("import error")
        assert "导入" in strategy or "import" in strategy.lower()

    def test_test_failure_strategy(self, repair_mgr):
        """测试: 测试失败修复策略。"""
        strategy = repair_mgr._generate_fix_strategy("test failure")
        assert "测试" in strategy or "test" in strategy.lower()

    def test_unknown_issue_strategy(self, repair_mgr):
        """测试: 未知问题默认修复策略。"""
        strategy = repair_mgr._generate_fix_strategy("unknown weird issue")
        assert "unknown weird issue" in strategy

    def test_stop_signal_strategy(self, repair_mgr):
        """测试: stop_signal 缺失修复策略。"""
        strategy = repair_mgr._generate_fix_strategy("missing stop_signal")
        assert "stop_signal" in strategy.lower() or "信号" in strategy
