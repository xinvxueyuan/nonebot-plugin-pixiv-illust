"""pytest 全局初始化。

**为什么必须有这个文件**：本包的 `__init__.py` 是 NoneBot 插件入口，模块级就调用
`on_command()` / `get_driver()` / `get_plugin_config()`。而测试要
`from nonebot_plugin_pixiv_illust import store` —— 这会**先执行 `__init__.py`**，
没有初始化过的 driver 就会直接抛 `ValueError: NoneBot has not been initialized.`。

`conftest.py` 会被 pytest 最先导入，所以在这里 `nonebot.init()` 正好赶在
任何测试模块 import 插件之前。
"""

import json
from pathlib import Path
from typing import Any

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

# 用 **none 驱动**：单元测试不需要任何服务器/客户端连接，
# 这样就不必为跑测试装 fastapi+uvicorn 这一大坨。
# `~` 前缀告诉 NoneBot 这是内置驱动（避免它当成已安装的第三方包）。
nonebot.init(driver="~none", log_level="WARNING")
nonebot.get_driver().register_adapter(Adapter)

FIXTURES = Path(__file__).parent / "fixtures"

# ── 表态库（可选依赖）的本地解析 ──────────────────────────────────
#
# 插件把 `nonebot-plugin-message-reaction` 当**可选依赖**：库不在时功能照跑，
# 只是没有表情反馈（`__init__.py` 里有降级替身）。
#
# 但「可选」不等于「不用测」—— 加载期那次 `from ... import with_reaction` 一旦
# 失败，插件会**静默**退到替身，于是所有表态接线断言都会在「库其实没装上」的
# 情况下照样通过（假绿）。所以这里在 import 插件**之前**把兄弟仓库挂上 sys.path，
# 让本地/能拿到两份源码的环境真的走到库那一条分支。
#
# 找不到就什么都不做 —— 没有库的环境下相关测试用 importorskip 跳过，
# 而不是把整仓测试弄红。
_REACTION_SRC = Path(__file__).resolve().parents[2] / "nonebot-plugin-message-reaction" / "src" / "plugins"
if _REACTION_SRC.is_dir():
    import sys

    if str(_REACTION_SRC) not in sys.path:
        sys.path.append(str(_REACTION_SRC))

# ── 合并转发库（可选依赖）的本地解析 ──────────────────────────────
#
# 同一个道理：`>3 页走合并转发` 的接线一旦在「库其实没装上」的环境里跑，
# 插件会静默退到「多图一条」的降级路径 —— 于是那条分支的断言**假绿**。
# 所以也把兄弟仓挂上 sys.path；找不到就什么都不做
# （`tests/test_merge_forward_wiring.py` 用 importorskip 跳过，不会把整仓弄红）。
_MERGE_FORWARD_SRC = Path(__file__).resolve().parents[2] / "nonebot-plugin-merge-forward" / "src" / "plugins"
if _MERGE_FORWARD_SRC.is_dir():
    import sys

    if str(_MERGE_FORWARD_SRC) not in sys.path:
        sys.path.append(str(_MERGE_FORWARD_SRC))


class FakeJsonDict(dict):
    """复刻 pixivpy3 `JsonDict` 的**关键语义**：缺失的键返回 `None`，**不抛异常**。

    为什么测试替身非要用它：真实 API 返回的是 JsonDict，所以

        getattr(resp, "x_restrict", 0)      # 想给个默认值 0
        resp.get("x_restrict", 0)           # 想给个默认值

    **都拿不到默认值** —— 属性式访问返回 None，`.get()` 因为键存在（值为 None）
    也返回 None。于是结构性错误（字段嵌错层、拼错名）不会报错，
    只会静默变成 0 / 空串。

    这个类让测试替身和真实 API 行为一致，这类 bug 才可能在单测里暴露。
    """

    def __getattr__(self, name: str) -> Any:
        return self.get(name)  # 缺键 → None，与真实 JsonDict 一致


# 真实响应的**嵌套** dict 也是 JsonDict（实测 `d.illust.x_restrict` 属性访问可用），
# 所以测试构造假响应时必须用 wrap_json 递归包装，否则内层变成普通 dict，
# 属性访问会抛 AttributeError —— 那是测试替身失真，不是被测代码的问题。


def load_fixture(name: str) -> FakeJsonDict:
    """读一份**真实录下来**的响应 fixture（见 scripts/record_fixtures.py）。

    用真结构而不是手写假数据 —— 手写的假数据字段全在顶层，会让
    「illust_detail 顶层是 {"illust": {...}}」这种真 bug 测不出来。
    """
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return wrap_json(raw)


def wrap_json(value: Any) -> Any:
    if isinstance(value, dict):
        return FakeJsonDict({k: wrap_json(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap_json(v) for v in value]
    return value
