"""
tests/test_convergence.py —— ConvergenceEngine 收敛引擎单元测试。

测试 convergence_counter 操作表（P5/P6 合并）、should_terminate()
终止判定、Part 1 语义收敛检测、优先级操作表。
"""

import pytest
from unittest.mock import MagicMock

from loop_aider.convergence import (
    ConvergenceEngine, ConvergenceResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_state():
    """创建一个基础 state 字典，包含 Part 1 完成状态。"""
    return {
        "schema_version": 1,
        "progress": {
            "phase": "part_2_2",
            "cycle": 1,
            "convergence_counter": 0,
            "part1_round": 0,
            "new_issues_this_round": False,
            "phase_transitions": [
                {"from": "init", "to": "part_1_1"},
                {"from": "part_1_1", "to": "part_1_2"},
                {"from": "part_1_2", "to": "part_1_3"},
                {"from": "part_1_3", "to": "part_2_1"},
                {"from": "part_2_1", "to": "part_2_2"},
            ],
        },
        "config": {
            "max_cycles": 5,
            "convergence_rounds": 2,
            "route_repeat_max": 3,
        },
        "artifacts": {
            "requirements.md": {"present": True},
            "direction.md": {"present": True},
            "solution.md": {"present": True},
        },
        "issues": {
            "active": {"p0": [], "p1": [], "p2": []},
        },
        "termination": {"status": "running"},
    }


@pytest.fixture
def engine():
    """创建收敛引擎实例。"""
    return ConvergenceEngine()


# ---------------------------------------------------------------------------
# Test Cases: convergence_counter Update (P5/P6)
# ---------------------------------------------------------------------------

class TestConvergenceCounter:
    """convergence_counter 操作表测试（P5/P6 合并规则）。"""

    def test_p5_new_issues_resets_counter(self, base_state, engine):
        """测试: 有新问题时 counter 重置为 0。"""
        base_state["progress"]["convergence_counter"] = 3
        result = engine.update_counter(
            base_state, new_issues=True, issues_resolved=False
        )
        assert result == 0
        assert base_state["progress"]["convergence_counter"] == 0

    def test_p5_no_new_issues_increments(self, base_state, engine):
        """测试: 无新问题且有问题解决时 counter += 1。"""
        base_state["progress"]["convergence_counter"] = 1
        result = engine.update_counter(
            base_state, new_issues=False, issues_resolved=True
        )
        assert result == 2
        assert base_state["progress"]["convergence_counter"] == 2

    def test_p6_stagnation_increments(self, base_state, engine):
        """测试: 停滞状态（无新问题+无解决）缓慢递增。"""
        base_state["progress"]["convergence_counter"] = 0
        result = engine.update_counter(
            base_state, new_issues=False, issues_resolved=False
        )
        assert result == 1

    def test_p6_external_error_no_penalty(self, base_state, engine):
        """测试: 非零退出码时 counter 不变。"""
        base_state["progress"]["convergence_counter"] = 3
        result = engine.update_counter(
            base_state, new_issues=False, issues_resolved=True,
            exit_code=1
        )
        assert result == 3  # 不变

    def test_p6_timeout_no_penalty(self, base_state, engine):
        """测试: 超时时 counter 不变。"""
        base_state["progress"]["convergence_counter"] = 4
        result = engine.update_counter(
            base_state, new_issues=False, issues_resolved=False,
            timed_out=True
        )
        assert result == 4  # 不变

    def test_counter_persists_in_state(self, base_state, engine):
        """测试: counter 正确持久化在 state 中。"""
        engine.update_counter(base_state, new_issues=False, issues_resolved=True)
        assert base_state["progress"]["convergence_counter"] == 1


# ---------------------------------------------------------------------------
# Test Cases: Part 1 Semantic Convergence
# ---------------------------------------------------------------------------

class TestPart1SemanticConvergence:
    """Part 1 语义收敛检测测试。"""

    def test_part1_converged_when_all_done(self, base_state, engine):
        """测试: 三阶段完成后语义收敛。"""
        result = engine.check_part1_semantic_convergence(base_state)
        assert result.converged is True
        assert result.semver_converged is True

    def test_part1_not_converged_missing_phase(self, base_state, engine):
        """测试: 缺少阶段时未收敛。"""
        base_state["progress"]["phase_transitions"] = [
            {"from": "init", "to": "part_1_1"},
            {"from": "part_1_1", "to": "part_1_2"},
        ]
        base_state["progress"]["phase"] = "part_1_2"
        result = engine.check_part1_semantic_convergence(base_state)
        assert result.converged is False

    def test_part1_not_converged_missing_artifacts(self, base_state, engine):
        """测试: 产物缺失时未收敛。"""
        base_state["artifacts"] = {}
        result = engine.check_part1_semantic_convergence(base_state)
        assert result.converged is False

    def test_part1_not_converged_with_p0_issues(self, base_state, engine):
        """测试: 存在 P0 设计问题时未收敛。"""
        base_state["issues"]["active"]["p0"] = [{"title": "致命设计缺陷"}]
        result = engine.check_part1_semantic_convergence(base_state)
        assert result.converged is False

    def test_part1_not_converged_with_p1_issues(self, base_state, engine):
        """测试: 存在 P1 设计问题时未收敛。"""
        base_state["issues"]["active"]["p1"] = [{"title": "设计冲突"}]
        result = engine.check_part1_semantic_convergence(base_state)
        assert result.converged is False


# ---------------------------------------------------------------------------
# Test Cases: should_terminate() 终止判定
# ---------------------------------------------------------------------------

class TestShouldTerminate:
    """should_terminate() 终止判定测试。"""

    def test_terminate_counter_met(self, base_state, engine):
        """测试: counter 达到阈值时终止。"""
        base_state["progress"]["convergence_counter"] = 5
        base_state["config"]["convergence_rounds"] = 2
        result = engine.should_terminate(base_state)
        assert result.converged is True

    def test_terminate_counter_not_met(self, base_state, engine):
        """测试: counter 未达到阈值时不终止。"""
        base_state["progress"]["convergence_counter"] = 0
        base_state["config"]["convergence_rounds"] = 2
        result = engine.should_terminate(base_state)
        assert result.converged is False

    def test_terminate_max_cycles_exceeded(self, base_state, engine):
        """测试: 周期超限时终止。"""
        base_state["progress"]["cycle"] = 10
        base_state["config"]["max_cycles"] = 5
        result = engine.should_terminate(base_state)
        assert result.converged is True

    def test_terminate_user_completed(self, base_state, engine):
        """测试: 用户标记完成时终止。"""
        base_state["termination"]["status"] = "completed"
        result = engine.should_terminate(base_state)
        assert result.converged is True

    def test_terminate_p0_unresolved(self, base_state, engine):
        """测试: 未解决 P0 问题时终止。"""
        base_state["issues"]["active"]["p0"] = [{"title": "致命"}]
        result = engine.should_terminate(base_state)
        assert result.converged is True

    def test_terminate_counter_met_but_part1_not(self, base_state, engine):
        """测试: counter 达标但 Part1 未收敛时不终止。"""
        base_state["progress"]["convergence_counter"] = 5
        base_state["config"]["convergence_rounds"] = 2
        # Part1 未收敛 (移除产物)
        base_state["artifacts"] = {}
        result = engine.should_terminate(base_state)
        # counter >= rounds 但 Part1 未收敛 → 不终止
        assert result.converged is False


# ---------------------------------------------------------------------------
# Test Cases: Priority Operation Table
# ---------------------------------------------------------------------------

class TestOperationTable:
    """优先级操作表测试。"""

    def test_op_table_new_issues_resolved_ok(self, engine):
        """测试: 有新问题+有解决+成功 → reset。"""
        op = engine.get_operation(True, True, True)
        assert op["action"] == "reset_counter"
        assert op["next"] == "continue"

    def test_op_table_no_issues_resolved_ok(self, engine):
        """测试: 无新问题+有解决+成功 → inc。"""
        op = engine.get_operation(False, True, True)
        assert op["action"] == "inc_counter"
        assert op["next"] == "continue"

    def test_op_table_no_issues_no_resolve_fail(self, engine):
        """测试: 无新问题+无解决+失败 → keep。"""
        op = engine.get_operation(False, False, False)
        assert op["action"] == "keep_counter"
        assert op["next"] == "retry"

    def test_op_table_stagnation_ok(self, engine):
        """测试: 无新问题+无解决+成功 → inc。"""
        op = engine.get_operation(False, False, True)
        assert op["action"] == "inc_counter"

    def test_apply_operation_resets_counter(self, base_state, engine):
        """测试: apply_operation 重置 counter。"""
        base_state["progress"]["convergence_counter"] = 5
        result = engine.apply_operation(base_state, True, True, True)
        assert result == 0

    def test_apply_operation_increments_counter(self, base_state, engine):
        """测试: apply_operation 递增 counter。"""
        base_state["progress"]["convergence_counter"] = 2
        result = engine.apply_operation(base_state, False, True, True)
        assert result == 3

    def test_apply_operation_keeps_counter(self, base_state, engine):
        """测试: apply_operation 保持 counter。"""
        base_state["progress"]["convergence_counter"] = 4
        result = engine.apply_operation(base_state, False, False, False)
        assert result == 4
