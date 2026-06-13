"""
loop_aider/cli.py —— CLI 命令行入口模块。

提供 loop-aider 的主命令行界面，支持以下子命令:
    - run:    启动自主循环，执行 goal 驱动的全闭环开发流程。
    - status: 查看当前 state.json 状态摘要。
    - resume: 从中断的 state.json 恢复执行。
    - init:   初始化 loop-aider 工作目录（创建 state.json）。

信任模式:
    - --safe:         L1 安全模式，所有 Gate 激活。
    - --unsafe:       L3 无限制模式，仅灾难性操作拦截。
    - --interactive:  L1+ 协作模式，关键决策暂停等待用户确认。
    （默认 L2 auto 模式）

Usage:
    python -m loop_aider.cli init
    python -m loop_aider.cli run --goal "用 Python 写一个天气 CLI 工具"
    python -m loop_aider.cli run --goal "..." --model sonnet --safe --max-cycles 10
    python -m loop_aider.cli status
    python -m loop_aider.cli resume
"""

from __future__ import annotations
import argparse
import sys

from loop_aider.config import Config
from loop_aider.state_machine import StateMachine


def main():
    """CLI 主入口点。"""
    parser = argparse.ArgumentParser(
        prog="loop-aider",
        description="Aider CLI 的自主驾驶层——设定目标，全自动完成从设计到验证的闭环。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------
    # run 子命令
    # ------------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="启动 loop-aider 自主循环")
    run_parser.add_argument(
        "--goal", required=True, help="自然语言目标描述（需求/goal）"
    )
    run_parser.add_argument(
        "--model", default=None, help="Aider 使用的模型名称（如 sonnet/opus/gpt-4o）"
    )
    run_parser.add_argument(
        "--safe",
        action="store_true",
        help="L1 安全模式：所有 Gate 激活，关键步骤暂停等待确认",
    )
    run_parser.add_argument(
        "--unsafe",
        action="store_true",
        help="L3 无限制模式：仅灾难性操作硬拦截，其余 Gate 关闭",
    )
    run_parser.add_argument(
        "--interactive",
        action="store_true",
        help="L1+ 协作模式：Part 1 决策暂停等待用户，Part 2 自动执行",
    )
    run_parser.add_argument(
        "--max-cycles",
        type=int,
        default=5,
        help="最大循环轮次（默认 5）",
    )
    run_parser.add_argument(
        "--convergence-rounds",
        type=int,
        default=2,
        help="收敛所需连续无问题轮次（默认 2）",
    )

    # ------------------------------------------------------------------
    # status / resume / init 子命令
    # ------------------------------------------------------------------
    subparsers.add_parser("status", help="查看当前 state.json 状态摘要")
    subparsers.add_parser("resume", help="从中断的 state.json 恢复执行")
    subparsers.add_parser("init", help="初始化 loop-aider 工作目录")

    # ------------------------------------------------------------------
    # 参数解析
    # ------------------------------------------------------------------
    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args)

    elif args.command == "status":
        _cmd_status()

    elif args.command == "resume":
        _cmd_resume()

    elif args.command == "init":
        _cmd_init()


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace):
    """
    执行 run 子命令 —— 启动自主循环。

    流程:
        1. 解析信任模式（safe/auto/unsafe/interactive）。
        2. 创建 Config 并同步到 state.json。
        3. 执行 Aider 健康检查。
        4. 调用 AiderManager.run_phase() 执行当前 phase。
    """
    # 确定信任模式
    mode = "auto"
    if args.safe:
        mode = "safe"
    elif args.unsafe:
        mode = "unsafe"
    elif args.interactive:
        mode = "interactive"

    # 创建配置
    config = Config.from_args(
        goal=args.goal,
        mode=mode,
        model=args.model,
        max_cycles=args.max_cycles,
        convergence_rounds=args.convergence_rounds,
    )

    # 初始化状态机并同步配置
    sm = StateMachine(state_dir=config.state_dir)
    state = sm.load_state()
    state["config"]["mode"] = config.mode
    state["config"]["user_request"] = config.user_request
    state["config"]["max_cycles"] = config.max_cycles
    state["config"]["convergence_rounds"] = config.convergence_rounds
    sm.save_state(state)

    # 执行健康检查
    from loop_aider.aider_manager import AiderManager, HealthStatus

    mgr = AiderManager(
        {
            "aider_timeout_seconds": config.aider_timeout_seconds,
            "aider_path": config.aider_path,
            "model": config.model,
            "mode": config.mode,
        }
    )
    health = mgr.check_health()

    if health == HealthStatus.INCOMPATIBLE:
        print("ERROR: Aider 版本不兼容。需要 >= 0.77.0。")
        print("请升级: pip install --upgrade aider-chat")
        sys.exit(1)

    if health == HealthStatus.NOT_FOUND:
        print("ERROR: Aider CLI 未找到。请安装: pip install aider-chat")
        sys.exit(1)

    if health == HealthStatus.COMPATIBLE_WITH_WARNINGS:
        print(f"WARNING: Aider {mgr.get_version()} 存在已知兼容性限制。")

    # 输出启动信息
    print(f"loop-aider v0.1.0 | Phase: {state['progress']['phase']}")
    print(f"Aider: {mgr.get_version()} | Mode: {config.mode}")
    print(f"Goal: {config.user_request}")

    # 执行当前 phase
    result = mgr.run_phase(
        phase=state["progress"]["phase"],
        template_vars={
            "goal": config.user_request,
            "cycle": state["progress"]["cycle"],
        },
    )
    print(
        f"Completed | Exit: {result.exit_code} | "
        f"Duration: {result.duration_ms}ms"
    )
    sys.exit(0 if result.exit_code == 0 else 1)


def _cmd_status():
    """执行 status 子命令 —— 输出当前 state.json 状态摘要。"""
    sm = StateMachine()
    state = sm.load_state()
    import json

    print(
        json.dumps(
            {
                "phase": state["progress"]["phase"],
                "cycle": state["progress"]["cycle"],
                "termination": state["termination"]["status"],
            },
            indent=2,
        )
    )


def _cmd_resume():
    """执行 resume 子命令 —— 显示当前可恢复的状态信息。"""
    sm = StateMachine()
    state = sm.load_state()
    print(
        f"Phase={state['progress']['phase']} "
        f"Cycle={state['progress']['cycle']} "
        f"Status={state['termination']['status']}"
    )
    if state["termination"]["status"] == "running":
        print("上次运行未正常终止，可从当前 phase 恢复。")


def _cmd_init():
    """执行 init 子命令 —— 初始化 state.json。"""
    sm = StateMachine()
    sm.load_state()  # 会在目录下创建默认 state.json
    print(f"loop-aider 已初始化。状态文件: {sm.state_path}")
    print("下一步: loop-aider run --goal \"你的目标描述\"")


if __name__ == "__main__":
    main()
