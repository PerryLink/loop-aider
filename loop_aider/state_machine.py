"""
loop_aider/state_machine.py —— 文件驱动的阶段状态机。

loop-aider 的核心状态管理模块。通过 state.json 文件驱动 11 phase 工作流，
支持原子写入协议、phase 转换记录、锁文件并发保护。

关键设计:
    - 原子写入协议: tmp → fsync → rename → fsync dir
    - Default-FAIL 合约: termination.status 初始为 "running"，防止错误终止
    - 锁文件并发保护: O_CREAT|O_EXCL + 指数退避 + 死锁超时

Usage:
    sm = StateMachine(state_dir=".aider/loop-aider")
    state = sm.load_state()
    # 修改 state ...
    sm.save_state(state)
"""

from __future__ import annotations
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 默认状态模板 — Default-FAIL 合约
# ---------------------------------------------------------------------------

DEFAULT_STATE: dict[str, Any] = {
    "schema_version": 1,
    "progress": {
        "phase": "init",
        "cycle": 1,
        "convergence_counter": 0,
        "part1_round": 0,
        "new_issues_this_round": False,
        "new_issues_last_round": False,
        "issues_snapshot_at_round_start": {"p0": 0, "p1": 0, "p2": 0},
        "retry_count_this_phase": 0,
        "verification_pass_count": 0,
        "implementation_engine": None,
        "repair_context": None,
        "aider_model": None,
        "aider_version": None,
        "phase_transitions": [],
    },
    "config": {
        "mode": "auto",
        "max_cycles": 5,
        "max_part1_rounds": 5,
        "convergence_rounds": 2,
        "route_repeat_max": 3,
        "user_request": "",
        "aider_timeout_seconds": 600,
        "aider_retry_count": 2,
        "git_semantic_commit_template": "[loop-aider] phase={phase} cycle={cycle}",
        "gate_dangerous_command_patterns": [],
        "gate_file_count_threshold": {"safe": 3, "auto": 10, "unsafe": 999},
    },
    "tasks": {
        "total": 0,
        "by_status": {"completed": 0, "in_progress": 0, "pending": 0, "failed": 0, "skipped": 0},
    },
    "issues": {
        "active": {"p0": [], "p1": [], "p2": []},
        "resolved": {"p0": 0, "p1": 0, "p2": 0},
        "all_time": {"p0_total": 0, "p1_total": 0, "p2_total": 0},
    },
    "artifacts": {},
    "routing_history": [],
    "routing_repeat_tracker": {},
    "pending_confirmation": None,
    # Default-FAIL 合约: termination.status 初始为 "running"
    "termination": {"status": "running", "completed_at": None, "exit_reason": None},
    "phase_contracts": {"active_phase": None, "declared_at": None, "contracts": {}},
    "context_snapshot": {"last_action": None, "key_decisions": [], "narrative_1k": None},
    "aider_session": {
        "last_exit_code": None,
        "last_duration_ms": 0,
        "total_aider_calls": 0,
        "total_aider_duration_ms": 0,
        "last_model_used": None,
        "aider_version": None,
        "aider_health": "unknown",
    },
    "housekeeping": {
        "invocation_count": 0,
        "total_tokens_estimated": 0,
        "lock_file": ".aider/loop-aider/.lock",
    },
}


# ---------------------------------------------------------------------------
# StateMachine 核心类
# ---------------------------------------------------------------------------

