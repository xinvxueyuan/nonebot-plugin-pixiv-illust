"""pixiv 插画·漫画订阅推送插件。

命令（裸词触发，qbot 的 COMMAND_START=[""]）：

    订阅插画 <画师id>    群聊。本群订阅该画师的新作（插画 + 漫画）
    退订插画 <画师id>    群聊
    插画订阅列表         群聊。htmlkit 渲染成图片

参数支持直接粘贴 pixiv 链接（`artworks/123`、`member_illust.php?...illust_id=123`、`i/123`）。

三条硬约束（用户明确要求）：
  - **不引用**：回复一律用 `finish()` 直接发，**不使用引用回复**（不传 reply 消息段）。
  - **不共享**：订阅数据按 `(group_id, author_id)` 存，**各群之间不共享**。
  - **只发 release**：本项目只通过 GitHub Release 分发，不发布到 PyPI。

> 阶段 1 只落最小占位包（能 import 即可）：命令注册、URL hook、轮询接线在阶段 10 补。
"""

from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="pixiv 插画·漫画",
    description=(
        "pixiv 插画/漫画订阅推送：按群订阅画师新作自动推送，多页作品发前 N 页（R18 图片可打码，推送/打码两轴可配）"
    ),
    usage=(
        "订阅插画 <画师id>    本群订阅该画师新作（仅群聊）\n"
        "退订插画 <画师id>    取消订阅（仅群聊）\n"
        "插画订阅列表         查看本群订阅（渲染成图片）\n"
        "参数支持直接粘贴 pixiv 链接；多页作品一条消息最多发前 N 页"
    ),
    type="application",
    homepage="https://github.com/xinvxueyuan/nonebot-plugin-pixiv-illust",
    supported_adapters={"nonebot.adapters.onebot.v11"},
)
