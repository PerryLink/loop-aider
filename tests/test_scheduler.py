"""
tests/test_scheduler.py —— Scheduler 调度器单元测试。

测试调度器初始化、phase 推进、cycle 管理、终止条件、
路由执行、修复流程等核心功能。
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from loop_aider.scheduler import (
    Scheduler, ALL_PHASES, PART1_PHASES, PART2_PHASES,
)
from loop_aider.state_machine import StateMachine
from loop_aider.router import RoutingAction


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_run_dir():
    """创建临时工作目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def scheduler(temp_run_dir):
    """创建基础 Scheduler 实例（不启动完整循环）。"""
    return Scheduler(
        run_dir=temp_run_dir,
        goal="Test goal for scheduler",
        mode="auto",
        max_cycles=3,
        convergence_rounds=2,
        aider_timeout_seconds=30,
        aider_retry_count=0,
    )


@pytest.fixture
def state_machine(temp_run_dir):
    """创建独立的 StateMachine 用于辅助测试。"""
    state_dir = Path(temp_run_dir) / ".aider" / "loop-aider"
    return StateMachine(str(state_dir))


# ---------------------------------------------------------------------------
# Test Cases: Scheduler Initialization
# ---------------------------------------------------------------------------

class TestSchedulerInit:
    """Scheduler 初始化测试。"""

    def test_scheduler_creates_state_dir(self, temp_run_dir):
        """测试: 调度器自动创建 state_dir。"""
        state_dir = Path(temp_run_dir) / ".aider" / "loop-aider"
        assert not state_dir.exists()

        Scheduler(run_dir=temp_run_dir, goal="test")

        assert state_dir.exists()

    def test_scheduler_sets_goal_in_config(self, scheduler):
        """测试: goal 写入 state.config。"""
        assert scheduler.state["config"]["user_request"] == "Test goal for scheduler"

    def test_scheduler_auto_advances_from_init(self, scheduler):
        """测试: 从 init 自动推进到 part_1_1。"""
        assert scheduler.state["progress"]["phase"] == "part_1_1"

    def test_scheduler_loads_existing_state(self, temp_run_dir):
        """测试: 从已有 state.json 恢复。"""
        state_dir = Path(temp_run_dir) / ".aider" / "loop-aider"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "state.json"
        pre_state = {
            "schema_version": 1,
            "progress": {"phase": "part_2_1", "cycle": 2, "convergence_counter": 1,
                         "part1_round": 0, "new_issues_this_round": False,
                         "phase_transitions": []},
            "config": {"user_request": "pre-existing", "mode": "auto",
                       "max_cycles": 5, "convergence_rounds": 2},
            "termination": {"status": "running"},
        }
        with open(state_path, "w") as f:
            json.dump(pre_state, f)

        sched = Scheduler(run_dir=temp_run_dir, goal="new goal")
        assert sched.state["progress"]["phase"] == "part_2_1"
        assert sched.state["progress"]["cycle"] == 2

    def test_scheduler_initializes_subsystems(self, scheduler):
        """测试: 所有子系统均正确初始化。"""
        assert scheduler.phase_guard is not None
        assert scheduler.aider_manager is not None
        assert scheduler.diff_parser is not None
        assert scheduler.router is not None
        assert scheduler.convergence is not None
        assert scheduler.repair_manager is not None


# ---------------------------------------------------------------------------
# Test Cases: Phase Management
# ---------------------------------------------------------------------------

class TestPhaseManagement:
    """Phase 推进管理测试。"""

    def test_get_next_phase_first_to_second(self, scheduler):
        """测试: 第一个阶段到第二个阶段的推进。"""
        next_p = scheduler._get_next_phase("part_1_1")
        assert next_p == "part_1_2"

    def test_get_next_phase_last_to_cycle_complete(self, scheduler):
        """测试: 最后一个阶段到达周期完成。"""
        next_p = scheduler._get_next_phase("part_2_8")
        assert next_p == "cycle_complete"

    def test_get_next_phase_unknown(self, scheduler):
        """测试: 未知阶段返回 cycle_complete。"""
        next_p = scheduler._get_next_phase("unknown_phase")
        assert next_p == "cycle_complete"

    def test_all_phases_chain(self, scheduler):
        """测试: 所有 11 个阶段的推进链完整。"""
        phases = list(ALL_PHASES)
        assert len(phases) == 11
        assert phases[0] == "part_1_1"
        assert phases[-1] == "part_2_8"

    def test_advance_state_single_step(self, scheduler):
        """测试: 推进一个阶段。"""
        scheduler.state["progress"]["phase"] = "part_1_1"
        scheduler._advance_state()
        assert scheduler.state["progress"]["phase"] == "part_1_2"

    def test_advance_state_cycle_complete(self, scheduler):
        """测试: 周期完成时 cycle 递增。"""
        scheduler.state["progress"]["phase"] = "part_2_8"
        scheduler.state["progress"]["cycle"] = 1
        scheduler._advance_state()
        assert scheduler.state["progress"]["cycle"] == 2


