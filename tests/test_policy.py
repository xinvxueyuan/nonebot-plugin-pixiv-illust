"""policy：命令闸门 / 纯身份 / 群名单 —— 全部是纯函数矩阵。

插画/漫画没有「全文」概念，参照仓里那条「全文投递决策」（渠道轴 × R18 轴）
在本插件不存在，因此本文件**没有**任何全文投递用例 —— 别照搬回来。
"""

from nonebot_plugin_pixiv_illust import policy
from nonebot_plugin_pixiv_illust.policy import is_admin, is_admin_identity

# ── 管理员闸门（需求：指令可配置为仅管理员可用）───────────────


def test_admin_only_off_lets_everyone_in():
    assert is_admin(999, admin_only=False, admin_ids=[], superusers=["1"]) is True


def test_explicit_admin_ids_take_precedence_over_superusers():
    assert is_admin(123, admin_only=True, admin_ids=[123], superusers=["999"]) is True
    assert is_admin(999, admin_only=True, admin_ids=[123], superusers=["999"]) is False


def test_falls_back_to_superusers_when_admin_ids_empty():
    qbot_superusers = ["1330509996", "2846018938"]
    assert is_admin(1330509996, admin_only=True, admin_ids=[], superusers=qbot_superusers) is True
    assert is_admin(42, admin_only=True, admin_ids=[], superusers=qbot_superusers) is False


def test_admin_check_accepts_both_str_and_int():
    """NoneBot 的 user_id 是 str；配置里可能写成 int。两边都要认。"""
    assert is_admin("1330509996", admin_only=True, admin_ids=[], superusers=["1330509996"]) is True
    assert is_admin(1330509996, admin_only=True, admin_ids=[1330509996], superusers=[]) is True


def test_nobody_is_admin_when_admin_only_and_both_lists_empty():
    assert is_admin(1, admin_only=True, admin_ids=[], superusers=[]) is False


# ── 群白名单：纯函数矩阵 ──────────────────────────────────────────
#
# 最要紧的一条是**空列表的语义**：空 = 关闭白名单（全放行），
# 而不是「谁都不许用」。两者写反的表现都是「群里发命令毫无反应」，
# 且不报任何错 —— 所以用测试把它钉死。


def test_empty_whitelist_disables_the_feature_and_allows_everyone():
    """空列表 = **关闭白名单**（默认），不是「全拒」。

    写法锚点：白名单是「限制名单」，空 = 没限制。
    """
    assert policy.is_group_allowed(123, whitelist=[]) is True
    assert policy.is_group_allowed(999999, whitelist=[]) is True


def test_whitelisted_group_passes():
    assert policy.is_group_allowed(123, whitelist=[123]) is True
    assert policy.is_group_allowed(123, whitelist=[123, 456]) is True


def test_group_outside_whitelist_is_denied():
    assert policy.is_group_allowed(456, whitelist=[123]) is False


def test_private_message_is_never_blocked_by_the_group_whitelist():
    """私聊不是「群」，白名单不该管它。

    如果这里返回 False，用户一配白名单就没法在私聊用插件 ——
    而白名单的语义明明是「限制群」，属于把范围放大了。
    """
    assert policy.is_group_allowed(None, whitelist=[123]) is True


def test_int_and_str_group_ids_both_match():
    """NoneBot 的 `event.group_id` 是 int，而用户手改 `.env` 时可能写成字符串。

    不两边都转 str 的话，`123 in {"123"}` 是 False ——
    表现为「配了白名单但没生效」，完全静默。
    """
    assert policy.is_group_allowed(123, whitelist=["123"]) is True
    assert policy.is_group_allowed("123", whitelist=[123]) is True
    assert policy.is_group_allowed(123, whitelist=[" 123 "]) is True


# ── 群黑名单（2026-10-02 用户要求新增）──────────────────────────────


def test_empty_blacklist_is_disabled_by_default():
    """空黑名单 = 谁都不拉黑（默认），不是「全拒」。"""
    assert policy.is_group_allowed(123, whitelist=[], blacklist=[]) is True
    assert policy.is_group_allowed(999999, whitelist=[], blacklist=[]) is True


def test_blacklisted_group_is_denied_with_empty_whitelist():
    """用户的实际配置形态：**清空白名单 + 只拉黑一个群**。

    也就是「不限制别的群，单独禁掉这一个」。白名单空了，拦人的只剩黑名单。
    """
    assert policy.is_group_allowed(1094538078, whitelist=[], blacklist=[1094538078]) is False
    assert policy.is_group_allowed(868258211, whitelist=[], blacklist=[1094538078]) is True


def test_blacklist_beats_whitelist():
    """两份名单都命中同一个群时，**黑名单赢**。

    这条是刻意的优先级：用户明确说「禁掉这个群」却被一条白名单悄悄推翻，
    是最难查的一类「配置没生效」。
    """
    assert policy.is_group_allowed(123, whitelist=[123], blacklist=[123]) is False


def test_blacklist_does_not_block_other_whitelisted_groups():
    assert policy.is_group_allowed(456, whitelist=[123, 456], blacklist=[123]) is True


def test_private_message_is_never_blocked_by_the_group_blacklist():
    """私聊不是「群」，黑名单也不该管它。"""
    assert policy.is_group_allowed(None, whitelist=[], blacklist=[123]) is True


def test_blacklist_matches_int_and_str():
    assert policy.is_group_allowed(123, whitelist=[], blacklist=["123"]) is False
    assert policy.is_group_allowed("123", whitelist=[], blacklist=[123]) is False
    assert policy.is_group_allowed(" 123 ", whitelist=[], blacklist=[123]) is False


# ══════════════════════════════════════════════════════════════════
# 身份 ≠ 闸门：这两件事必须分开
#
# 踩过的坑：`is_admin()` 在 `admin_only=False` 时对**所有人**返回 True（放行语义）。
# 若把它直接当作「是不是管理员」的判据，那么一关掉 admin_only 让普通群友也能用命令，
# **每个群友都会被当成管理员** —— 改配置等于白改。
# ══════════════════════════════════════════════════════════════════


def test_identity_ignores_admin_only_switch():
    """身份判定只看名单，不受 admin_only 影响。"""
    su = ["1330509996", "2846018938"]
    # 闸门关掉时：闸门对所有人放行，但身份仍然只认名单里的人
    assert is_admin(42, admin_only=False, admin_ids=[], superusers=su) is True
    assert is_admin_identity(42, admin_ids=[], superusers=su) is False

    assert is_admin(1330509996, admin_only=False, admin_ids=[], superusers=su) is True
    assert is_admin_identity(1330509996, admin_ids=[], superusers=su) is True


def test_identity_prefers_explicit_admin_ids():
    assert is_admin_identity(123, admin_ids=[123], superusers=["999"]) is True
    assert is_admin_identity(999, admin_ids=[123], superusers=["999"]) is False


def test_gate_and_identity_agree_when_admin_only_is_on():
    """admin_only=True 时两者结论一致（闸门就是身份）。"""
    su = ["1"]
    for uid in (1, 2):
        for ids in ([], [1]):
            assert is_admin(uid, admin_only=True, admin_ids=ids, superusers=su) == is_admin_identity(
                uid, admin_ids=ids, superusers=su
            )


def test_config_default_is_admin_only_false():
    """配置默认值回归：默认必须是 False。

    True 的话只有管理员能用命令，而普通群友根本进不来 ——
    群里其它限制对实际使用者等于失效（能进来的人全是管理员）。
    """
    from nonebot_plugin_pixiv_illust.config import Config

    assert Config().pixiv_illust_admin_only is False
