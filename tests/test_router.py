"""
tests/test_router.py —— Router 路由系统单元测试。

测试 P0/P1/P2 三层检测逻辑、routing_decision() 主函数、
以及路由历史记录。
"""

import pytest
from unittest.mock import MagicMock

from loop_aider.router import (
    Router, RoutingDecision, RoutingAction,
    P1_DESIGN_CONDITIONS, P1_NEGATION_CONDITIONS,
    routing_decision as route_fn,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_state():
    """创建一个基础 state 字典用于测试。"""
    return {
        "schema_version": 1,
        "progress": {
            "phase": "part_2_2",
            "cycle": 1,
            "convergence_counter": 0,
            "part1_round": 0,
            "new_issues_this_round": False,
            "new_issues_last_round": False,
            "repair_context": None,
            "phase_transitions": [],
        },
        "config": {
            "mode": "auto",
            "max_cycles": 5,
            "convergence_rounds": 2,
            "route_repeat_max": 3,
            "user_request": "test goal",
        },
        "issues": {
            "active": {"p0": [], "p1": [], "p2": []},
        },
        "routing_repeat_tracker": {},
        "routing_history": [],
    }


@pytest.fixture
def mock_aider_result():
    """创建一个模拟的 AiderResult。"""
    result = MagicMock()
    result.affected_files = ["src/main.py", "src/utils.py"]
    result.added_lines = 50
    result.removed_lines = 10
    result.exit_code = 0
    result.timed_out = False
    result.success = True
    return result


@pytest.fixture
def mock_audit_issue():
    """创建一个模拟的审计 Issue。"""
    issue = MagicMock()
    issue.severity = "P2"
    issue.title = "测试问题"
    issue.source = "A1_output_validity"
    return issue


# ---------------------------------------------------------------------------
# Test Cases: P0 Detection
# ---------------------------------------------------------------------------

class TestP0Detection:
    """P0 致命设计问题检测测试。"""

    def test_detect_p0_empty(self):
        """测试: 无 P0 问题时返回空列表。"""
        router = Router()
        issues = [
            MagicMock(severity="P1", title="问题1"),
            MagicMock(severity="P2", title="问题2"),
        ]
        result = router.detect_p0(issues)
        assert result == []

    def test_detect_p0_found(self):
        """测试: 检测到 P0 致命问题。"""
        router = Router()
        issues = [
            MagicMock(severity="P0", title="后门代码"),
            MagicMock(severity="P1", title="问题1"),
        ]
        result = router.detect_p0(issues)
        assert len(result) == 1
        assert "后门代码" in result[0]

    def test_detect_p0_multiple(self):
        """测试: 多个 P0 问题同时检测。"""
        router = Router()
        issues = [
            MagicMock(severity="P0", title="后门"),
            MagicMock(severity="P0", title="敏感凭证泄露"),
            MagicMock(severity="P2", title="风格问题"),
        ]
        result = router.detect_p0(issues)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Test Cases: P1 Decision Tree
# ---------------------------------------------------------------------------

class TestP1DecisionTree:
    """P1 决策树测试（5 设计条件 + 4 否定条件）。"""

    def test_p1_no_issues(self, base_state, mock_aider_result):
        """测试: 无 P1 问题时决策树不触发。"""
        router = Router()
        audit_issues = [MagicMock(severity="P2", title="")]
        should_repeat, reason = router.evaluate_p1_decision_tree(
            base_state, mock_aider_result, audit_issues
        )
        # 否定条件 4: 只有 P2 问题 → 不触发 P1
        assert should_repeat is False

    def test_p1_negation_repair_active(self, base_state, mock_aider_result):
        """测试: 否定条件1——repair 已激活时跳过。"""
        router = Router()
        base_state["progress"]["repair_context"] = {"status": "active"}
        audit_issues = [MagicMock(severity="P1", title="设计问题")]
        should_repeat, reason = router.evaluate_p1_decision_tree(
            base_state, mock_aider_result, audit_issues
        )
        assert should_repeat is False
        assert "repair" in reason.lower()

    def test_p1_negation_repeat_exhausted(self, base_state, mock_aider_result):
        """测试: 否定条件2——重复次数超限时跳过。"""
        router = Router()
        base_state["routing_repeat_tracker"]["part_2_2"] = 5
        audit_issues = [MagicMock(severity="P1", title="设计问题")]
        should_repeat, _ = router.evaluate_p1_decision_tree(
            base_state, mock_aider_result, audit_issues
        )
        assert should_repeat is False

    def test_p1_negation_convergence_achieved(self, base_state, mock_aider_result):
        """测试: 否定条件3——收敛达成时跳过。"""
        router = Router()
        base_state["progress"]["convergence_counter"] = 5
        audit_issues = [MagicMock(severity="P1", title="设计问题")]
        should_repeat, _ = router.evaluate_p1_decision_tree(
            base_state, mock_aider_result, audit_issues
        )
        assert should_repeat is False

    def test_p1_trigger_new_issues(self, base_state, mock_aider_result):
        """测试: P1 正面条件——新问题引入触发。"""
        router = Router()
        base_state["progress"]["new_issues_this_round"] = True
        audit_issues = [MagicMock(severity="P1", title="设计缺陷")]
        should_repeat, reason = router.evaluate_p1_decision_tree(
            base_state, mock_aider_result, audit_issues
        )
        assert should_repeat is True

    def test_p1_large_diff_trigger(self, base_state, mock_aider_result):
        """测试: P1 正面条件——大变更触发。"""
        router = Router()
        mock_aider_result.added_lines = 200
        mock_aider_result.removed_lines = 50
        audit_issues = [MagicMock(severity="P1", title="范围过大")]
        should_repeat, _ = router.evaluate_p1_decision_tree(
            base_state, mock_aider_result, audit_issues
        )
        assert should_repeat is True


# ---------------------------------------------------------------------------
# Test Cases: P2 Detection
# ---------------------------------------------------------------------------

class TestP2Detection:
    """P2 实施问题检测测试。"""

    def test_detect_p2_empty(self):
        """测试: 无 P2 问题时返回空列表。"""
        router = Router()
        issues = [MagicMock(severity="P0", title="致命")]
        result = router.detect_p2(issues)
        assert result == []

    def test_detect_p2_found(self):
        """测试: 检测到 P2 实施问题。"""
        router = Router()
        issues = [
            MagicMock(severity="P2", title="缺少 stop_signal"),
            MagicMock(severity="P2", title="语法错误"),
        ]
        result = router.detect_p2(issues)
        assert len(result) == 2

    def test_detect_p2_with_dict_issues(self):
        """测试: P2 检测兼容 dict 形式的 Issue。"""
        router = Router()
        issues = [
            {"severity": "P2", "title": "文件未关闭"},
            {"severity": "P1", "title": "设计问题"},
        ]
        result = router.detect_p2(issues)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Test Cases: routing_decision() Main Function
# ---------------------------------------------------------------------------

class TestRoutingDecision:
    """routing_decision() 主函数测试。"""

    def test_routing_continue_no_issues(self, base_state, mock_aider_result):
        """测试: 无问题时返回 CONTINUE。"""
        decision = route_fn(base_state, mock_aider_result, [])
        assert decision.action == RoutingAction.CONTINUE
        assert decision.p0_count == 0
        assert decision.p1_count == 0
        assert decision.p2_count == 0

    def test_routing_rollback_on_p0(self, base_state, mock_aider_result):
        """测试: P0 问题时回退到 Part 1。"""
        issues = [MagicMock(severity="P0", title="后门代码")]
        decision = route_fn(base_state, mock_aider_result, issues)
        assert decision.action == RoutingAction.ROLLBACK_TO_PART1
        assert decision.target_phase == "part_1_1"
        assert decision.p0_count == 1

    def test_routing_repeat_on_p2(self, base_state, mock_aider_result):
        """测试: P2 问题时重复当前阶段。"""
        issues = [MagicMock(severity="P2", title="语法错误")]
        decision = route_fn(base_state, mock_aider_result, issues)
        assert decision.action == RoutingAction.REPEAT_PHASE
        assert decision.target_phase == "part_2_2"
        assert decision.metadata.get("repair_needed") is True

    def test_routing_has_fatal_issues_flag(self, base_state, mock_aider_result):
        """测试: has_fatal_issues 标志位正确（P0 触发回退而非 repair）。"""
        issues = [MagicMock(severity="P0", title="致命")]
        decision = route_fn(base_state, mock_aider_result, issues)
        assert decision.has_fatal_issues is True
        # P0 级别问题触发 ROLLBACK_TO_PART1，不触发 repair
        assert decision.action == RoutingAction.ROLLBACK_TO_PART1

    def test_routing_p0_takes_priority_over_p2(self, base_state, mock_aider_result):
        """测试: P0 优先级高于 P2。"""
        issues = [
            MagicMock(severity="P2", title="语法错误"),
            MagicMock(severity="P0", title="后门代码"),
        ]
        decision = route_fn(base_state, mock_aider_result, issues)
        # P0 应优先触发 ROLLBACK_TO_PART1 而非 REPEAT_PHASE
        assert decision.action == RoutingAction.ROLLBACK_TO_PART1


# ---------------------------------------------------------------------------
# Test Cases: Router with Configuration
# ---------------------------------------------------------------------------

class TestRouterConfig:
    """Router 配置相关测试。"""

    def test_router_with_custom_config(self):
        """测试: 自定义配置的 Router。"""
        config = {"route_repeat_max": 5}
        router = Router(config=config)
        assert router.config["route_repeat_max"] == 5

    def test_router_default_config(self):
        """测试: Router 默认配置。"""
        router = Router()
        assert isinstance(router.config, dict)
        assert len(router.config) == 0