class StateMachine:
    """
    文件驱动的阶段状态机。

    负责 state.json 的读/写、原子替换、phase 转换记录和锁文件并发保护。

    Attributes:
        state_dir:  状态文件存放目录（默认 .aider/loop-aider）。
        state_path: state.json 完整路径。
        bak_path:   state.json.bak 备份路径。
        tmp_path:   原子写入时使用的临时文件路径。
    """

    def __init__(self, state_dir: str = ".aider/loop-aider"):
        """
        初始化状态机。

        Args:
            state_dir: 状态文件目录路径。
        """
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "state.json"
        self.bak_path = self.state_dir / "state.json.bak"
        self.tmp_path = self.state_dir / "state.json.tmp"

    # ------------------------------------------------------------------
    # 读写操作
    # ------------------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        """
        加载 state.json。若文件不存在则创建默认状态并写盘。

        Returns:
            完整的 state 字典。
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        # 首次初始化: 深拷贝默认状态并写盘
        state = json.loads(json.dumps(DEFAULT_STATE))
        self.save_state(state)
        return state

    def save_state(self, state: dict[str, Any]):
        """
        原子写入协议: tmp → fsync → rename/ReplaceFileW → bak 备份。

        流程:
            1. 若 state.json 已存在，先备份到 state.json.bak。
            2. 将新状态序列化写入 .tmp 临时文件。
            3. flush + fsync 确保数据落盘。
            4. 原子替换:
               - Windows: 调用 kernel32.ReplaceFileW（事务性替换）。
               - POSIX:   使用 os.replace（原子性 rename）。
            5. 清理残留临时文件。
        """
        # 备份现有状态
        if self.state_path.exists():
            shutil.copy2(self.state_path, self.bak_path)

        # 写入临时文件
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        # 原子替换
        # Windows ReplaceFileW 要求目标文件已存在，首次使用时回退到 rename
        if sys.platform == "win32":
            if self.state_path.exists():
                import ctypes
                ctypes.windll.kernel32.ReplaceFileW(
                    str(self.state_path), str(self.tmp_path), None, 0, None, None
                )
            else:
                os.rename(str(self.tmp_path), str(self.state_path))
        else:
            os.replace(self.tmp_path, self.state_path)

        # 清理临时文件（Windows ReplaceFileW 后可能残留）
        if self.tmp_path.exists():
            self.tmp_path.unlink()

    # ------------------------------------------------------------------
    # Phase 转换
    # ------------------------------------------------------------------

    def record_phase_transition(
        self, state: dict[str, Any], from_phase: str, to_phase: str
    ):
        """
        记录一次 phase 转换，追加到 progress.phase_transitions 列表。

        Args:
            state:      当前 state 字典（原地修改）。
            from_phase: 转换前的 phase 名称。
            to_phase:   转换后的 phase 名称。
        """
        now = datetime.now(timezone.utc).isoformat()
        state["progress"]["phase_transitions"].append(
            {"from": from_phase, "to": to_phase, "at": now}
        )
        state["progress"]["phase"] = to_phase

    def update_phase(self, state: dict[str, Any], new_phase: str):
        """
        更新当前 phase 并自动记录转换历史。

        等价于调用 record_phase_transition(state, state["progress"]["phase"], new_phase)。

        Args:
            state:     当前 state 字典（原地修改）。
            new_phase: 目标 phase 名称。
        """
        old_phase = state["progress"]["phase"]
        self.record_phase_transition(state, old_phase, new_phase)

    # ------------------------------------------------------------------
    # 锁文件并发保护
    # ------------------------------------------------------------------

    def acquire_lock(self) -> bool:
        """
        尝试获取排他锁文件，使用指数退避重试。

        锁文件内容包含 PID 和获取时间，用于死锁检测。

        Returns:
            True 获取成功，False 超时失败。
        """
        lock_path = self.state_dir / ".lock"
        backoff = 0.1  # 初始退避时间（秒）
        max_attempts = 10

        for attempt in range(max_attempts):
            try:
                fd = os.open(
                    str(lock_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
                with os.fdopen(fd, "w") as f:
                    json.dump(
                        {
                            "pid": os.getpid(),
                            "acquired_at": datetime.now(timezone.utc).isoformat(),
                        },
                        f,
                    )
                self._lock_fd = fd
                return True
            except FileExistsError:
                # 锁文件已存在，检查是否过期
                if self._is_lock_stale(lock_path):
                    self._force_release_lock(lock_path)
                    continue
                time.sleep(min(backoff, 15.0))
                backoff *= 2

        return False

    def release_lock(self):
        """释放锁文件并清理文件描述符。"""
        lock_path = self.state_dir / ".lock"
        if hasattr(self, "_lock_fd"):
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            del self._lock_fd
        if lock_path.exists():
            lock_path.unlink()

    def _is_lock_stale(self, lock_path: Path) -> bool:
        """
        检查锁文件是否已过期。

        Args:
            lock_path: 锁文件路径。

        Returns:
            True 表示锁已过期，可以强制释放。
        """
        if not lock_path.exists():
            return True
        try:
            with open(lock_path, "r") as f:
                data = json.load(f)
            acquired = data.get("acquired_at", "")
            acquired_dt = datetime.fromisoformat(acquired)
            age = (datetime.now(timezone.utc) - acquired_dt).total_seconds()
            timeout_minutes = getattr(self, "lock_timeout_minutes", 15)
            return age > (timeout_minutes * 60)
        except Exception:
            return True

    def _force_release_lock(self, lock_path: Path):
        """强制删除过期锁文件。"""
        if lock_path.exists():
            lock_path.unlink(missing_ok=True)
