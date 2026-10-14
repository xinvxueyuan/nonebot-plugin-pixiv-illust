"""被动 hook 与 3 个命令的**接线**断言。

为什么单独一个文件：这类错误纯函数测试**看不见** ——
「hook 的 priority / block 取什么值」全写在装饰器参数里，逻辑函数一个都不受影响。
变异一下（把 hook 改回 priority=10 或 block=True）这里必须变红。

钉住的几条：
  1. 命令 `priority=10, block=True`、hook `priority=20, block=False`
     → `订阅插画 <画师主页链接>` 只走命令那条路，**不会**被 hook 再回一次卡片；
       hook 也不吞别人的消息（别的插件照常收到）。
  2. 命令集合恰好是那 3 个（多出来 / 少掉都要被发现）。
  3. 4 个入口（3 命令 + hook）都过群白名单，且**白名单排在管理员闸门之前**。
  4. refresh_token 为空时回退全局 `PIXIV_REFRESH_TOKEN`。
  5. 启动时按「有没有 token」决定要不要挂轮询任务；轮询回调要注入 `_send_extra`。
  6. `extra.commands` 里有那 3 个命令（info 插件读它出菜单）。
"""

import re
from types import SimpleNamespace

import pytest
import source_introspect

pytest.importorskip("nonebot")

import nonebot_plugin_pixiv_illust as plugin

COMMAND_MATCHERS = ("subscribe_cmd", "unsubscribe_cmd", "list_cmd")
EXPECTED_COMMANDS = {"订阅插画", "退订插画", "插画订阅列表"}


def _command_names(matcher) -> list[str]:
    """从 matcher 的 rule 里抠出命令名。

    NoneBot 2.5 把命令名藏在 `rule.dependent` 的私有属性里（`dep.call` 取到的是 None），
    可读的稳定形式是 repr：`Rule(Dependent(call=Command(cmds=(('订阅插画',),))))`。
    所以这里用 repr + 正则提取。

    如果哪天 NoneBot 改了 repr，`_require_extraction_works()` 会先炸 ——
    而不是让这些断言对着**空集合**真空通过。
    """
    return re.findall(r"'([^']+)'", str(getattr(matcher, "rule", "")))


def _require_extraction_works() -> None:
    """护栏：提取手段必须真的能拿到命令名，否则整组测试是假的。"""
    got = _command_names(plugin.subscribe_cmd)
    assert got, (
        "从 rule repr 里提取不到命令名了（NoneBot 换了 repr 或 rule 结构）。"
        "请更新 _command_names 的提取方式，别让接线测试真空通过。"
    )


# ── 命令注册 ──────────────────────────────────────────────────────


def test_extraction_guard():
    _require_extraction_works()


def test_command_names_are_exactly_the_three_expected():
    _require_extraction_works()
    found: set[str] = set()
    for name in COMMAND_MATCHERS:
        found.update(_command_names(getattr(plugin, name)))
    assert found == EXPECTED_COMMANDS, f"命令集合变了：{found}"


def test_commands_block_and_run_before_the_hook():
    """**接线核心**（变异测试的靶子）。

    命令 `block=True` 会阻断**更低优先级** matcher 的传播，
    hook 又在更低的 20 → `订阅插画 <画师链接>` **不会**被回两次。
    """
    for name in COMMAND_MATCHERS:
        m = getattr(plugin, name)
        assert m.priority == 10, f"{name}.priority 变成 {m.priority}，顺序关系被破坏"
        assert m.block is True, f"{name}.block 必须为 True，否则 hook 会对同一条消息再回一次"


def test_hook_runs_last_and_never_blocks():
    assert plugin.url_hook.priority == 20, (
        f"url_hook.priority 变成 {plugin.url_hook.priority} —— 它必须**大于**命令的 10，否则命令的 block 拦不住它"
    )
    assert plugin.url_hook.block is False, "url_hook 必须是旁观者（block=False)；改成 True 会吞掉别人的消息"


def test_hook_has_no_command_rule():
    """hook 不该绑命令规则（否则就变成第 4 个命令了）。"""
    assert _command_names(plugin.url_hook) == []


def test_hook_config_defaults():
    assert plugin.plugin_config.pixiv_illust_url_hook_enabled is True
    assert plugin.plugin_config.pixiv_illust_url_hook_cooldown == 60


# ── 元数据（info 插件读 extra.commands 出菜单）─────────────────────