# ---------------------------------------------------------------------------
# Test Cases: Termination
# ---------------------------------------------------------------------------

class TestTermination:
    """终止条件测试。"""

    def test_should_terminate_false_initially(self, scheduler):
        """测试: 初始状态下不应终止。"""
        result = scheduler.should_terminate()
        assert result is False

    def test_should_terminate_p0_active(self, scheduler):
        """测试: 存在 P0 问题时终止。"""
        scheduler.state["issues"]["active"]["p0"] = [{"title": "致命"}]
        result = scheduler.should_terminate()
        assert result is True

    def test_should_terminate_max_cycles(self, scheduler):
        """测试: 周期超限时终止。"""
        scheduler.state["progress"]["cycle"] = 10
        result = scheduler.should_terminate()
        assert result is True

    def test_terminate_with_reason(self, scheduler):
        """测试: 终止时设置 exit_reason。"""
        scheduler.state["progress"]["cycle"] = 100
        scheduler._should_terminate_check()
        assert scheduler.state["termination"]["status"] == "completed"
        assert scheduler.state["termination"]["exit_reason"] is not None

    def test_rollback_part1_increments_round(self, scheduler):
        """测试: Part 1 回退时 round 递增。"""
        from loop_aider.router import RoutingDecision, RoutingAction
        decision = RoutingDecision(
            action=RoutingAction.ROLLBACK_TO_PART1,
            reason="test rollback",
            target_phase="part_1_1",
        )
        scheduler._rollback_to_part1(decision)
        assert scheduler.state["progress"]["part1_round"] == 1

    def test_rollback_part1_exceeds_max(self, scheduler):
        """测试: Part 1 回退超限时终止。"""
        from loop_aider.router import RoutingDecision, RoutingAction
        scheduler.state["progress"]["part1_round"] = 100
        decision = RoutingDecision(
            action=RoutingAction.ROLLBACK_TO_PART1,
            reason="test",
        )
        scheduler._rollback_to_part1(decision)
        assert scheduler._terminated is True


# ---------------------------------------------------------------------------
# Test Cases: Routing Execution
# ---------------------------------------------------------------------------

class TestRoutingExecution:
    """路由决策执行测试。"""

    def test_routing_repeat_updates_tracker(self, scheduler):
        """测试: 重复阶段时更新追踪器。"""
        from loop_aider.router import RoutingDecision, RoutingAction
        scheduler.state["progress"]["phase"] = "part_2_2"
        decision = RoutingDecision(
            action=RoutingAction.REPEAT_PHASE,
            reason="test repeat",
            target_phase="part_2_2",
        )
        scheduler._schedule_repeat(decision)
        tracker = scheduler.state.get("routing_repeat_tracker", {})
        assert tracker.get("part_2_2", 0) == 1

    def test_routing_terminate_sets_flag(self, scheduler):
        """测试: TERMINATE 动作设置终止标志。"""
        from loop_aider.router import RoutingDecision, RoutingAction
        decision = RoutingDecision(action=RoutingAction.TERMINATE)
        scheduler._execute_routing_decision(decision, MagicMock())
        assert scheduler._terminated is True

    def test_routing_pause_sets_flag(self, scheduler):
        """测试: PAUSE 动作设置暂停标志。"""
        from loop_aider.router import RoutingDecision, RoutingAction
        decision = RoutingDecision(
            action=RoutingAction.PAUSE, reason="user input needed"
        )
        scheduler._execute_routing_decision(decision, MagicMock())
        assert scheduler._paused is True
        assert "user input" in scheduler._pause_reason

    def test_routing_history_recorded(self, scheduler):
        """测试: 路由决策被记录到 routing_history。"""
        from loop_aider.router import RoutingDecision, RoutingAction
        decision = RoutingDecision(action=RoutingAction.CONTINUE,
                                   reason="all good")
        scheduler._execute_routing_decision(decision, MagicMock())
        history = scheduler.state.get("routing_history", [])
        assert len(history) == 1
        assert history[0]["action"] == "continue"


# ---------------------------------------------------------------------------
# Test Cases: Repair Flow
# ---------------------------------------------------------------------------

