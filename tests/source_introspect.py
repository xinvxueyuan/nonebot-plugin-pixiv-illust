"""静态接线断言的 AST 工具。

**为什么不直接对源码做字符串匹配**（踩过的坑）：注释里出现的符号名会被算进去。
实测：URL hook 的注释里写了「见下面的 `_build_card_with_reaction`」，
于是「表态调用排在闸门之后」的断言**误判为失败**（注释里的名字位置更靠前）。
反向同样危险 —— 注释里提到某个调用会让「这条链路没接上」的断言**假通过**。

AST 只看真实代码，注释与字符串天然被忽略。所有静态断言都应走这里，
不要退回去写 `src.index("...")`。
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "plugins" / "nonebot_plugin_pixiv_illust" / "__init__.py"

#: 函数定义节点的两种形态。**别只用 `AsyncFunctionDef`**：插件里有同步的
#: `def`（如 `_is_group_allowed`、`_session_key`），只匹配异步版
#: 会在 `next(...)` 上抛 `StopIteration`（踩过）。
FUNCTION_DEFS: tuple[type, ...] = (ast.FunctionDef, ast.AsyncFunctionDef)


def plugin_tree() -> ast.Module:
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def handlers(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef]:
    """取所有 `@<matcher>.handle()` 装饰的 handler。

    ⚠️ 这些 handler 的函数名**全都是 `_`**，只能靠装饰器区分 ——
    按函数名找会在拿到第一个就以为找齐了。
    """
    out: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            # ⚠️ `@x.handle()` 在 AST 里是 **Call**（func=Attribute），不是裸 Attribute。
            # 只判 Attribute 会一个都提取不到 —— 于是所有「每个 handler 都要…」的
            # 断言对空集合恒真（假绿）。这条踩过。
            target = deco.func if isinstance(deco, ast.Call) else deco
            if isinstance(target, ast.Attribute) and target.attr == "handle":
                out[ast.unparse(target.value)] = node
    return out


def called_names(node: ast.AST) -> set[str]:
    """这个节点（含其子树）里出现过的被调用函数名。"""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def first_call_lineno(node: ast.AST, name: str) -> int | None:
    """该名字**第一次被调用**的行号（没调用返回 None）。"""
    lines = [
        sub.lineno
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and (
            (isinstance(sub.func, ast.Name) and sub.func.id == name)
            or (isinstance(sub.func, ast.Attribute) and sub.func.attr == name)
        )
    ]
    return min(lines) if lines else None


def decorated_with(node: ast.AST, name: str) -> bool:
    """节点是否带指定装饰器（支持 `@x` 与 `@x(...)` 两种写法）。"""
    for deco in getattr(node, "decorator_list", []):
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Name) and target.id == name:
            return True
        if isinstance(target, ast.Attribute) and target.attr == name:
            return True
    return False


def guards_with_bare_return(node: ast.AST, name: str) -> bool:
    """存在形如 `if not <name>(...): return` 的**静默返回**分支。

    用于「被拒绝时不回文案」这类断言 —— 若分支体不是裸 return
    （而是 `await matcher.finish(...)`），这里返回 False。
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.If):
            continue
        if name not in called_names(sub.test):
            continue
        if len(sub.body) == 1 and isinstance(sub.body[0], ast.Return) and sub.body[0].value is None:
            return True
    return False