def test_metadata_lists_the_three_commands():
    meta = plugin.__plugin_meta__
    assert meta.name == "pixiv 插画·漫画"

    commands = [str(c["command"]) for c in meta.extra["commands"]]
    assert [c.split(" ")[0] for c in commands] == ["订阅插画", "退订插画", "插画订阅列表"]
    for name in EXPECTED_COMMANDS:
        assert any(name in c for c in commands), f"extra.commands 里少了「{name}」"
    assert all(c.get("desc") for c in meta.extra["commands"]), "每条命令都要有 desc"


# ── 闸门顺序（群白名单在管理员之前）──────────────────────────────


def test_the_handler_extractor_still_finds_all_handlers():
    """护栏：AST 提取一旦失效，下面那些断言会**恒真**（空字典 → 循环不执行）。

    所以先钉住 handler 名字集。少了/多了都会红，逼人回来修提取或补闸门。
    """
    found = set(source_introspect.handlers(source_introspect.plugin_tree()))
    assert found == {
        "subscribe_cmd",
        "unsubscribe_cmd",
        "list_cmd",
        "url_hook",
    }


def test_every_handler_checks_the_group_whitelist():
    """三个命令 + 被动 hook 都必须过群白名单。

    漏一个的症状很有欺骗性：比如只有 hook 漏了，则是「命令不能用但贴链接照回卡片」，
    用户会以为白名单没生效。
    """
    tree = source_introspect.plugin_tree()
    for name, node in source_introspect.handlers(tree).items():
        assert "_is_group_allowed" in source_introspect.called_names(node), f"{name} 没有过群白名单闸门"


def test_group_gate_runs_before_the_admin_gate():
    """白名单必须在管理员闸门**之前**。

    否则管理员在非白名单群里照样能用 —— 白名单就只限制了普通群友，等于没限制。
    """
    tree = source_introspect.plugin_tree()
    for name, node in source_introspect.handlers(tree).items():
        group_line = source_introspect.first_call_lineno(node, "_is_group_allowed")
        admin_line = source_introspect.first_call_lineno(node, "_is_admin")
        if admin_line is None:
            continue
        assert group_line is not None
        assert group_line < admin_line, f"{name} 里管理员闸门排在群白名单前面"


def test_url_hook_returns_silently_when_group_not_allowed():
    """被动 hook 被白名单拒绝时要**静默返回**，不能回一句「不在白名单」。

    群里贴个链接就被 bot 呛一句「本插件未在此群启用」，比不回还烦人；
    而且会让别的群友以为 bot 坏了。
    """
    tree = source_introspect.plugin_tree()
    hook = source_introspect.handlers(tree)["url_hook"]
    assert source_introspect.guards_with_bare_return(hook, "_is_group_allowed"), "url_hook 的白名单分支不是静默 return"


def test_group_gate_is_fed_by_both_lists():
    """`_is_group_allowed` 必须把**两个**字段都传给 policy。

    漏传黑名单的表现是「配了黑名单但完全不生效」—— 而 policy 那层的单测全绿，
    因为它测的是 policy 自己。这类「参数没接上」只有静态接线断言能抓。
    """
    tree = source_introspect.plugin_tree()
    fn = next(
        node
        for node in tree.body
        # ⚠️ 这里用「函数定义」基类判定：`_is_group_allowed` 是**同步** def，
        # 只匹配 AsyncFunctionDef 会 StopIteration。
        if isinstance(node, source_introspect.FUNCTION_DEFS) and node.name == "_is_group_allowed"
    )
    passed = {
        kw.arg
        for node in source_introspect.ast.walk(fn)
        if isinstance(node, source_introspect.ast.Call)
        for kw in node.keywords
    }
    assert "whitelist" in passed, "_is_group_allowed 没有把白名单传给 policy"
    assert "blacklist" in passed, "_is_group_allowed 没有把黑名单传给 policy"


async def test_gate_actually_denies_a_blacklisted_group(monkeypatch):
    """端到端：改配置后闸门真的拒（而不是只有 policy 单测通过）。"""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    event = GroupMessageEvent.model_construct(group_id=1094538078, user_id=10001)

    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_group_whitelist", [])
    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_group_blacklist", [1094538078])
    assert plugin._is_group_allowed(event) is False

    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_group_blacklist", [])
    assert plugin._is_group_allowed(event) is True

    # 生产形态：白名单清空 + 黑名单单个群 —— 别的群要照常可用
    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_group_blacklist", [999])
    assert plugin._is_group_allowed(event) is True


# ── refresh_token 回退全局 PIXIV_REFRESH_TOKEN ─────────────────────


