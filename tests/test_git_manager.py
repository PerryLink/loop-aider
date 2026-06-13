"""
tests/test_git_manager.py —— GitManager 单元测试（Milestone 3）

测试 GitManager 的核心功能：工作区检查、语义化提交、diff 获取、变更文件列表。
使用临时 Git 仓库进行隔离测试。
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from loop_aider.git_manager import GitManager


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def git_repo():
    """创建临时 Git 仓库并返回 GitManager 实例。

    每个测试在其中独立运行。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        # 初始化 Git 仓库
        subprocess.run(
            ["git", "init"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        # 配置用户信息（提交必需）
        subprocess.run(
            ["git", "config", "user.email", "test@loop-aider.test"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Loop Aider Test"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        # 创建初始文件并提交
        (repo_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )

        gm = GitManager(cwd=str(repo_path))
        yield gm


# =============================================================================
# check_workspace_clean 测试
# =============================================================================

class TestCheckWorkspaceClean:
    """check_workspace_clean 测试套件。"""

    def test_workspace_clean_on_fresh_repo(self, git_repo):
        """新仓库工作区应干净。"""
        assert git_repo.check_workspace_clean() is True

    def test_workspace_dirty_after_file_modification(self, git_repo):
        """修改文件后工作区应不干净。"""
        readme = Path(git_repo.cwd) / "README.md"
        readme.write_text("# Modified\n", encoding="utf-8")
        assert git_repo.check_workspace_clean() is False

    def test_workspace_dirty_after_new_file(self, git_repo):
        """新建未跟踪文件后工作区应不干净。"""
        (Path(git_repo.cwd) / "newfile.py").write_text("print('hi')\n")
        assert git_repo.check_workspace_clean() is False

    def test_workspace_clean_after_commit(self, git_repo):
        """提交后工作区应恢复干净。"""
        (Path(git_repo.cwd) / "test.py").write_text("x = 1\n")
        subprocess.run(
            ["git", "add", "test.py"],
            cwd=str(git_repo.cwd),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add test.py"],
            cwd=str(git_repo.cwd),
            capture_output=True,
            check=True,
        )
        assert git_repo.check_workspace_clean() is True


# =============================================================================
# get_changed_files 测试
# =============================================================================

class TestGetChangedFiles:
    """get_changed_files 测试套件。"""

    def test_no_changed_files_on_fresh_repo(self, git_repo):
        """新仓库无变更文件。"""
        files = git_repo.get_changed_files()
        assert len(files) == 0

    def test_returns_modified_file(self, git_repo):
        """返回已修改的文件。"""
        (Path(git_repo.cwd) / "README.md").write_text("# Updated\n")
        files = git_repo.get_changed_files()
        assert "README.md" in files

    def test_untracked_files(self, git_repo):
        """get_untracked_files 返回未跟踪文件。"""
        (Path(git_repo.cwd) / "untracked.py").write_text("x=1\n")
        untracked = git_repo.get_untracked_files()
        assert "untracked.py" in untracked

    def test_all_changed_files_includes_untracked(self, git_repo):
        """get_all_changed_files 包含未跟踪文件。"""
        (Path(git_repo.cwd) / "modified.md").write_text("# mod\n")
        (Path(git_repo.cwd) / "new.py").write_text("y=2\n")
        all_files = git_repo.get_all_changed_files()
        # modified.md tracked change, new.py untracked
        assert len(all_files) >= 1


# =============================================================================
# get_diff 测试
# =============================================================================

class TestGetDiff:
    """get_diff 测试套件。"""

    def test_no_diff_on_fresh_repo(self, git_repo):
        """新仓库无 diff。"""
        diff = git_repo.get_diff()
        assert diff == "" or diff.strip() == ""

    def test_diff_after_modification(self, git_repo):
        """修改后有 diff 内容。"""
        readme = Path(git_repo.cwd) / "README.md"
        readme.write_text("# Modified Content\n", encoding="utf-8")
        diff = git_repo.get_diff()
        assert len(diff) > 0
        assert "Modified Content" in diff

    def test_file_diff_for_specific_file(self, git_repo):
        """获取单个文件 diff。"""
        readme = Path(git_repo.cwd) / "README.md"
        readme.write_text("# Single File Diff\n", encoding="utf-8")
        diff = git_repo.get_file_diff("README.md")
        assert "README.md" in diff or len(diff) > 0

    def test_diff_stat(self, git_repo):
        """diff stat 返回统计信息。"""
        readme = Path(git_repo.cwd) / "README.md"
        readme.write_text("# Stats Test\n", encoding="utf-8")
        stat = git_repo.get_diff_stat()
        assert "README.md" in stat or len(stat) > 0


# =============================================================================
# create_semantic_commit 测试
# =============================================================================

class TestCreateSemanticCommit:
    """create_semantic_commit 测试套件。"""

    def test_no_commit_when_clean(self, git_repo):
        """工作区干净时不提交。"""
        result = git_repo.create_semantic_commit(
            phase="part_2_2", cycle=1, extra="test"
        )
        assert result is False

    def test_commit_creates_with_semantic_message(self, git_repo):
        """有变更时创建语义化提交。"""
        (Path(git_repo.cwd) / "src.py").write_text("def main(): pass\n")
        result = git_repo.create_semantic_commit(
            phase="part_2_2", cycle=3, extra="实现核心功能"
        )
        assert result is True
        # 验证提交信息
        log = git_repo.get_commit_log(max_count=1)
        assert len(log) == 1
        assert "part_2_2" in log[0]["message"]
        assert "cycle=3" in log[0]["message"]

    def test_commit_without_extra(self, git_repo):
        """无 extra 也可提交。"""
        (Path(git_repo.cwd) / "test.py").write_text("x=1\n")
        result = git_repo.create_semantic_commit(phase="part_1_1", cycle=1)
        assert result is True

    def test_multiple_commits_in_sequence(self, git_repo):
        """连续多次提交。"""
        for i in range(3):
            f = Path(git_repo.cwd) / f"file_{i}.py"
            f.write_text(f"# file {i}\n")
            result = git_repo.create_semantic_commit(
                phase=f"part_2_{i+2}", cycle=i+1, extra=f"file {i}"
            )
            assert result is True
        log = git_repo.get_commit_log(max_count=5)
        assert len(log) >= 3


# =============================================================================
# build_commit_message 测试
# =============================================================================

class TestBuildCommitMessage:
    """build_commit_message 静态方法测试。"""

    def test_basic_message(self):
        """基本消息格式。"""
        msg = GitManager.build_commit_message("part_2_2", 1)
        assert "[loop-aider]" in msg
        assert "phase=part_2_2" in msg
        assert "cycle=1" in msg

    def test_message_with_extra_and_files(self):
        """含 extra 和文件列表的消息。"""
        msg = GitManager.build_commit_message(
            "part_2_2", 3, extra="完成登录模块",
            affected_files=["src/auth.py", "src/models.py"],
        )
        assert "完成登录模块" in msg
        assert "src/auth.py" in msg
        assert "src/models.py" in msg


# =============================================================================
# 辅助方法测试
# =============================================================================

class TestGitManagerHelpers:
    """GitManager 辅助方法测试套件。"""

    def test_is_git_repo(self, git_repo):
        """is_git_repo 对仓库返回 True。"""
        assert git_repo.is_git_repo() is True

    def test_not_git_repo(self):
        """is_git_repo 对非仓库目录返回 False。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            gm = GitManager(cwd=tmpdir)
            assert gm.is_git_repo() is False

    def test_get_current_branch(self, git_repo):
        """获取当前分支名。"""
        branch = git_repo.get_current_branch()
        assert len(branch) > 0

    def test_get_last_commit_hash(self, git_repo):
        """获取最后一次提交 hash。"""
        hash_val = git_repo.get_last_commit_hash(short=True)
        assert len(hash_val) > 0
        assert len(hash_val) <= 8  # short hash

    def test_get_commit_log(self, git_repo):
        """获取提交日志。"""
        log = git_repo.get_commit_log(max_count=5)
        assert len(log) >= 1
        assert "hash" in log[0]
        assert "message" in log[0]
