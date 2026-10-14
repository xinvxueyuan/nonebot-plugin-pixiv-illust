"""权限闸门（纯函数，便于单测）。

本插件有**两类互相独立**的闸门：
  - `is_admin` / `is_admin_identity` → 「谁能用命令」与「谁是管理员本人」
  - `is_group_allowed`               → 「插件在哪些群生效」

插画/漫画没有「全文」概念，所以小说插件里的「渠道轴 / R18 全文轴」
（`decide_text_delivery`）在本插件**不存在** —— 不要照搬回来。
"""

from __future__ import annotations

from collections.abc import Iterable


def _as_id_set(ids: Iterable[object]) -> set[str]:
    """统一成字符串集合：NoneBot 的 user_id 是 str，配置里可能写 int。"""
    return {str(i) for i in ids}


def is_admin_identity(
    user_id: object,
    *,
    admin_ids: Iterable[object],
    superusers: Iterable[object],
) -> bool:
    """**纯身份**判定：这个 QQ 号是不是管理员 —— 与「闸门开关」无关。

    为什么必须和 `is_admin()` 分开（这里踩过一个坑）：
    `is_admin()` 在 `admin_only=False` 时对**所有人**返回 True（那是「放行」的语义）。
    如果把它的结果直接当作「可以绕过限制」的判据，
    那么一旦把 `pixiv_illust_admin_only` 关掉让普通群友也能用命令，
    **每个普通群友都会被当成管理员**，相关限制随之彻底失效 —— 改配置等于白改。

    所以两件事分开：
      · 「能不能用这个命令」→ `is_admin()`（含 admin_only 开关）
      · 「是不是管理员本人」→ 本函数（只看名单）
    """
    uid = str(user_id)
    explicit = _as_id_set(admin_ids)
    if explicit:
        return uid in explicit
    return uid in _as_id_set(superusers)


def is_admin(
    user_id: object,
    *,
    admin_only: bool,
    admin_ids: Iterable[object],
    superusers: Iterable[object],
) -> bool:
    """命令闸门：这个用户能不能用命令。

    admin_only=False            → 所有人放行（闸门关掉）
    admin_ids 非空              → 只认这个名单
    admin_ids 为空 且 admin_only → 回退 NoneBot 全局 SUPERUSERS
    """
    if not admin_only:
        return True
    return is_admin_identity(user_id, admin_ids=admin_ids, superusers=superusers)


def is_group_allowed(
    group_id: object,
    *,
    whitelist: Iterable[object],
    blacklist: Iterable[object] = (),
) -> bool:
    """群白名单/黑名单闸门：这个群能不能用插件。

    判断顺序（**测试依赖这个顺序，不要改**）：
        ① 群号在黑名单里 → **拒绝**（黑名单优先于白名单）
        ② 白名单为空 → 放行（功能关闭）
        ③ 群号为 None（私聊）→ 放行（这两份名单管的是「群」）
        ④ 群号在白名单里 → 放行

    **空列表 = 关闭该名单**，不是「谁都不许用」/「谁都放行」：
    - 白名单空 = 不限制（默认，新装即可用）；
    - 黑名单空 = 不拉黑任何人（默认）。
      （这两份都是「限制名单」，空 = 没限制；别拿「允许名单」型配置的
       语义照搬过来 —— 那种空列表是「都不受理」，正好相反。）

    黑名单排第一是刻意的：用户 2026-10-02 要求「清空白名单、把某个群设为黑名单」，
    也就是「不限制别的群，单独禁掉这一个」。若白名单优先，那条配置会完全失效
    （白名单空了本就不存在冲突），但一旦两者都填，语义必须是黑名单赢 ——
    否则「我明确禁了这个群」会被一条白名单悄悄推翻。

    ⚠️ 两边都转 `str` 再比：NoneBot 的 `group_id` 是 int，而配置里可能
    写成字符串（用户手改 .env 时很常见），直接 `in` 比对会静默不匹配。
    """
    denied = {str(x).strip() for x in blacklist}
    if group_id is not None and str(group_id).strip() in denied:
        return False

    allowed = {str(x).strip() for x in whitelist}
    if not allowed:
        return True
    if group_id is None:
        return True
    return str(group_id).strip() in allowed