def test_token_falls_back_to_the_global_pixiv_key(monkeypatch):
    """插件自己的键为空时回退全局 `PIXIV_REFRESH_TOKEN`（同一个 pixiv 账号）。

    两个 pixiv 插件共用一份凭据，换 token 只想改一处 —— 回退链断掉的表现是
    「配了全局键但轮询一直不推」，而且不报错。
    """
    from nonebot import get_driver

    global_cfg = get_driver().config

    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_refresh_token", "")
    monkeypatch.setattr(global_cfg, "pixiv_refresh_token", "GLOBAL_TOKEN", raising=False)
    assert plugin._resolve_refresh_token() == "GLOBAL_TOKEN"

    # 插件自己的键优先（两个都配时不被全局顶掉）
    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_refresh_token", "OWN_TOKEN")
    assert plugin._resolve_refresh_token() == "OWN_TOKEN"

    # 两边都空 → 空串，走「只注册命令、不启动轮询」那条路
    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_refresh_token", "")
    monkeypatch.setattr(global_cfg, "pixiv_refresh_token", "", raising=False)
    assert plugin._resolve_refresh_token() == ""


def test_token_resolution_ignores_blank_strings(monkeypatch):
    """只写了空白字符不算「配了 token」—— 别把它当成有效值去 auth（那必然失败）。"""
    from nonebot import get_driver

    monkeypatch.setattr(plugin.plugin_config, "pixiv_illust_refresh_token", "   ")
    monkeypatch.setattr(get_driver().config, "pixiv_refresh_token", "GLOBAL_TOKEN", raising=False)
    assert plugin._resolve_refresh_token() == "GLOBAL_TOKEN"


# ── 调度器接线 ────────────────────────────────────────────────────


async def test_startup_registers_poll_job_only_when_token_is_configured(monkeypatch, tmp_path):
    """有 token 才挂轮询任务，且间隔取自配置。

    没 token 时**只注册命令**（否则任务每轮都自己 warning 一遍，日志被刷）。
    """
    from nonebot_plugin_apscheduler import scheduler

    jobs: list[dict] = []
    monkeypatch.setattr(
        scheduler, "add_job", lambda fn, trigger, **kw: jobs.append({"fn": fn, "trigger": trigger, **kw})
    )
    # 不碰真实 localstore 目录：本用例只验调度器接线
    # （localstore 的目录解析要求「调用方是被 NoneBot 加载的插件」，
    #   而测试里直接调 `_startup` 拿不到那个身份 —— 这是测试环境差异，不是生产问题）
    import nonebot_plugin_localstore as localstore

    monkeypatch.setattr(localstore, "get_plugin_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(plugin.store, "default_db_path", lambda: tmp_path / "unused.sqlite3")
    monkeypatch.setattr(plugin.store, "init", lambda path: None)
    monkeypatch.setattr(plugin.store, "all_authors", list)
    monkeypatch.setattr(plugin.avatars, "init", lambda path: None)

    monkeypatch.setattr(plugin, "_resolve_refresh_token", lambda: "")
    await plugin._startup()
    assert jobs == []

    monkeypatch.setattr(plugin, "_resolve_refresh_token", lambda: "tk")
    await plugin._startup()
    assert len(jobs) == 1
    assert jobs[0]["trigger"] == "interval"
    assert jobs[0]["seconds"] == plugin.plugin_config.pixiv_illust_poll_interval
    assert jobs[0]["id"] == "pixiv_illust_poll"
    assert jobs[0]["replace_existing"] is True
    assert jobs[0]["fn"] is plugin._poll_job


async def test_poll_job_injects_send_and_send_extra(monkeypatch):
    """轮询必须把 `_send_extra` 注入进去 —— 否则 >3 页永远走不到合并转发。"""
    seen: dict[str, object] = {}

    fake_client = SimpleNamespace(refresh_token="tk", _invalidate_api=lambda: None)

    async def fake_poll_once(client, config, *, send, send_pages=None):
        seen.update(client=client, config=config, send=send, send_pages=send_pages)
        return 0

    monkeypatch.setattr(plugin, "client", fake_client)
    monkeypatch.setattr(plugin, "poll_once", fake_poll_once)
    monkeypatch.setattr(plugin, "_resolve_refresh_token", lambda: "tk")

    await plugin._poll_job()

    assert seen["client"] is fake_client
    assert seen["config"] is plugin.plugin_config
    assert seen["send"] is plugin._send
    assert seen["send_pages"] is plugin._send_extra
