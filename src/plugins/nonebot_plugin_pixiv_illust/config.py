"""插件配置。

所有字段都从 NoneBot 全局配置读取（大小写不敏感，字段名大写即环境变量名）。
即 `pixiv_illust_refresh_token` ← `.env.dev` 里的 `PIXIV_ILLUST_REFRESH_TOKEN`。
列表字段在 .env 里写 JSON，如 `PIXIV_ILLUST_GROUP_WHITELIST=[868258211]`。

前缀是 `PIXIV_ILLUST_*`，与 pixiv 小说插件的 `PIXIV_*` **完全不冲突**（两个插件可以同机共存）。

`pixiv_illust_refresh_token` 留空时，`__init__.py` 会回退读全局 `PIXIV_REFRESH_TOKEN`
（同一个 pixiv 账号，省得换 token 时漏改一处）。

R18 相关有**两根互相独立的轴 + 一个非轴开关**，**不要合并**
（插画/漫画没有「全文」概念，所以小说的「渠道轴 / R18 全文轴」在本插件不存在）：
  - `pixiv_illust_r18_push_enabled` → R18 **新作**要不要推送（默认 true）
  - `pixiv_illust_blur_r18` / `pixiv_illust_blur_radius` → R18 **图片**模糊开关与半径（不是「轴」）
"""

import logging

from pydantic import BaseModel, Field, ValidationInfo, field_validator

logger = logging.getLogger("nonebot_plugin_pixiv_illust")


class Config(BaseModel):
    # ---- 凭据 ----
    pixiv_illust_refresh_token: str = Field(
        default="",
        description="pixiv refresh_token；为空时回退全局 PIXIV_REFRESH_TOKEN",
    )

    # ---- 网络 ----
    pixiv_illust_proxy: str = Field(
        default="http://127.0.0.1:1081",
        description="访问 pixiv 的代理地址；pixiv 域名在国内直连不通，留空=直连",
    )

    # ---- 轮询 ----
    pixiv_illust_poll_interval: int = Field(
        default=600,
        ge=60,
        description="轮询间隔（秒）。下限 60 秒，防止触发 pixiv 限流",
    )
    pixiv_illust_max_push_per_poll: int = Field(
        default=3,
        ge=1,
        le=20,
        description="单次轮询里，每个订阅最多推几条，防止一次性刷屏",
    )

    # ---- 多页作品 ----
    pixiv_illust_max_pages: int = Field(
        default=4,
        ge=1,
        le=30,
        description="多页作品一次最多发几页图；多余页不发图，只在文案里写「共 X 页，本条发送前 N 页」",
    )
    pixiv_illust_image_max_width: int = Field(
        default=0,
        ge=0,
        le=2400,
        description="页图最大宽度（像素）。**0 = 发原图**（去掉 CDN 缩放段后的 1200 档，默认）；"
        ">0 时服务端把图缩到该宽度再发，用来控制消息体积",
    )

    # ---- R18 图片 ----
    pixiv_illust_blur_r18: bool = Field(default=True, description="R18 页图是否高斯模糊")
    pixiv_illust_blur_radius: int = Field(
        default=9,
        ge=6,
        le=12,
        description="高斯模糊半径（像素）。取值 6~12，默认 9；固定像素，不随图片尺寸缩放",
    )

    # ---- R18 新作推送（与上面的图片模糊开关是**两回事**，别混）----
    pixiv_illust_r18_push_enabled: bool = Field(
        default=True,
        description="R18 新作是否推送；false=遇到 R18 新作静默跳过（高水位照常推进）",
    )

    # ---- 权限 ----
    # 默认 **False**：让群里普通成员也能用这 3 个命令，
    # 这样「R18 推送轴」才真的对非管理员生效（否则能进来的人全是管理员，
    # 而管理员按需求可以绕过该轴 → 该轴等于不存在）。
    # 要恢复「仅管理员可用」，把 PIXIV_ILLUST_ADMIN_ONLY 设成 true 即可。
    pixiv_illust_admin_only: bool = Field(
        default=False,
        description="指令是否仅管理员可用；**默认 false=所有人可用**",
    )
    pixiv_illust_admin_ids: list[int] = Field(
        default_factory=list,
        description="管理员 QQ 名单；留空=回退到 NoneBot 全局 SUPERUSERS。",
    )

    # ---- 群白名单 ----
    # 空列表 = **关闭白名单功能**（所有群都放行，默认行为）。
    # 一旦填了群号，插件只在这些群里工作：3 个命令 + 被动 URL hook + 新作推送都会被拦。
    # 私聊不受影响（白名单管的是「群」）。
    pixiv_illust_group_whitelist: list[int] = Field(
        default_factory=list,
        description="群白名单：**只**在这些群里启用插件。空列表=关闭白名单（全放行，默认）；"
        "填了就是白名单，如 [868258211]",
    )
    pixiv_illust_group_blacklist: list[int] = Field(
        default_factory=list,
        description="群黑名单：这些群里**永不**启用插件（优先级高于白名单）。"
        "空列表=关闭黑名单（默认）；如 [1094538078]",
    )

    # ---- 被动 URL hook：消息里出现 pixiv 插画/漫画链接就回文案 + 图 ----
    pixiv_illust_url_hook_enabled: bool = Field(
        default=True,
        description="群里/私聊里出现 pixiv 插画/漫画链接时，自动回一条「文案 + 前 N 页图」",
    )
    pixiv_illust_url_hook_cooldown: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="同一作品在同一会话里的去重窗口（秒）；防刷屏，也省 pixiv 配额。0=不去重",
    )

    @field_validator("pixiv_illust_group_whitelist", "pixiv_illust_group_blacklist", mode="before")
    @classmethod
    def _normalize_group_list(cls, v: object, info: ValidationInfo) -> object:
        """群号统一成 int 并去重，保留首次出现顺序；转不成 int 的项**丢弃**。

        为什么不抛：这类名单混进一个手滑的字符（如空串、带标注的 `123456(书友群)`）
        就让整个插件起不来，代价远大于丢掉那一项。
        但**丢弃必须留痕**，否则用户会以为那个群生效了却收不到任何推送。

        白名单与黑名单共用这一份实现（命名按字段动态取，日志才不会把黑名单的
        手滑项说成白名单的 —— 那会把人带偏到反方向去查）。
        """
        field_name = getattr(info, "field_name", "group_list")
        if not isinstance(v, list):
            return v
        out: list[int] = []
        for item in v:
            try:
                gid = int(str(item).strip())
            except (TypeError, ValueError):
                logger.warning(f"{field_name.upper()} 里跳过无法解析为群号的项: {item!r}")
                continue
            if gid not in out:
                out.append(gid)
        return out
