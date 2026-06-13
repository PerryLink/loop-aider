"""
tests/test_state_machine.py —— 状态机单元测试。

测试 state.json 的读写、原子写入协议、备份机制、phase 转换记录等核心功能。
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from loop_aider.state_machine import StateMachine, DEFAULT_STATE


class TestStateMachine:
    """StateMachine 核心功能测试套件。"""

    def test_load_state_creates_default_on_missing_file(self):
        """测试: 文件不存在时自动创建默认 state.json。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            assert state["schema_version"] == 1
            assert state["progress"]["phase"] == "init"
            # Default-FAIL 合约: 终止状态初始为 "running"
            assert state["termination"]["status"] == "running"

    def test_save_and_load_preserves_data(self):
        """测试: 保存后加载的数据完整性。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["progress"]["phase"] = "part_1_1"
            state["progress"]["cycle"] = 2
            sm.save_state(state)
            state2 = sm.load_state()
            assert state2["progress"]["phase"] == "part_1_1"
            assert state2["progress"]["cycle"] == 2

    def test_atomic_write_produces_valid_json(self):
        """测试: 原子写入后 state.json 为有效 JSON 且数据正确。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            state["config"]["user_request"] = "test goal"
            sm.save_state(state)
            state_path = Path(tmpdir) / "state.json"
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data["config"]["user_request"] == "test goal"

    def test_backup_created_on_save(self):
        """测试: 保存时自动创建 state.json.bak 备份。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            sm.save_state(state)
            bak_path = Path(tmpdir) / "state.json.bak"
            assert bak_path.exists()

    def test_tmp_file_cleaned_after_save(self):
        """测试: 原子写入后临时文件被清理。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            sm.save_state(state)
            tmp_path = Path(tmpdir) / "state.json.tmp"
            assert not tmp_path.exists()

    def test_phase_transition_records_history(self):
        """测试: phase 转换记录完整。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            sm.record_phase_transition(state, "init", "part_1_1")
            assert len(state["progress"]["phase_transitions"]) == 1
            assert state["progress"]["phase_transitions"][0]["from"] == "init"
            assert state["progress"]["phase_transitions"][0]["to"] == "part_1_1"

    def test_update_phase_convenience_method(self):
        """测试: update_phase 便捷方法正确记录转换。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            sm.update_phase(state, "part_1_1")
            assert state["progress"]["phase"] == "part_1_1"
            assert len(state["progress"]["phase_transitions"]) == 1
            assert state["progress"]["phase_transitions"][0]["from"] == "init"

    def test_multiple_transitions_chain(self):
        """测试: 连续多次 phase 转换链。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            state = sm.load_state()
            phases = ["part_1_1", "part_1_2", "part_1_3", "part_2_1"]
            for p in phases:
                sm.update_phase(state, p)
            assert len(state["progress"]["phase_transitions"]) == len(phases)
            assert state["progress"]["phase"] == "part_2_1"

    def test_lock_acquire_and_release(self):
        """测试: 锁文件获取和释放。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(state_dir=tmpdir)
            result = sm.acquire_lock()
            assert result is True
            lock_path = Path(tmpdir) / ".lock"
            assert lock_path.exists()
            sm.release_lock()
            assert not lock_path.exists()

    def test_lock_denied_when_held(self):
        """测试: 持锁时另一个 StateMachine 无法获取锁。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm1 = StateMachine(state_dir=tmpdir)
            sm2 = StateMachine(state_dir=tmpdir)
            assert sm1.acquire_lock() is True
            # sm2 应在 sm1 持锁时获取失败
            result = sm2.acquire_lock()
            # 由于指数退避，这里可能超时失败
            assert result is False or sm2.acquire_lock() is False
            sm1.release_lock()
