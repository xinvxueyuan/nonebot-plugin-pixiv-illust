def test_package_importable():
    import nonebot_plugin_pixiv_illust  # noqa: F401


# ── 日志可见性（生产环境实测发现的问题）────────────────────────────
#
# NoneBot 把 stdlib logging 的 root logger 钉在 WARNING 且 **root 没有 handler**，
# 所以插件里的 `logger.info(...)` 在 journal 里**一条都看不到**。
# 插件在模块级挂了 NoneBot 的 LoguruHandler 并降级到 INFO。
# 这几条把该前提与修法一起锁住，防止有人「清理」掉那两行。


def test_root_logger_would_swallow_info_without_fix():
    """前提断言：如果不做处理，INFO 必定被吞。

    这行挂了说明 NoneBot 改了日志行为 —— 那下面的修法可能已不需要，
    但更要紧的是要重新确认「INFO 到底能不能进 journal」。

    注：断言的是**级别闸门**而不是 `root.handlers == []`。
    裸 python 下 root 确实没有 handler，但 **pytest 自己的 logging 插件
    会往 root 挂若干 handler**（`LogCaptureHandler` 等），所以 handler 这部分
    在测试里判断不了；真正吞掉 INFO 的是 `root.level == WARNING` 这道闸门。
    """
    import logging

    root = logging.getLogger()
    assert root.level == logging.WARNING, f"root level 变成 {root.level} 了，请重新评估日志可见性"

    # 全新 logger 不设级别 → 继承 root 的 WARNING → INFO 被 isEnabledFor 挡掉
    bare = logging.getLogger("pixiv_illust_no_level_probe")
    assert not bare.isEnabledFor(logging.INFO), "INFO 不再被吞了？那修法可以重新评估"
    assert bare.isEnabledFor(logging.WARNING)


def test_plugin_logger_attached_to_loguru_and_set_to_info():
    """7 个模块共用一个 logger 名，所以在 __init__ 里一次接好即可全覆盖。"""
    import logging

    import nonebot_plugin_pixiv_illust  # noqa: F401

    lg = logging.getLogger("nonebot_plugin_pixiv_illust")
    check_name = "nonebot_plugin_pixiv_illust"
    assert lg.name == check_name
    assert lg.getEffectiveLevel() == logging.INFO

    # 用的是 NoneBot 自带的桥
    from nonebot.log import LoguruHandler

    handlers = [h for h in lg.handlers if isinstance(h, LoguruHandler)]
    assert len(handlers) == 1, f"LoguruHandler 数量不对: {lg.handlers}"


def test_plugin_info_reaches_loguru_sink():
    """端到端：`logger.info` 真能被 loguru 收到（journal 走的就是 loguru）。"""
    import logging

    import nonebot_plugin_pixiv_illust  # noqa: F401
    from loguru import logger as loguru_logger

    captured = []
    sink = loguru_logger.add(lambda m: captured.append(m), level="INFO", format="{message}")
    try:
        lg = logging.getLogger("nonebot_plugin_pixiv_illust")
        lg.info("SMOKE_INFO_MARKER")
        lg.warning("SMOKE_WARN_MARKER")
        lg.debug("SMOKE_DEBUG_SHOULD_NOT_APPEAR")
    finally:
        loguru_logger.remove(sink)

    joined = "\n".join(captured)
    assert "SMOKE_INFO_MARKER" in joined, "INFO 没能到达 loguru → 生产 journal 里看不到"
    assert sum("SMOKE_WARN_MARKER" in m for m in captured) == 1, "WARNING 重复了"
    assert "SMOKE_DEBUG_SHOULD_NOT_APPEAR" not in joined, "级别被放宽过头"
