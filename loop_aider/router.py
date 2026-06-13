"""
loop_aider/router.py —— Router 路由系统（Milestone 4）

在 Aider 调用之后（Post-call Audit 完成），根据 diff 解析结果和审计发现
的问题，路由决策接下来应该执行什么操作。

三层决策架构：
    P0 检测 — 致命设计问题（后门、安全漏洞、设计级错误）
              → 触发 Part 1 回退（重新分析需求/方案）
    P1 决策树 — 设计级问题（5 设计条件 + 4 否定条件）
              → 决定是否 repeat Part 2 或进入下一阶段
    P2 检测 — 实施级问题（语法错误、测试失败、缺少 stop_signal）
              → 触发 Part 2 repair（局部修复）

Key Function:
    routing_decision(state, aider_result, audit_result) → RoutingDecision

Usage:
    from loop_aider.router import Router, routing_decision

    router = Router()
    decision = router.routing_decision(state, aider_result, audit_result)
    if decision.action == "rollback_to_part1":
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------

class RoutingAction(Enum):
    """路由决策动作枚举。"""
    CONTINUE = "continue"                 # 正常进入下一阶段
    REPEAT_PHASE = "repeat_phase"         # 重复当前阶段（修复后重试）
    NEXT_CYCLE = "next_cycle"             # 完成当前周期，进入下一周期
    ROLLBACK_TO_PART1 = "rollback_to_part1"  # 回退到 Part 1（重大设计问题）
    TERMINATE = "terminate"               # 终止循环（收敛完成或致命错误）
    PAUSE = "pause"                       # 暂停等待用户输入
    SKIP = "skip"                         # 跳过当前阶段


@dataclass
class RoutingDecision:
    """
    路由决策结果。

    Attributes:
        action:       路由动作（CONTINUE / REPEAT_PHASE / ROLLBACK_TO_PART1 等）。
        reason:       决策原因（人类可读）。
        target_phase: 目标阶段（action 为 REPEAT_PHASE 或 ROLLBACK_TO_PART1 时）。
        issues_found: 发现的问题列表（P0/P1/P2 分类）。
        p0_count:     P0 级问题数量。
        p1_count:     P1 级问题数量。
        p2_count:     P2 级问题数量。
        metadata:     额外元数据（用于路由历史记录）。
    """
    action: RoutingAction = RoutingAction.CONTINUE
    reason: str = ""
    target_phase: str = ""
    issues_found: dict[str, list[str]] = field(default_factory=lambda: {"p0": [], "p1": [], "p2": []})
    p0_count: int = 0
    p1_count: int = 0
    p2_count: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def has_fatal_issues(self) -> bool:
        """是否有 P0 致命问题。"""
        return self.p0_count > 0

    @property
    def needs_repair(self) -> bool:
        """是否需要修复（P1 或 P2 问题）。"""
        return self.p1_count > 0 or self.p2_count > 0


# ---------------------------------------------------------------------------
# P1 决策树：设计级条件与否定条件
# ---------------------------------------------------------------------------

# P1 正面条件（设计级决策）—— 满足任一条件即进入决策分支
P1_DESIGN_CONDITIONS = {
    "unexpected_files_changed": {
        "description": "修改了非预期的文件，可能偏离设计方案",
        "threshold": 0,  # 任何非预期文件即触发
    },
    "design_artifact_missing": {
        "description": "设计方案产物缺失（如缺少 plan.md / review.md）",
        "threshold": 0,
    },
    "large_diff_no_semantic_commit": {
        "description": "大变更 (>200 行) 但缺少语义化提交信息",
        "threshold": 200,
    },
    "new_issues_introduced": {
        "description": "本轮引入了新问题（P1/P2 数量增加）",
        "threshold": 0,
    },
    "phase_contract_not_met": {
        "description": "阶段合约未满足（未产出预期产物）",
        "threshold": 0,
    },
}

# P1 否定条件 —— 满足任一否定条件则跳过 P1 决策分支
P1_NEGATION_CONDITIONS = {
    "repair_already_applied": {
        "description": "本轮已执行过修复操作",
        "check_key": "repair_context",  # state["progress"]["repair_context"] 不为 null 时跳过
    },
    "repeat_count_exhausted": {
        "description": "重复次数已达上限（route_repeat_max）",
        "check_key": "route_repeat_max_vs_count",
    },
    "convergence_achieved": {
        "description": "收敛计数器已达到收敛目标",
        "check_key": "convergence_counter",
    },
    "only_p2_issues": {
        "description": "只有 P2 问题（无 P1 问题），走 P2 repair 路径而非 P1 回退",
        "check_key": "p1_count_zero",
    },
}


# ---------------------------------------------------------------------------
# Router Core
# ---------------------------------------------------------------------------

class Router:
    """
    路由系统核心——在 Aider 调用后决定下一步操作。

    三层检测：
        P0: 致命设计问题 → Part 1 回退
        P1: 设计级问题（决策树：5 正面 + 4 否定条件）
        P2: 实施级问题 → Part 2 repair

    Attributes:
        config: 配置字典。
        logger: 日志记录器。
    """

    def __init__(self, config: Optional[dict] = None):
        """初始化 Router。

        Args:
            config: 配置字典，包含 route_repeat_max 等参数。
        """
        self.config = config or {}
        self.logger = logging.getLogger("loop_aider.router")

    # ========================================================================
    # P0 检测：致命设计问题
    # ========================================================================

    def detect_p0(self, audit_issues: list) -> list[str]:
        """P0 检测：从审计问题中提取 P0 级别的致命设计问题。

        P0 级别问题包括：
            - 后门 / 恶意代码（A4 banned_behaviors 检测）
            - 绕过认证机制
            - 敏感凭证泄露
            - 利用已知漏洞
            - 灾难性命令（rm -rf / 等）

        Args:
            audit_issues: 审计发现的问题列表（Issue 对象）。

        Returns:
            P0 问题的描述字符串列表。
        """
        p0_issues: list[str] = []
        for issue in audit_issues:
            # Issue 对象有 severity 属性；兼容 dict 形式
            severity = getattr(issue, "severity", "") if not isinstance(issue, dict) else issue.get("severity", "")
            title = getattr(issue, "title", "") if not isinstance(issue, dict) else issue.get("title", "")
            if severity == "P0":
                p0_issues.append(str(title) if title else str(issue))
        if p0_issues:
            self.logger.critical("P0 致命问题检测: %d 个问题", len(p0_issues))
        return p0_issues

    # ========================================================================
    # P1 决策树
    # ========================================================================

    def evaluate_p1_decision_tree(
        self,
        state: dict,
        aider_result,
        audit_issues: list,
    ) -> tuple[bool, str]:
        """P1 决策树：基于 5 个设计条件 + 4 个否定条件进行路由决策。

        正面条件（满足任一即可能触发 repeat）：
            1. unexpected_files_changed
            2. design_artifact_missing
            3. large_diff_no_semantic_commit
            4. new_issues_introduced
            5. phase_contract_not_met

        否定条件（满足任一即跳过 P1 决策）：
            1. repair_already_applied
            2. repeat_count_exhausted
            3. convergence_achieved
            4. only_p2_issues

        Args:
            state: 完整的 state.json 字典。
            aider_result: AiderResult 对象。
            audit_issues: 审计问题列表。

        Returns:
            (should_repeat_part2, reason) 元组。
            True 表示应重复 Part 2 阶段。
        """
        progress = state.get("progress", {})
        phase = progress.get("phase", "")
        repair_context = progress.get("repair_context", None)

        # ---- 否定条件检查（优先级高于正面条件） ----

        # 否定条件 1: repair 已被应用
        if repair_context is not None and repair_context != "null":
            self.logger.info("P1 否定：已存在活跃 repair_context，跳过 P1 决策")
            return False, "repair 已应用，跳过 P1 决策"

        # 否定条件 2: 重复次数超限
        route_repeat_max = state.get("config", {}).get("route_repeat_max", 3)
        repeat_tracker = state.get("routing_repeat_tracker", {})
        current_phase_repeats = repeat_tracker.get(phase, 0)
        if current_phase_repeats >= route_repeat_max:
            self.logger.warning(
                "P1 否定：阶段 %s 已重复 %d 次（上限 %d）",
                phase, current_phase_repeats, route_repeat_max
            )
            return False, f"重复次数达到上限（{current_phase_repeats}/{route_repeat_max}）"

        # 否定条件 3: 收敛已达成
        convergence_counter = progress.get("convergence_counter", 0)
        convergence_rounds = state.get("config", {}).get("convergence_rounds", 2)
        if convergence_counter >= convergence_rounds:
            self.logger.info("P1 否定：收敛计数器 %d >= %d，跳过 P1 决策",
                             convergence_counter, convergence_rounds)
            return False, "收敛已达成，跳过 P1 决策"

        # 否定条件 4: 只有 P2 问题
        p1_count = sum(1 for i in audit_issues
                       if getattr(i, "severity", "") == "P1" or
                       (isinstance(i, dict) and i.get("severity") == "P1"))
        if p1_count == 0:
            self.logger.info("P1 否定：仅有 P2 问题，跳过 P1 决策走 P2 repair")
            return False, "仅 P2 问题，走 P2 repair 路径"

        # ---- 正面条件检查 ----

        reasons: list[str] = []

        # 正1: unexpected_files_changed
        affected_files = getattr(aider_result, "affected_files", [])
        task_files_from_state = self._get_task_files(state)
        if task_files_from_state and affected_files:
            unexpected = [f for f in affected_files
                          if not any(f.startswith(tf) or tf.endswith(f.split('/')[-1])
                                     for tf in task_files_from_state)]
            if unexpected:
                reasons.append(f"非预期文件变更: {unexpected}")

        # 正2: design_artifact_missing
        artifacts = state.get("artifacts", {})
        expected_artifacts = self._get_expected_artifacts(phase)
        missing = [a for a in expected_artifacts if a not in artifacts]
        if missing:
            reasons.append(f"设计产物缺失: {missing}")

        # 正3: large_diff_no_semantic_commit
        total_changes = (getattr(aider_result, "added_lines", 0) +
                         getattr(aider_result, "removed_lines", 0))
        if total_changes > 200:
            reasons.append(f"大变更 ({total_changes} 行) 需审查")

        # 正4: new_issues_introduced
        if progress.get("new_issues_this_round", False):
            reasons.append("本轮检测到新引入问题")

        # 正5: phase_contract_not_met
        if not self._check_phase_contract(state, aider_result):
            reasons.append("阶段合约未完全满足")

        if reasons:
            self.logger.info("P1 决策树触发: %s", "; ".join(reasons))
            return True, "P1 决策: " + "; ".join(reasons)

        return False, "P1 决策通过，继续"

    def _get_task_files(self, state: dict) -> list[str]:
        """从 state 中提取任务预期文件列表。"""
        # 从 contracts 中提取 expected_outputs
        contracts = state.get("phase_contracts", {}).get("contracts", {})
        phase = state.get("progress", {}).get("phase", "")
        contract = contracts.get(phase, {})
        return contract.get("expected_outputs", [])

    def _get_expected_artifacts(self, phase: str) -> list[str]:
        """根据阶段返回预期产物名称列表。"""
        PHASE_ARTIFACTS = {
            "part_1_1": ["requirements.md"],
            "part_1_2": ["direction.md"],
            "part_1_3": ["solution.md"],
            "part_2_1": ["plan.md"],
            "part_2_2": [],  # 实施阶段产出的是代码文件，不在此处追踪
            "part_2_3": ["review.md"],
            "part_2_4": ["test_strategy.md"],
            "part_2_5": ["test_plan.md"],
            "part_2_6": ["test_results.md"],
            "part_2_7": ["audit.md"],
            "part_2_8": ["verification.md"],
        }
        return PHASE_ARTIFACTS.get(phase, [])

    def _check_phase_contract(self, state: dict, aider_result) -> bool:
        """检查阶段合约是否满足。
        Returns True if contract is met or no contract defined.
        """
        contracts = state.get("phase_contracts", {}).get("contracts", {})
        phase = state.get("progress", {}).get("phase", "")
        contract = contracts.get(phase, {})
        if not contract:
            return True
        expected = contract.get("expected_outputs", [])
        if not expected:
            return True
        affected = getattr(aider_result, "affected_files", [])
        return len(affected) > 0

    # ========================================================================
    # P2 检测
    # ========================================================================

    def detect_p2(self, audit_issues: list) -> list[str]:
        """P2 检测：从审计问题中提取 P2 级别的实施问题。

        P2 问题包括：
            - 语法错误 / import 错误
            - 测试失败
            - 代码风格问题
            - 警告（非阻塞）
            - 缺少 stop_signal 标记

        Args:
            audit_issues: 审计发现的问题列表（Issue 对象）。

        Returns:
            P2 问题的描述字符串列表。
        """
        p2_issues: list[str] = []
        for issue in audit_issues:
            severity = getattr(issue, "severity", "") if not isinstance(issue, dict) else issue.get("severity", "")
            title = getattr(issue, "title", "") if not isinstance(issue, dict) else issue.get("title", "")
            if severity == "P2":
                p2_issues.append(str(title) if title else str(issue))
        if p2_issues:
            self.logger.info("P2 实施问题检测: %d 个问题", len(p2_issues))
        return p2_issues

    # ========================================================================
    # 主路由函数
    # ========================================================================

    def routing_decision(
        self,
        state: dict,
        aider_result,
        audit_issues: list,
        current_phase: str = "",
    ) -> RoutingDecision:
        """主路由决策函数——串联 P0/P1/P2 三层检测。

        决策优先级:
            1. P0 检测 — 致命设计问题 → ROLLBACK_TO_PART1
            2. P1 检测 — 设计级问题 → REPEAT_PHASE（回到 Part 2.1）
            3. P2 检测 — 实施问题 → REPEAT_PHASE（当前阶段）
            4. 无问题 → CONTINUE

        Args:
            state: 完整的 state.json 字典。
            aider_result: AiderResult 对象（含 affected_files、diffs 等）。
            audit_issues: Post-call Audit 发现的问题列表。
            current_phase: 当前阶段名称（可选，默认从 state 中读取）。

        Returns:
            RoutingDecision 路由决策对象。
        """
        progress = state.get("progress", {})
        phase = current_phase or progress.get("phase", "")

        # 分类问题
        p0_issues = self.detect_p0(audit_issues)
        p2_issues = self.detect_p2(audit_issues)
        p1_issues = [
            getattr(i, "title", str(i)) if not isinstance(i, dict) else i.get("title", str(i))
            for i in audit_issues
            if (getattr(i, "severity", "") if not isinstance(i, dict) else i.get("severity", "")) == "P1"
        ]

        decision = RoutingDecision(
            action=RoutingAction.CONTINUE,
            reason="",
            target_phase="",
            issues_found={"p0": p0_issues, "p1": p1_issues, "p2": p2_issues},
            p0_count=len(p0_issues),
            p1_count=len(p1_issues),
            p2_count=len(p2_issues),
        )

        # ---- 第 1 层: P0 检测（最高优先级） ----
        if p0_issues:
            self.logger.critical(
                "P0 路由决策: 回退到 Part 1 — %s", p0_issues[0]
            )
            decision.action = RoutingAction.ROLLBACK_TO_PART1
            decision.reason = f"P0 致命设计问题: {p0_issues[0]}"
            decision.target_phase = "part_1_1"
            return decision

        # ---- 第 2 层: P1 决策树 ----
        trigger_p1, p1_reason = self.evaluate_p1_decision_tree(
            state, aider_result, audit_issues
        )
        if trigger_p1:
            decision.action = RoutingAction.REPEAT_PHASE
            decision.reason = p1_reason
            # P1 触发 → 回退到 Part 2.1（重新方案）
            decision.target_phase = "part_2_1"
            self.logger.info("P1 路由决策: REPEAT_PHASE → %s", decision.target_phase)
            return decision

        # ---- 第 3 层: P2 检测 ----
        if p2_issues:
            decision.action = RoutingAction.REPEAT_PHASE
            decision.reason = f"P2 实施问题: {p2_issues[0]}"
            decision.target_phase = phase  # 重复当前阶段
            decision.metadata["repair_needed"] = True
            self.logger.info("P2 路由决策: REPEAT_PHASE → %s (repair)", phase)
            return decision

        # ---- 无问题: 继续 ----
        decision.action = RoutingAction.CONTINUE
        decision.reason = "所有检查通过，继续推进"
        self.logger.info("路由决策: CONTINUE (阶段 %s)", phase)
        return decision


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------

def routing_decision(
    state: dict,
    aider_result,
    audit_issues: list,
    current_phase: str = "",
) -> RoutingDecision:
    """便捷函数: 一行调用完成路由决策。

    等价于:
        router = Router()
        decision = router.routing_decision(state, aider_result, audit_issues)

    Args:
        state:         完整的 state.json 字典。
        aider_result:  AiderResult 对象。
        audit_issues:  Post-call Audit 问题列表。
        current_phase: 当前阶段名称（可选）。

    Returns:
        RoutingDecision 路由决策对象。
    """
    router = Router(state.get("config", {}))
    return router.routing_decision(state, aider_result, audit_issues, current_phase)
