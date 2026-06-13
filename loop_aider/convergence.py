"""
loop_aider/convergence.py —— ConvergenceEngine 收敛引擎（Milestone 4）

核心职责：
    1. convergence_counter 操作表（P5/P6 合并语义）
    2. 优先级操作表（priority-based operation table）
    3. should_terminate() 终止判定
    4. Part 1 语义收敛检测

收敛概念：
    loop-aider 通过 convergence_counter 追踪连续"无新问题"的轮次。
    当 convergence_counter 达到配置的 convergence_rounds 阈值时，
    表示设计已稳定，可以终止。

convergence_counter 操作表（P5⊕P6 合并）:
    P5 (new_issues = TRUE,  any_issues_resolved) → counter = 0  （有进展但新问题 → 重置）
    P5 (new_issues = TRUE,  no_issues_resolved)  → counter = 0  （回退 → 重置）
    P5 (new_issues = FALSE, any_issues_resolved) → counter++     （收敛中 → 递增）
    P6 (new_issues = FALSE, no_issues_resolved)  → counter += 1  （停滞 → 缓慢递增）
    P6 (exit_code != 0,    timeout)              → counter 不减  （外部错误不惩罚）

Usage:
    from loop_aider.convergence import ConvergenceEngine

    engine = ConvergenceEngine()
    engine.update_counter(state, new_issues=False, issues_resolved=True)
    if engine.should_terminate(state):
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------

@dataclass
class ConvergenceResult:
    """
    收敛检测结果。

    Attributes:
        converged:         是否已收敛。
        counter:           当前收敛计数器值。
        reason:            判定理由。
        semver_converged:  Part 1 语义收敛是否达成。
        metadata:          额外元数据。
    """
    converged: bool = False
    counter: int = 0
    reason: str = ""
    semver_converged: bool = False
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ConvergenceEngine Core
# ---------------------------------------------------------------------------

class ConvergenceEngine:
    """
    收敛引擎——追踪并判定 loop-aider 是否已达到收敛状态。

    两层收敛判定：
        Part 1 语义收敛: 需求/方案/方向三轮已完成且无新问题。
        全局收敛:       convergence_counter >= convergence_rounds。

    Attributes:
        config: 配置字典。
        logger: 日志记录器。
    """

    def __init__(self, config: Optional[dict] = None):
        """初始化收敛引擎。

        Args:
            config: 配置字典，含 convergence_rounds 等参数。
        """
        self.config = config or {}
        self.logger = logging.getLogger("loop_aider.convergence")

    # ========================================================================
    # convergence_counter 操作表（P5/P6 合并）
    # ========================================================================

    def update_counter(
        self,
        state: dict,
        new_issues: bool = False,
        issues_resolved: bool = False,
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> int:
        """
        根据 P5/P6 合并规则更新 convergence_counter。

        操作表:
            P5 新问题+有解决 → counter = 0
            P5 新问题+无解决 → counter = 0
            P5 无新问题+有解决 → counter += 1（收敛中）
            P6 无新问题+无解决 → counter += 1（停滞缓慢递增）
            P6 非零退出码/超时 → counter 不变（外部错误不惩罚）

        Args:
            state:           完整的 state.json 字典（原地修改）。
            new_issues:      本轮是否引入新问题。
            issues_resolved: 本轮是否有问题被解决。
            exit_code:       Aider 退出码。
            timed_out:       Aider 是否超时。

        Returns:
            更新后的 convergence_counter 值。
        """
        progress = state.setdefault("progress", {})
        counter = progress.get("convergence_counter", 0)

        # P6: 外部错误不惩罚（退出码非零或超时）
        if exit_code != 0 or timed_out:
            self.logger.info(
                "convergence_counter 不变 (%d): 退出码=%d 超时=%s",
                counter, exit_code, timed_out
            )
            return counter

        # P5: 有新问题 → counter 重置为 0
        if new_issues:
            self.logger.info(
                "convergence_counter 重置: 0 (本轮引入新问题, resolved=%s)",
                issues_resolved
            )
            progress["convergence_counter"] = 0
            return 0

        # P5: 无新问题 + 有问题解决 → counter += 1（收敛中）
        # P6: 无新问题 + 无问题解决 → counter += 1（停滞缓慢递增）
        new_counter = counter + 1
        progress["convergence_counter"] = new_counter

        if issues_resolved:
            self.logger.info(
                "convergence_counter 递增: %d (收敛中, 问题已解决)", new_counter
            )
        else:
            self.logger.info(
                "convergence_counter 递增: %d (停滞, 无新问题且无解决)", new_counter
            )

        return new_counter

    # ========================================================================
    # Part 1 语义收敛检测
    # ========================================================================

    def check_part1_semantic_convergence(self, state: dict) -> ConvergenceResult:
        """
        检查 Part 1 是否达成语义收敛。

        Part 1 包含三个阶段: part_1_1 (需求), part_1_2 (方向), part_1_3 (方案)。
        语义收敛条件:
            1. 三阶段均已完成（phase 已推进到 part_2_x）
            2. 三阶段产物均存在
            3. 无 P0/P1 级别的设计问题挂起

        Args:
            state: 完整的 state.json 字典。

        Returns:
            ConvergenceResult 收敛结果。
        """
        progress = state.get("progress", {})
        phase = progress.get("phase", "")
        transitions = progress.get("phase_transitions", [])

        # 检查 Part 1 三阶段是否均已完成
        part1_phases = {"part_1_1", "part_1_2", "part_1_3"}
        completed_phases: set[str] = set()
        for t in transitions:
            completed_phases.add(t.get("from", ""))
            completed_phases.add(t.get("to", ""))

        # 也检查当前阶段
        completed_phases.add(phase)

        part1_done = part1_phases.issubset(completed_phases) or (
            phase.startswith("part_2_") and "part_1_3" in completed_phases
        )

        # 检查产物存在
        artifacts = state.get("artifacts", {})
        required_artifacts = {
            "part_1_1": "requirements.md",
            "part_1_2": "direction.md",
            "part_1_3": "solution.md",
        }
        artifacts_exist = all(
            req in artifacts for req in required_artifacts.values()
        )

        # 检查 Part 1 阶段是否有未解决的设计问题
        issues = state.get("issues", {})
        active_p0 = issues.get("active", {}).get("p0", [])
        active_p1 = issues.get("active", {}).get("p1", [])
        has_design_issues = len(active_p0) > 0 or len(active_p1) > 0

        # 综合判定
        converged = part1_done and artifacts_exist and not has_design_issues

        # 构建原因
        reasons: list[str] = []
        if part1_done:
            reasons.append("Part 1 三阶段已完成")
        else:
            reasons.append("Part 1 尚未完成所有阶段")
        if artifacts_exist:
            reasons.append("三阶段产物均存在")
        else:
            reasons.append("部分阶段产物缺失")
        if not has_design_issues:
            reasons.append("无挂起的设计级问题")
        else:
            reasons.append(f"存在 {len(active_p0)} P0 / {len(active_p1)} P1 设计级问题")

        reason_str = "; ".join(reasons)

        self.logger.info(
            "Part 1 语义收敛检测: converged=%s phase=%s artifacts=%s design_issues=%s",
            converged, phase, artifacts_exist, has_design_issues
        )

        return ConvergenceResult(
            converged=converged,
            counter=progress.get("convergence_counter", 0),
            reason=reason_str,
            semver_converged=converged,
            metadata={
                "phase": phase,
                "part1_done": part1_done,
                "artifacts_exist": artifacts_exist,
                "design_issues": has_design_issues,
            },
        )

    # ========================================================================
    # should_terminate() 终止判定
    # ========================================================================

    def should_terminate(self, state: dict) -> ConvergenceResult:
        """
        综合判定是否应该终止 loop-aider 循环。

        终止条件（OR 关系，满足任一即终止）:
            1. convergence_counter >= convergence_rounds 且 Part 1 语义收敛
            2. cycle > max_cycles（周期数超限）
            3. 用户手动设置了 termination.status = "completed"
            4. 检测到不可恢复的错误（P0 问题无法绕过）

        Args:
            state: 完整的 state.json 字典。

        Returns:
            ConvergenceResult 收敛结果。
        """
        progress = state.get("progress", {})
        config = state.get("config", {})
        termination = state.get("termination", {})

        # 终止条件 3: 用户手动标记完成
        if termination.get("status") == "completed":
            self.logger.info("终止判定: 用户已标记完成")
            return ConvergenceResult(
                converged=True,
                counter=progress.get("convergence_counter", 0),
                reason="用户已标记终止状态为 completed",
            )

        # 终止条件 4: P0 不可恢复错误
        active_p0 = state.get("issues", {}).get("active", {}).get("p0", [])
        if active_p0:
            self.logger.critical("终止判定: 存在未解决的 P0 问题")
            return ConvergenceResult(
                converged=True,
                counter=progress.get("convergence_counter", 0),
                reason=f"存在 {len(active_p0)} 个未解决的 P0 问题，建议人工介入",
            )

        # 终止条件 2: 周期数超限
        max_cycles = config.get("max_cycles", 5)
        current_cycle = progress.get("cycle", 1)
        if current_cycle > max_cycles:
            self.logger.warning(
                "终止判定: 周期数 %d 超过上限 %d", current_cycle, max_cycles
            )
            return ConvergenceResult(
                converged=True,
                counter=progress.get("convergence_counter", 0),
                reason=f"周期数超限 ({current_cycle}/{max_cycles})",
            )

        # 终止条件 1: 收敛计数器阈值
        convergence_rounds = config.get("convergence_rounds", 2)
        counter = progress.get("convergence_counter", 0)

        # 先检查 Part 1 语义收敛
        part1_result = self.check_part1_semantic_convergence(state)

        if counter >= convergence_rounds and part1_result.converged:
            self.logger.info(
                "终止判定: 全局收敛达成 (counter=%d >= rounds=%d, Part1=%s)",
                counter, convergence_rounds, part1_result.converged
            )
            return ConvergenceResult(
                converged=True,
                counter=counter,
                reason=f"收敛达成: counter={counter}/{convergence_rounds}, Part1 语义收敛",
                semver_converged=True,
            )

        # 未达成终止条件
        self.logger.info(
            "终止判定: 未收敛 (counter=%d/%d, Part1=%s, cycle=%d/%d)",
            counter, convergence_rounds, part1_result.converged,
            current_cycle, max_cycles,
        )
        return ConvergenceResult(
            converged=False,
            counter=counter,
            reason=f"继续迭代: counter={counter}/{convergence_rounds}",
            semver_converged=part1_result.converged,
        )

    # ========================================================================
    # 优先级操作表（priority-based operation table）
    # ========================================================================

    OPERATION_TABLE = {
        # (new_issues, issues_resolved, exit_ok) → action
        (True,  True,  True):  {"desc": "有新问题+有解决+成功", "action": "reset_counter", "next": "continue"},
        (True,  True,  False): {"desc": "有新问题+有解决+失败", "action": "reset_counter", "next": "retry"},
        (True,  False, True):  {"desc": "有新问题+无解决+成功", "action": "reset_counter", "next": "continue"},
        (True,  False, False): {"desc": "有新问题+无解决+失败", "action": "reset_counter", "next": "retry"},
        (False, True,  True):  {"desc": "无新问题+有解决+成功", "action": "inc_counter", "next": "continue"},
        (False, True,  False): {"desc": "无新问题+有解决+失败", "action": "keep_counter", "next": "retry"},
        (False, False, True):  {"desc": "无新问题+无解决+成功", "action": "inc_counter", "next": "continue"},
        (False, False, False): {"desc": "无新问题+无解决+失败", "action": "keep_counter", "next": "retry"},
    }

    def get_operation(
        self, new_issues: bool, issues_resolved: bool, exit_ok: bool
    ) -> dict:
        """
        根据优先级操作表查询推荐操作。

        Args:
            new_issues:      本轮是否引入新问题。
            issues_resolved: 本轮是否有问题被解决。
            exit_ok:         Aider 退出码是否为 0。

        Returns:
            操作字典: {"desc": ..., "action": ..., "next": ...}
        """
        key = (bool(new_issues), bool(issues_resolved), bool(exit_ok))
        result = self.OPERATION_TABLE.get(key, {
            "desc": "未知状态", "action": "keep_counter", "next": "continue"
        })
        self.logger.debug(
            "操作表查询: new=%s resolved=%s exit_ok=%s → %s",
            new_issues, issues_resolved, exit_ok, result["desc"]
        )
        return result

    def apply_operation(self, state: dict, new_issues: bool,
                        issues_resolved: bool, exit_ok: bool) -> int:
        """自动应用优先级操作表中的推荐操作。

        Args:
            state:           完整的 state.json 字典（原地修改）。
            new_issues:      本轮是否引入新问题。
            issues_resolved: 本轮是否有问题被解决。
            exit_ok:         Aider 退出码是否为 0。

        Returns:
            更新后的 convergence_counter 值。
        """
        op = self.get_operation(new_issues, issues_resolved, exit_ok)
        progress = state.setdefault("progress", {})
        counter = progress.get("convergence_counter", 0)

        if op["action"] == "reset_counter":
            progress["convergence_counter"] = 0
            self.logger.info("操作: 重置 convergence_counter → 0 (%s)", op["desc"])
            return 0
        elif op["action"] == "inc_counter":
            new_counter = counter + 1
            progress["convergence_counter"] = new_counter
            self.logger.info("操作: 递增 convergence_counter → %d (%s)",
                             new_counter, op["desc"])
            return new_counter
        else:  # keep_counter
            self.logger.info("操作: 保持 convergence_counter=%d (%s)",
                             counter, op["desc"])
            return counter
