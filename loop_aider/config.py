"""
loop_aider/config.py —— 配置管理模块。

负责加载和合并来自 CLI 参数和 config.yml 文件的配置项，
为 loop-aider 运行时提供全局 Config 数据类。

支持四种信任模式:
    - safe:   L1 安全模式，所有 Gate 激活
    - auto:   L2 自动模式（默认），平衡安全与自动化
    - unsafe: L3 无限制模式，仅灾难性操作拦截
    - interactive: L1+ 协作模式，关键决策暂停等待用户
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    """
    loop-aider 运行时全局配置。

    Attributes:
        mode: 信任模式 ("safe" / "auto" / "unsafe" / "interactive")。
        max_cycles: 最大循环轮次，防止无限循环。
        max_part1_rounds: Part 1（设计）阶段最大回退轮数。
        convergence_rounds: 收敛所需连续无问题轮次。
        route_repeat_max: 路由同一点重复上限，超限则暂停。
        user_request: 用户原始需求（goal）。
        aider_timeout_seconds: 单次 Aider 调用超时时间（秒）。
        aider_retry_count: Aider 调用失败重试次数。
        git_semantic_commit_template: Git 语义化提交信息模板。
        model: Aider 使用的模型名称。
        lock_timeout_minutes: 锁文件过期时间（分钟）。
        interactive_timeout_minutes: 交互模式超时时间（分钟）。
        state_dir: 状态文件目录路径。
    """

    mode: str = "auto"
    max_cycles: int = 5
    max_part1_rounds: int = 5
    convergence_rounds: int = 2
    route_repeat_max: int = 3
    user_request: str = ""
    aider_path: str = "aider"
    aider_timeout_seconds: int = 600
    aider_retry_count: int = 2
    git_semantic_commit_template: str = "[loop-aider] phase={phase} cycle={cycle}"
    model: Optional[str] = None
    lock_timeout_minutes: int = 15
    interactive_timeout_minutes: int = 30
    state_dir: str = ".aider/loop-aider"

    @classmethod
    def from_args(
        cls,
        goal: str,
        mode: str = "auto",
        model: Optional[str] = None,
        max_cycles: int = 5,
        convergence_rounds: int = 2,
    ) -> Config:
        """
        从 CLI 参数创建 Config 实例，并可选地合并 config.yml 文件配置。

        Args:
            goal: 用户需求描述文本。
            mode: 信任模式。
            model: Aider 模型名称。
            max_cycles: 最大循环轮次。
            convergence_rounds: 收敛所需轮次。

        Returns:
            初始化完成的 Config 实例。
        """
        config = cls(
            mode=mode,
            user_request=goal,
            model=model,
            max_cycles=max_cycles,
            convergence_rounds=convergence_rounds,
        )
        config._load_from_file()
        return config

    def _load_from_file(self):
        """
        从 state_dir 下的 config.yml 文件加载配置覆盖项。

        文件中存在的键将覆盖 dataclass 中的对应默认值。
        若文件不存在或解析失败，则静默忽略（使用默认值）。
        """
        cfg_path = Path(self.state_dir) / "config.yml"
        if not cfg_path.exists():
            return
        try:
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for key, value in data.items():
                if hasattr(self, key):
                    setattr(self, key, value)
        except Exception:
            # 配置文件损坏或不可读时使用默认值，不中断启动
            pass

    def to_dict(self) -> dict:
        """
        将 Config 序列化为字典，用于写入 state.json。
        """
        return {
            "mode": self.mode,
            "max_cycles": self.max_cycles,
            "max_part1_rounds": self.max_part1_rounds,
            "convergence_rounds": self.convergence_rounds,
            "route_repeat_max": self.route_repeat_max,
            "user_request": self.user_request,
            "aider_timeout_seconds": self.aider_timeout_seconds,
            "aider_retry_count": self.aider_retry_count,
            "git_semantic_commit_template": self.git_semantic_commit_template,
        }