class TestRepairFlow:
    """修复流程测试。"""

    def test_create_repair_context_on_p2(self, scheduler):
        """测试: 检测到 P2 问题时创建修复上下文。"""
        mock_result = MagicMock()
        mock_p2 = MagicMock()
        mock_p2.severity = "P2"
        mock_p2.title = "test issue"
        mock_result.audit_issues = [mock_p2]

        scheduler._last_result = mock_result
        result = scheduler._should_create_repair_context()
        assert result is True
        assert scheduler.repair_manager.is_repair_active(scheduler.state)

    def test_no_repair_without_p2(self, scheduler):
        """测试: 无 P2 问题时不创建修复上下文。"""
        scheduler._last_result = None
        result = scheduler._should_create_repair_context()
        assert result is False

    def test_execute_repair_flow(self, scheduler):
        """测试: 完整修复执行流程。"""
        from loop_aider.repair import RepairContext
        ctx = RepairContext(
            id="test-repair-001", status="active",
            issues=["test issue"], fix_strategies=["fix it"],
            max_attempts=3,
        )
        scheduler.state["progress"]["repair_context"] = ctx.__dict__

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.exit_code = 0
        mock_result.audit_issues = []
        scheduler.aider_manager.run_phase = MagicMock(return_value=mock_result)

        scheduler._execute_repair(ctx)
        assert scheduler.repair_manager.is_repair_active(scheduler.state) is False


# ---------------------------------------------------------------------------
# Test Cases: Resume and Status
# ---------------------------------------------------------------------------

class TestResumeAndStatus:
    """暂停/恢复与状态查询测试。"""

    def test_resume_clears_paused(self, scheduler):
        """测试: resume 清除暂停状态。"""
        scheduler._paused = True
        scheduler._pause_reason = "test"
        result = scheduler.resume()
        assert result is True
        assert scheduler._paused is False
        assert scheduler._pause_reason == ""

    def test_resume_when_not_paused(self, scheduler):
        """测试: 未暂停时 resume 返回 False。"""
        result = scheduler.resume()
        assert result is False

    def test_get_status_returns_dict(self, scheduler):
        """测试: get_status 返回完整状态信息。"""
        status = scheduler.get_status()
        assert "phase" in status
        assert "cycle" in status
        assert "convergence_counter" in status
        assert "termination_status" in status
        assert "paused" in status

    def test_get_state_returns_full_state(self, scheduler):
        """测试: get_state 返回完整 state 字典。"""
        state_copy = scheduler.get_state()
        assert "schema_version" in state_copy
        assert "progress" in state_copy
        assert "config" in state_copy
        assert state_copy["config"]["user_request"] == "Test goal for scheduler"


# ---------------------------------------------------------------------------
# Test Cases: Error Handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """异常处理测试。"""

    def test_handle_error_increments_retry(self, scheduler):
        """测试: 异常处理时增加重试计数。"""
        scheduler._handle_error(Exception("test error"))
        assert scheduler.state["progress"]["retry_count_this_phase"] == 1

    def test_handle_error_exceeds_max_retries(self, scheduler):
        """测试: 重试超限时终止。"""
        scheduler.state["progress"]["retry_count_this_phase"] = 5
        scheduler._handle_error(Exception("test error"))
        assert scheduler._terminated is True

    def test_handle_blocked_sets_termination(self, scheduler):
        """测试: Gate 阻塞时设置终止状态。"""
        from loop_aider.phase_guard import PhaseBlockedError
        exc = PhaseBlockedError({"G1": MagicMock()})
        scheduler._handle_blocked(exc)
        assert scheduler.state["termination"]["status"] == "blocked"
        assert scheduler._terminated is True


# ---------------------------------------------------------------------------
# Test Cases: Aider Session Tracking
# ---------------------------------------------------------------------------

class TestAiderSessionTracking:
    """Aider 会话统计测试。"""

    def test_update_aider_session(self, scheduler):
        """测试: Aider 会话统计更新。"""
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.duration_ms = 5000
        mock_result.models_used = ["claude-sonnet-4-20250514"]

        scheduler._update_aider_session(mock_result)

        session = scheduler.state["aider_session"]
        assert session["last_exit_code"] == 0
        assert session["last_duration_ms"] == 5000
        assert session["total_aider_calls"] == 1
        assert session["last_model_used"] == "claude-sonnet-4-20250514"

    def test_update_aider_session_accumulates(self, scheduler):
        """测试: Aider 会话统计累加。"""
        mock_r1 = MagicMock()
        mock_r1.exit_code = 0
        mock_r1.duration_ms = 1000
        mock_r1.models_used = []

        mock_r2 = MagicMock()
        mock_r2.exit_code = 0
        mock_r2.duration_ms = 2000
        mock_r2.models_used = []

        scheduler._update_aider_session(mock_r1)
        scheduler._update_aider_session(mock_r2)

        session = scheduler.state["aider_session"]
        assert session["total_aider_calls"] == 2
        assert session["total_aider_duration_ms"] == 3000
