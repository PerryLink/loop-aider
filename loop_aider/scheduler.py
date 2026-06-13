"""
loop_aider/scheduler.py —— 完整调度器（Milestone 4）

Scheduler 是 loop-aider 的"大脑"——它将所有模块串联成完整的自主循环。

架构概览:
        ┌─────────────────────────────────────────────────────────┐
        │                    Scheduler (scheduler.py)             │
        │                                                         │
        │  ┌─────────────┐     ┌─────────────┐     ┌───────────┐ │
        │  │ StateMachine │────→│ PhaseGuard  │────→│AiderMgr   │ │
        │  └─────────────┘     │ (guard.py)  │     │(aider_mgr)│ │
        │                      └─────────────┘     └─────┬─────┘ │
        │                                               │        │
        │  ┌─────────────┐     ┌─────────────┐     ┌───┴───────┐ │
        │  │Convergence  │←────│   Router    │←────│DiffParser │ │
        │  │Engine       │     │ (router.py) │     │(diff_prsr)│ │
        │  └─────────────┘     └─────────────┘     └───────────┘ │
        │                                                         │
        │  ┌─────────────┐                                       │
        │  │RepairManager│←── P2 问题时激活                       │
        │  │(repair.py)  │                                       │
        │  └─────────────┘                                       │
        └─────────────────────────────────────────────────────────┘

主循环伪代码:
    scheduler = Scheduler()
    while not scheduler.should_terminate():
        scheduler.run_phase()

Usage:
    from loop_aider.scheduler import Scheduler

    scheduler = Scheduler(run_dir=".", goal="Add unit tests for module X")
    scheduler.run()  # 启动自主循环
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .state_machine import StateMachine, DEFAULT_STATE
from .guard import PhaseGuard
from .aider_manager import AiderManager, AiderResult, HealthStatus
from .diff_parser import DiffParser
from .router import Router, RoutingDecision, RoutingAction
from .convergence import ConvergenceEngine
from .repair import RepairManager, RepairContext
from .phase_guard import TrustLevel, PhaseBlockedError, PhasePausedError


# ---------------------------------------------------------------------------
# Phase Sequence Definition
# ---------------------------------------------------------------------------

# Part 1: 需求分析与方案设计
PART1_PHASES = ["part_1_1", "part_1_2", "part_1_3"]

# Part 2: 实施与验证（8 个子阶段）
PART2_PHASES = [
    "part_2_1",  # 实施计划
    "part_2_2",  # 代码实现
    "part_2_3",  # 代码审查
    "part_2_4",  # 测试策略
    "part_2_5",  # 测试计划
    "part_2_6",  # 测试执行
    "part_2_7",  # 审计
    "part_2_8",  # 验证
]

# 完整 11 阶段序列
ALL_PHASES = PART1_PHASES + PART2_PHASES


# ---------------------------------------------------------------------------
# Scheduler Core
# ---------------------------------------------------------------------------

class Scheduler:
    """
    完整调度器——串联所有模块实现自主闭环。

    核心职责:
        1. 初始化所有子系统（StateMachine, PhaseGuard, AiderManager,
           DiffParser, Router, ConvergenceEngine, RepairManager）
        2. 运行主循环: while not should_terminate(): run_phase()
        3. 管理 phase 推进、cycle 递增、重试和路由
        4. 处理异常: Gate 阻塞、超时、Aider 失败

    Attributes:
        run_dir:          工作目录。
        state_dir:        状态文件目录。
        state_machine:    StateMachine 实例。
        phase_guard:      PhaseGuard 实例。
        aider_manager:    AiderManager 实例。
        diff_parser:      DiffParser 实例。
        router:           Router 实例。
        convergence:      ConvergenceEngine 实例。
        repair_manager:   RepairManager 实例。
        state:            当前 state.json 字典。
        logger:           日志记录器。
        _terminated:      内部终止标志。
    """

    def __init__(
        self,
        run_dir: str = ".",
        state_dir: Optional[str] = None,
        goal: str = "",
        mode: str = "auto",
        max_cycles: int = 5,
        convergence_rounds: int = 2,
        aider_timeout_seconds: int = 600,
        aider_retry_count: int = 2,
        model: Optional[str] = None,
    ):
        """初始化 Scheduler。

        Args:
            run_dir:               工作目录（Git 仓库根目录）。
            state_dir:             状态文件目录（默认 .aider/loop-aider）。
            goal:                  用户设定的目标。
            mode:                  信任模式（safe / auto / unsafe / interactive）。
            max_cycles:            最大周期数。
            convergence_rounds:    收敛所需连续无问题轮次。
            aider_timeout_seconds: Aider 调用超时（秒）。
            aider_retry_count:     Aider 调用失败重试次数。
            model:                 指定的 AI 模型（None 则使用 Aider 默认）。
        """
        self.run_dir = Path(run_dir).resolve()
        self.state_dir = (
            Path(state_dir) if state_dir
            else self.run_dir / ".aider" / "loop-aider"
        )

        # 确保 state_dir 存在
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # 日志 —— 必须在 _restore_from_state 之前初始化
        self.logger = logging.getLogger("loop_aider.scheduler")

        # 初始化核心模块
        self.state_machine = StateMachine(str(self.state_dir))
        self.state = self.state_machine.load_state()

        # 合并配置
        self._merge_config(goal, mode, max_cycles, convergence_rounds,
                           aider_timeout_seconds, aider_retry_count, model)

        # 恢复运行状态
        self._restore_from_state()

        # 初始化子系统
        self.phase_guard = PhaseGuard(
            config=self.state["config"],
            mode=self._trust_level,
        )
        self.aider_manager = AiderManager(
            config=self.state["config"],
        )
        self.diff_parser = DiffParser()
        self.router = Router(config=self.state["config"])
        self.convergence = ConvergenceEngine(config=self.state["config"])
        self.repair_manager = RepairManager(config=self.state["config"])

        # 运行状态
        self._terminated = False
        self._paused = False
        self._pause_reason = ""
        self._last_result: Optional[AiderResult] = None

    # ========================================================================
    # 初始化辅助方法
    # ========================================================================

    def _merge_config(self, goal: str, mode: str, max_cycles: int,
                      convergence_rounds: int, aider_timeout_seconds: int,
                      aider_retry_count: int, model: Optional[str]):
        """合并运行参数到 state.config。

        将 Scheduler 构造参数合并到 state["config"] 中，已有配置项不覆盖。

        Args:
            goal:                   用户设定的目标。
            mode:                   信任模式（safe/auto/unsafe/interactive）。
            max_cycles:             最大周期数。
            convergence_rounds:     收敛所需连续无问题轮次。
            aider_timeout_seconds:  Aider 调用超时（秒）。
            aider_retry_count:      Aider 调用失败重试次数。
            model:                  指定的 AI 模型（None 则使用默认）。
        """
        config = self.state.setdefault("config", {})
        if goal:
            config["user_request"] = goal
        if mode:
            config["mode"] = mode
        config.setdefault("max_cycles", max_cycles)
        config.setdefault("convergence_rounds", convergence_rounds)
        config.setdefault("aider_timeout_seconds", aider_timeout_seconds)
        config.setdefault("aider_retry_count", aider_retry_count)
        if model:
            config["model"] = model

    def _restore_from_state(self):
        """从 state.json 恢复运行上下文。

        恢复 phase（若为 init 则自动推进到 part_1_1）、
        解析 trust_level 映射、加载配置状态。

        Raises:
            KeyError: state.json 结构不完整时可能抛出。
        """
        progress = self.state.get("progress", {})
        phase = progress.get("phase", "init")

        # 如果是从"干净"状态开始（init），自动推进到第一个阶段
        if phase == "init":
            self.state_machine.update_phase(self.state, "part_1_1")
            self.logger.info("自动从 init 推进到 part_1_1")

        # 解析信任级别
        mode = self.state.get("config", {}).get("mode", "auto")
        mode_map = {
            "safe": TrustLevel.SAFE,
            "auto": TrustLevel.AUTO,
            "unsafe": TrustLevel.UNSAFE,
            "interactive": TrustLevel.INTERACTIVE,
        }
        self._trust_level = mode_map.get(mode, TrustLevel.AUTO)

    # ========================================================================
    # 主循环
    # ========================================================================

    def run(self) -> dict:
        """
        启动自主循环——loop-aider 的主入口。

        循环结构:
            while not should_terminate():
                run_phase()

        每个 phase 执行:
            1. 锁定进程（并发保护）
            2. 运行当前 phase（通过 Aider）
            3. 解析输出（DiffParser）
            4. 审计（PhaseGuard Post-call Audits）
            5. 路由决策（Router）
            6. 收敛更新（ConvergenceEngine）
            7. 推进到下一 phase / cycle

        Returns:
            最终的 state 字典。
        """
        self.logger.info(
            "=" * 60 + "\n"
            "Scheduler 启动: goal=%s mode=%s max_cycles=%d\n" +
            "=" * 60,
            self.state["config"].get("user_request", ""),
            self.state["config"].get("mode", "auto"),
            self.state["config"].get("max_cycles", 5),
        )

        # 主循环
        while not self._terminated:
            # Step 0: 终止检查
            if self._should_terminate_check():
                break

            # Step 1: 获取锁
            if not self.state_machine.acquire_lock():
                self.logger.error("无法获取进程锁，退出")
                break

            try:
                # Step 2: 运行当前 phase
                self._run_current_phase()

                # Step 3: 推进状态
                self._advance_state()

                # Step 4: 持久化
                self.state_machine.save_state(self.state)

            except PhaseBlockedError as exc:
                self.logger.error("Phase 被 Gate 阻塞: %s", exc)
                self._handle_blocked(exc)
            except PhasePausedError as exc:
                self.logger.info("Phase 被 Gate 暂停: %s", exc)
                self._handle_paused(exc)
            except Exception as exc:
                self.logger.exception("Scheduler 异常: %s", exc)
                self._handle_error(exc)
            finally:
                self.state_machine.release_lock()

        # 收尾
        self._finalize()
        return self.state

    def _should_terminate_check(self) -> bool:
        """检查是否应该终止。

        调用 ConvergenceEngine.should_terminate() 判定终止条件，
        若收敛达成则更新 state["termination"] 并设置 _terminated 标志。

        Returns:
            True 表示已触发终止，主循环应退出。
        """
        result = self.convergence.should_terminate(self.state)
        if result.converged:
            self.logger.info("终止条件达成: %s", result.reason)
            self.state["termination"]["status"] = "completed"
            self.state["termination"]["exit_reason"] = result.reason
            self._terminated = True
            return True

        if self._paused:
            self.logger.info("Scheduler 暂停中, 原因: %s", self._pause_reason)
            # 在交互模式下等待超时
            return False

        return False

    # ========================================================================
    # Phase 执行
    # ========================================================================

    def _run_current_phase(self):
        """执行当前 phase 的完整流程。

        流程概览:
            1. 检查修复上下文 → 如有则执行修复
            2. 构建模板变量
            3. 获取 phase 关联文件列表
            4. 运行 Aider（经过 PhaseGuard Gate）
            5. 更新 Aider 会话统计
            6. 提取审计问题
            7. 路由决策（P0/P1/P2）
            8. 收敛计数器更新
            9. 执行路由决策
            10. 更新问题跟踪

        Raises:
            PhaseBlockedError: Gate 阻塞时抛出。
            PhasePausedError: Gate 暂停时抛出。
        """
        phase = self.state["progress"]["phase"]
        cycle = self.state["progress"]["cycle"]

        self.logger.info(">>> 执行 Phase: %s (Cycle %d) <<<", phase, cycle)

        # 检查是否需要修复
        repair_ctx = self.repair_manager.get_context(self.state)
        if repair_ctx and repair_ctx.is_active:
            self.logger.info("检测到活跃修复上下文，执行修复")
            self._execute_repair(repair_ctx)
            return

        # 正常 phase 执行
        # 1. 准备模板变量
        template_vars = self._build_template_vars(phase)

        # 2. 准备文件列表
        files = self._get_files_for_phase(phase)

        # 3. 运行 Aider（经过 PhaseGuard）
        result = self.aider_manager.run_phase(
            phase=phase,
            template_vars=template_vars,
            files=files,
            state=self.state,
            user_approved_plan=self._is_plan_approved(),
        )
        self._last_result = result

        # 4. 更新 Aider 会话统计
        self._update_aider_session(result)

        # 5. 提取审计问题
        audit_issues = getattr(result, "audit_issues", [])

        # 6. 路由决策
        decision = self.router.routing_decision(
            self.state, result, audit_issues, phase
        )

        # 7. 收敛更新
        self._update_convergence(result, decision, audit_issues)

        # 8. 根据路由决策执行操作
        self._execute_routing_decision(decision, result)

        # 9. 更新问题跟踪
        self._update_issue_tracking(audit_issues, decision, result)

    # ========================================================================
    # Phase 辅助方法
    # ========================================================================

    def _build_template_vars(self, phase: str) -> dict:
        """构建 Jinja2 模板变量。

        从 state 中提取 phase/cycle/goal/mode/model/convergence_counter
        等字段，构造模板渲染上下文。

        Args:
            phase: 当前阶段名称。

        Returns:
            模板变量字典，包含 goal, cycle, model, existing_code 等键。
        """
        config = self.state.get("config", {})
        progress = self.state.get("progress", {})
        return {
            "phase": phase,
            "cycle": progress.get("cycle", 1),
            "goal": config.get("user_request", ""),
            "mode": config.get("mode", "auto"),
            "model": config.get("model", None),
            "convergence_counter": progress.get("convergence_counter", 0),
            "existing_code": self._read_codebase_summary(),
            "context_summary": self.state.get("context_snapshot", {}).get(
                "narrative_1k", ""
            ),
        }

    def _get_files_for_phase(self, phase: str) -> Optional[list[str]]:
        """获取当前 phase 相关的文件列表。

        Part 1 阶段返回 None（无需指定文件），Part 2 阶段从上轮结果
        的 affected_files 中提取（最多 20 个）。

        Args:
            phase: 当前阶段名称。

        Returns:
            文件路径列表，或 None。
        """
        # Part 1 阶段通常不需要指定文件
        if phase in PART1_PHASES:
            return None
        # Part 2 阶段从 affected_files 中提取（上一轮结果）
        if self._last_result and self._last_result.affected_files:
            return self._last_result.affected_files[:20]  # 限制文件数量
        return None

    def _is_plan_approved(self) -> bool:
        """检查用户是否已批准方案。"""
        pending = self.state.get("pending_confirmation")
        if pending is None:
            return True  # 无待确认项，视为批准
        return pending.get("status") == "approved"

    def _read_codebase_summary(self) -> str:
        """生成代码库摘要信息。

        返回最近变更文件的逗号分隔列表（最多 10 个文件）。

        Returns:
            代码库摘要字符串，无最近变更时返回空字符串。
        """
        # 简单实现：返回最近变更的文件列表
        if self._last_result and self._last_result.affected_files:
            return f"最近变更: {', '.join(self._last_result.affected_files[:10])}"
        return ""

    def _update_aider_session(self, result: AiderResult):
        """更新 Aider 会话统计到 state。

        递增 total_aider_calls、累加 total_aider_duration_ms，
        记录最后使用的模型和退出码。

        Args:
            result: Aider 调用结果对象，含 exit_code/duration_ms/models_used。
        """
        session = self.state.setdefault("aider_session", {})
        session["last_exit_code"] = result.exit_code
        session["last_duration_ms"] = result.duration_ms
        session["total_aider_calls"] = session.get("total_aider_calls", 0) + 1
        session["total_aider_duration_ms"] = (
            session.get("total_aider_duration_ms", 0) + result.duration_ms
        )
        if result.models_used:
            session["last_model_used"] = result.models_used[0]

    # ========================================================================
    # 收敛与路由操作
    # ========================================================================

    def _update_convergence(
        self, result: AiderResult, decision: RoutingDecision,
        audit_issues: list
    ):
        """更新收敛计数器。

        从 state["progress"] 读取 new_issues_this_round 标志，
        调用 ConvergenceEngine.update_counter() 更新计数器。

        Args:
            result:       Aider 调用结果。
            decision:     路由决策结果。
            audit_issues: 审计发现的问题列表。
        """
        progress = self.state["progress"]
        new_issues_this_round = progress.get("new_issues_this_round", False)
        issues_resolved = len(decision.issues_found.get("p0", [])) == 0

        self.convergence.update_counter(
            self.state,
            new_issues=new_issues_this_round,
            issues_resolved=issues_resolved,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
        )

    def _execute_routing_decision(
        self, decision: RoutingDecision, result: AiderResult
    ):
        """执行路由决策。

        根据 decision.action 执行对应操作:
            ROLLBACK_TO_PART1 → 回退到 Part 1
            REPEAT_PHASE → 重复当前阶段
            TERMINATE → 设置终止标志
            PAUSE → 设置暂停标志

        Args:
            decision: 路由决策对象（含 action/reason/target_phase）。
            result:   Aider 调用结果。
        """
        self.state.setdefault("routing_history", []).append({
            "action": decision.action.value,
            "reason": decision.reason,
            "phase": self.state["progress"]["phase"],
            "cycle": self.state["progress"]["cycle"],
            "at": datetime.now(timezone.utc).isoformat(),
        })

        if decision.action == RoutingAction.ROLLBACK_TO_PART1:
            self._rollback_to_part1(decision)
        elif decision.action == RoutingAction.REPEAT_PHASE:
            self._schedule_repeat(decision)
        elif decision.action == RoutingAction.TERMINATE:
            self._terminated = True
        elif decision.action == RoutingAction.PAUSE:
            self._paused = True
            self._pause_reason = decision.reason
        # CONTINUE / NEXT_CYCLE / SKIP → 后续 _advance_state() 处理

    def _update_issue_tracking(
        self, audit_issues: list, decision: RoutingDecision,
        result: AiderResult
    ):
        """更新 issues 跟踪信息。

        将审计发现的新问题去重后加入 state["issues"]["active"]，
        同步更新 all_time 总计数和 new_issues_this_round 标志。

        Args:
            audit_issues: 审计问题列表。
            decision:     路由决策对象（用于统计 P0/P1/P2 数量）。
            result:       Aider 调用结果。
        """
        issues = self.state.setdefault("issues", {})
        active = issues.setdefault("active", {"p0": [], "p1": [], "p2": []})
        all_time = issues.setdefault("all_time", {"p0_total": 0, "p1_total": 0, "p2_total": 0})

        # 更新活跃问题
        for issue in audit_issues:
            severity = getattr(issue, "severity", "") if not isinstance(issue, dict) else issue.get("severity", "")
            title = getattr(issue, "title", "") if not isinstance(issue, dict) else issue.get("title", "")
            if severity and title:
                # 避免重复
                existing_titles = [i.get("title", "") if isinstance(i, dict) else getattr(i, "title", "") for i in active.get(severity.lower(), [])]
                if title not in existing_titles:
                    active.setdefault(severity.lower(), []).append({
                        "title": title,
                        "source": getattr(issue, "source", "") if not isinstance(issue, dict) else issue.get("source", ""),
                        "added_at": datetime.now(timezone.utc).isoformat(),
                    })

        # 更新全时统计
        all_time["p0_total"] = (all_time.get("p0_total", 0) + decision.p0_count)
        all_time["p1_total"] = (all_time.get("p1_total", 0) + decision.p1_count)
        all_time["p2_total"] = (all_time.get("p2_total", 0) + decision.p2_count)

        # 标记是否有新问题
        progress = self.state["progress"]
        progress["new_issues_last_round"] = progress.get("new_issues_this_round", False)
        total_new = decision.p0_count + decision.p1_count + decision.p2_count
        progress["new_issues_this_round"] = total_new > 0

    # ========================================================================
    # 修复执行
    # ========================================================================

    def _execute_repair(self, ctx: RepairContext):
        """执行修复流程。

        调用 RepairManager.run_repair() 修复问题列表。
        成功后消费修复上下文；失败后检查重试次数，超限则终止。

        Args:
            ctx: 修复上下文对象（含 issues/attempt_count/max_attempts）。
        """
        self.logger.info(
            "执行修复: %d 个问题 (attempt %d/%d)",
            len(ctx.issues), ctx.attempt_count + 1, ctx.max_attempts
        )

        success = self.repair_manager.run_repair(
            self.state, ctx, self.aider_manager
        )

        if success:
            self.repair_manager.consume_context(self.state, ctx, success=True)
        else:
            # 检查是否能重试
            if ctx.attempt_count < ctx.max_attempts:
                self.logger.warning(
                    "修复失败, 将在下一轮重试 (%d/%d)",
                    ctx.attempt_count, ctx.max_attempts
                )
            else:
                self.logger.error("修复尝试次数耗尽")
                self.repair_manager.consume_context(self.state, ctx, success=False)
                self._terminated = True

    # ========================================================================
    # 状态推进
    # ========================================================================

    def _advance_state(self):
        """推进 phase/cycle 状态。

        流程:
            1. 检查是否需要创建 P2 修复上下文
            2. 获取下一 phase（可能是 cycle_complete）
            3. 若 cycle 完成则递增 cycle、检查 Part 1 收敛
            4. 更新 phase 并记录转换历史
        """
        progress = self.state["progress"]
        phase = progress["phase"]
        cycle = progress["cycle"]

        # 检查是否需要创建 P2 修复上下文
        if self._should_create_repair_context():
            return  # 下一轮执行修复

        # 正常推进
        next_phase = self._get_next_phase(phase)
        if next_phase == "cycle_complete":
            # 完成一个完整周期
            progress["cycle"] = cycle + 1
            progress["part1_round"] = 0
            # 检查 Part 1 是否收敛
            part1_result = self.convergence.check_part1_semantic_convergence(self.state)
            if part1_result.converged:
                next_phase = "part_2_1"
            else:
                next_phase = "part_1_1"
            self.logger.info(
                "Cycle %d 完成, 进入 Cycle %d", cycle, progress["cycle"]
            )

        if next_phase:
            self.state_machine.update_phase(self.state, next_phase)

    def _get_next_phase(self, current_phase: str) -> str:
        """获取下一个阶段。"""
        try:
            idx = ALL_PHASES.index(current_phase)
            if idx + 1 < len(ALL_PHASES):
                return ALL_PHASES[idx + 1]
        except ValueError:
            pass
        # 到达最后一个阶段 → 完成周期
        return "cycle_complete"

    def _should_create_repair_context(self) -> bool:
        """判断是否需要创建修复上下文（P2 问题自动修复）。

        检查上一轮 Aider 结果中是否存在 P2 问题，存在则
        自动调用 RepairManager.create_context() 创建修复上下文。

        Returns:
            True 表示已创建修复上下文，下一轮将执行修复。
        """
        if self._last_result is None:
            return False

        audit_issues = getattr(self._last_result, "audit_issues", [])
        p2_issues_raw = [
            i for i in audit_issues
            if (getattr(i, "severity", "") if not isinstance(i, dict) else i.get("severity", "")) == "P2"
        ]

        if not p2_issues_raw:
            return False

        # 创建修复上下文
        p2_titles = [
            getattr(i, "title", str(i)) if not isinstance(i, dict) else i.get("title", str(i))
            for i in p2_issues_raw
        ]
        affected = getattr(self._last_result, "affected_files", [])

        self.repair_manager.create_context(
            self.state, p2_titles, files_affected=affected
        )
        self.logger.info("自动创建 P2 修复上下文: %d 个问题", len(p2_titles))
        return True

    # ========================================================================
    # 回退与重试
    # ========================================================================

    def _rollback_to_part1(self, decision: RoutingDecision):
        """回退到 Part 1。

        递增 part1_round 计数器，检查是否超限（max_part1_rounds）。
        超限则终止，否则将 phase 切换回 part_1_1。

        Args:
            decision: 路由决策对象（含回退原因）。
        """
        self.state["progress"]["part1_round"] += 1
        max_part1_rounds = self.state["config"].get("max_part1_rounds", 5)

        if self.state["progress"]["part1_round"] > max_part1_rounds:
            self.logger.error(
                "Part 1 回退次数超限 (%d/%d), 终止",
                self.state["progress"]["part1_round"], max_part1_rounds
            )
            self._terminated = True
            return

        self.state_machine.update_phase(self.state, "part_1_1")
        self.logger.warning("回退到 Part 1 (round %d): %s",
                            self.state["progress"]["part1_round"], decision.reason)

    def _schedule_repeat(self, decision: RoutingDecision):
        """安排重复当前阶段。

        更新 routing_repeat_tracker 计数，若 decision 指定了
        目标阶段则切换过去，否则保持当前阶段不变。

        Args:
            decision: 路由决策对象（含 target_phase 和 reason）。
        """
        # 更新重复追踪
        phase = self.state["progress"]["phase"]
        tracker = self.state.setdefault("routing_repeat_tracker", {})
        tracker[phase] = tracker.get(phase, 0) + 1

        # 如果 router 指定了目标阶段，切换过去
        if decision.target_phase and decision.target_phase != phase:
            self.state_machine.update_phase(self.state, decision.target_phase)
            self.logger.info("重复: 切换到 %s", decision.target_phase)
        else:
            self.logger.info("重复当前阶段 %s (count=%d)", phase, tracker[phase])

    # ========================================================================
    # 异常处理
    # ========================================================================

    def _handle_blocked(self, exc: PhaseBlockedError):
        """处理 Gate 阻塞异常。

        设置 termination.status = "blocked"，记录退出原因并终止循环。

        Args:
            exc: PhaseBlockedError 异常对象（含阻塞原因）。
        """
        self.logger.error("Phase 被阻塞: %s", exc)
        self.state["termination"]["status"] = "blocked"
        self.state["termination"]["exit_reason"] = str(exc)
        self._terminated = True

    def _handle_paused(self, exc: PhasePausedError):
        """处理 Gate 暂停异常。

        设置暂停状态和暂停原因。在 AUTO/INTERACTIVE 模式下，
        超时后自动降级恢复。

        Args:
            exc: PhasePausedError 异常对象（含暂停原因）。
        """
        self._paused = True
        self._pause_reason = str(exc)
        # 在 auto 模式下，超时后自动降级
        if self._trust_level in (TrustLevel.AUTO, TrustLevel.INTERACTIVE):
            self.logger.info(
                "自动模式: 暂停 %d 分钟后自动恢复",
                self.phase_guard.interactive_timeout_minutes
            )

    def _handle_error(self, exc: Exception):
        """处理通用异常。

        递增 retry_count_this_phase，超限（>3 次）则终止循环。

        Args:
            exc: 捕获的异常对象。
        """
        retry_count = self.state["progress"].get("retry_count_this_phase", 0)
        retry_count += 1
        self.state["progress"]["retry_count_this_phase"] = retry_count

        max_retries = 3
        if retry_count > max_retries:
            self.logger.error(
                "重试次数超限 (%d/%d), 终止", retry_count, max_retries
            )
            self._terminated = True
        else:
            self.logger.warning(
                "异常, 重试 (%d/%d): %s", retry_count, max_retries, exc
            )

    # ========================================================================
    # 收尾
    # ========================================================================

    def _finalize(self):
        """收尾处理。

        记录完成时间、递增 invocation_count、持久化 state。
        输出最终统计: 总周期数、最终 phase、convergence_counter、Aider 调用次数。
        """
        now = datetime.now(timezone.utc).isoformat()
        self.state["termination"]["completed_at"] = now
        self.state["housekeeping"]["invocation_count"] = (
            self.state["housekeeping"].get("invocation_count", 0) + 1
        )

        self.state_machine.save_state(self.state)

        self.logger.info(
            "Scheduler 结束: 总周期=%d, 最终 phase=%s, "
            "convergence_counter=%d, Aider 调用=%d",
            self.state["progress"]["cycle"],
            self.state["progress"]["phase"],
            self.state["progress"]["convergence_counter"],
            self.state["aider_session"].get("total_aider_calls", 0),
        )

    # ========================================================================
    # 公共 API
    # ========================================================================

    def should_terminate(self) -> bool:
        """检查是否应该终止循环。

        Returns:
            True 表示应该终止。
        """
        return self._terminated or self.convergence.should_terminate(self.state).converged

    def resume(self) -> bool:
        """恢复暂停的 Scheduler。

        Returns:
            True 表示成功恢复。
        """
        if self._paused:
            self._paused = False
            self._pause_reason = ""
            self.logger.info("Scheduler 已恢复")
            return True
        return False

    def get_status(self) -> dict:
        """获取当前 Scheduler 状态摘要。

        Returns:
            包含 phase, cycle, convergence 等信息的字典。
        """
        progress = self.state.get("progress", {})
        termination = self.state.get("termination", {})
        session = self.state.get("aider_session", {})

        return {
            "phase": progress.get("phase", "unknown"),
            "cycle": progress.get("cycle", 0),
            "convergence_counter": progress.get("convergence_counter", 0),
            "termination_status": termination.get("status", "running"),
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "total_aider_calls": session.get("total_aider_calls", 0),
            "last_exit_code": session.get("last_exit_code"),
            "last_duration_ms": session.get("last_duration_ms", 0),
        }

    def get_state(self) -> dict:
        """获取完整的 state 字典。

        Returns:
            当前 state.json 的完整内容。
        """
        return dict(self.state)
