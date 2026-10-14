"""本地干跑：验证插件能被 NoneBot 真正加载并注册全部命令。

这是「服务器隔离干跑」的本地等价物 —— 不连 QQ、不碰生产，只证明
「import 期没炸 + 3 个命令都注册上了 + 定时任务按 token 决定挂不挂」。

⚠️ 这里**故意不挂**兄弟仓的 sys.path：本脚本要走的正是「宿主只给了 nonebot 与
必需依赖」的最朴素环境 —— 合并转发 / 表态库缺失时，插件必须**照常加载**，
只是降级（下面会把两个可选依赖的状态打出来）。
真库接线由 `tests/` 负责（conftest 里挂了兄弟仓）。

跑法：
    env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE -u VIRTUAL_ENV -u PYTHONPATH \\
        uv run python scripts/smoke_plugin_load.py
"""

import sys
from pathlib import Path

import nonebot

nonebot.init(driver="~none", log_level="INFO")
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "plugins"))

EXPECTED_COMMANDS = {"订阅插画", "退订插画", "插画订阅列表"}


def main() -> int:
    nonebot.load_plugin("nonebot_plugin_pixiv_illust")

    plugins = nonebot.get_loaded_plugins()
    pixiv = next((p for p in plugins if p.name == "nonebot_plugin_pixiv_illust"), None)
    if pixiv is None:
        print("❌ 插件没被加载")
        return 1

    matchers = pixiv.matcher
    print(f"✅ 插件已加载，注册了 {len(matchers)} 个 matcher")

    # 反查每个 on_command 的命令词
    from nonebot.rule import CommandRule

    found = set()
    for m in matchers:
        for rule in m.rule.checkers:
            call = rule.call
            if isinstance(call, CommandRule):
                for cmd in call.cmds:
                    # cmds 里装的是 tuple（如 ('订阅插画',)），不是裸字符串
                    found.add(cmd[0] if isinstance(cmd, tuple) else str(cmd))

    print(f"   命令词：{sorted(found)}")
    missing = EXPECTED_COMMANDS - found
    extra = found - EXPECTED_COMMANDS
    if missing:
        print(f"❌ 缺少命令：{sorted(missing)}")
        return 2
    if extra:
        print(f"⚠️ 多出来的命令：{sorted(extra)}")

    # 可选依赖的加载状态：缺失只该降级，绝不该让插件加载失败
    import nonebot_plugin_pixiv_illust as plugin

    print(f"   表态库（message-reaction）：{'可用' if plugin.reaction_available else '不可用(降级)'}")
    print(f"   合并转发（merge-forward）：{'可用' if plugin._mf_build_nodes else '不可用(降级多图)'}")

    # 定时任务是否挂上（没配 token 时应**不**挂，只注册命令）
    from nonebot_plugin_apscheduler import scheduler

    jobs = {j.id for j in scheduler.get_jobs()}
    print(f"   定时任务：{sorted(jobs) or '（无 token → 未挂轮询）'}")

    print("\n✅ 干跑通过：插件可加载、命令齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
