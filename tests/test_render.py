import re

import pytest
from nonebot_plugin_pixiv_illust import render, store

AVATAR_A = "https://i.pximg.net/a.jpg"
AVATAR_B = "https://i.pximg.net/b.jpg"
DATA_URI_A = "data:image/jpeg;base64,AAAA"

GROUP = 1094538078


@pytest.fixture
def _db(tmp_path):
    """用**真 store** 产出行。

    ⚠️ 这个 fixture 是必须的。初稿的 `_rows()` 手写的是 **dict 列表**，
    而生产环境 store 设了 `conn.row_factory = sqlite3.Row`，行**全是 Row**。
    Row 没有 `.get()` —— 于是 `render.py` 里 `r.get("author_name")` 在生产必抛
    AttributeError，被「回退纯文本」吞掉，**订阅列表的图片渲染整个是死的**，
    而单测因为用的是 dict 全绿。

    现在行直接从真 store 取，形状与生产完全一致，这类 bug 跑不掉。
    """
    store.init(tmp_path / "r.sqlite3")
    # 注：store.subscribe 的 created_at 由 store 自己填 time.time()，
    # 所以这里不传（订阅时间只断言格式，不断言具体值）。
    store.subscribe(GROUP, 111, baseline=100, author_name="作者甲", author_avatar_url=AVATAR_A)
    store.subscribe(GROUP, 222, baseline=50, author_name="", author_avatar_url="")
    store.set_last_seen(GROUP, 111, 900)
    store.set_last_seen(GROUP, 222, 800)
    yield


def _rows():
    """真 store 产出的行（`sqlite3.Row`），不是手写 dict。"""
    return store.list_by_group(GROUP)


def test_rows_are_really_sqlite3_rows(_db):
    """前提断言：真实行是 `sqlite3.Row` 且**没有** `.get()`。

    这行要是挂了，说明 store 的实现变了（比如换成 dict），
    下面那些「兼容 Row」的测试就失去意义了。
    """
    import sqlite3

    rows = _rows()
    assert rows, "fixture 没产出数据"
    assert isinstance(rows[0], sqlite3.Row)
    assert not hasattr(rows[0], "get")
    assert hasattr(rows[0], "keys")
    assert rows[0]["author_name"] == "作者甲"  # 但下标可以


def test_build_context_maps_rows_to_items(_db):
    ctx = render.build_context(_rows(), group_id=1094538078)
    assert ctx["group_id"] == 1094538078
    assert ctx["count"] == 2
    assert ctx["items"][0]["index"] == 1
    assert ctx["items"][0]["author_id"] == 111
    assert ctx["items"][0]["author_name"] == "作者甲"
    assert ctx["items"][0]["author_url"] == "https://www.pixiv.net/users/111"
    assert ctx["items"][0]["last_seen"] == 900
    assert ctx["truncated"] == 0


def test_build_context_includes_all_four_required_fields(_db):
    """需求：订阅列表要显示 **作者名 / 作者头像 / 订阅时间 / 作者id**，四个都不能少。"""
    item = render.build_context(_rows(), group_id=1, avatars={AVATAR_A: DATA_URI_A})["items"][0]
    assert item["author_name"] == "作者甲"
    assert item["avatar_uri"] == DATA_URI_A
    assert item["subscribed_at"]  # 订阅时间（已格式化）
    assert item["author_id"] == 111


def test_build_context_formats_subscribed_at(_db):
    ctx = render.build_context(_rows(), group_id=1)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", ctx["items"][0]["subscribed_at"])


def test_build_context_avatar_uri_none_when_missing(_db):
    """没头像 → avatar_uri 为 None，模板走首字占位块。"""
    assert render.build_context(_rows(), group_id=1)["items"][1]["avatar_uri"] is None


def test_build_context_avatar_uri_none_when_fetch_failed(_db):
    """URL 有但下载失败（不在 avatars 映射里）→ 也是 None，不能 KeyError。"""
    ctx = render.build_context(_rows(), group_id=1, avatars={})  # 空映射 = 全失败
    assert ctx["items"][0]["avatar_uri"] is None
    assert ctx["items"][0]["author_name"] == "作者甲"  # 其他字段不受影响


def test_build_context_falls_back_to_id_when_no_name(_db):
    item = render.build_context(_rows(), group_id=1)["items"][1]
    assert item["author_name"] == "222"  # 没名字就显示 ID，不显示空白


def test_build_context_caps_items_but_keeps_total(_db):
    # 造 200 条：直接用真 store（保证行的类型与生产一致）
    # ⚠️ 区间要避开 fixture 已用的 111/222，否则 INSERT OR IGNORE 会去重、数量对不上
    for i in range(1000, 1200):
        store.subscribe(GROUP, i, baseline=0, author_name="", author_avatar_url="")
    rows = _rows()
    assert len(rows) == 202  # 2（fixture） + 200
    ctx = render.build_context(rows, group_id=1, max_items=50)
    assert ctx["count"] == 202  # 总数照实报（2 + 200）
    assert len(ctx["items"]) == 50  # 只渲染前 50
    assert ctx["truncated"] == 152


def test_build_context_handles_empty():
    ctx = render.build_context([], group_id=1)
    assert ctx["count"] == 0
    assert ctx["items"] == []


@pytest.mark.asyncio
async def test_render_calls_template_to_pic_with_three_positional_args(monkeypatch, _db):
    """回归：template_to_pic 的位置参数是 (template_path, template_name, templates)。"""
    calls = {}

    async def fake_template_to_pic(template_path, template_name, templates, **kwargs):
        calls["template_path"] = str(template_path)
        calls["template_name"] = template_name
        calls["templates"] = templates
        calls["kwargs"] = kwargs
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(render, "_load_htmlkit", lambda: fake_template_to_pic)
    out = await render.render_subscription_list(_rows(), group_id=1)

    assert out.startswith(b"\x89PNG")
    assert calls["template_name"] == "subscription_list.html"
    assert calls["template_path"].endswith("templates")
    assert calls["templates"]["count"] == 2
    assert calls["kwargs"]["image_format"] == "png"


@pytest.mark.asyncio
async def test_render_passes_avatars_into_template_context(monkeypatch, _db):
    """头像必须进到模板上下文里，否则列表上不显示头像。"""
    seen = {}

    async def fake_template_to_pic(template_path, template_name, templates, **kwargs):
        seen.update(templates)
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(render, "_load_htmlkit", lambda: fake_template_to_pic)
    out = await render.render_subscription_list(_rows(), group_id=1, avatars={AVATAR_A: DATA_URI_A})
    assert out is not None
    assert seen["items"][0]["avatar_uri"] == DATA_URI_A
    assert seen["items"][0]["author_name"] == "作者甲"


@pytest.mark.asyncio
async def test_render_works_without_avatars_argument(monkeypatch, _db):
    """不传 avatars（= 全部下载失败）也要能渲染，不能抛。"""

    async def fake_template_to_pic(*a, **kw):
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(render, "_load_htmlkit", lambda: fake_template_to_pic)
    assert await render.render_subscription_list(_rows(), group_id=1) is not None


@pytest.mark.asyncio
async def test_render_returns_none_when_htmlkit_import_fails(monkeypatch, _db):
    def boom():
        raise ImportError("no htmlkit here")

    monkeypatch.setattr(render, "_load_htmlkit", boom)
    assert await render.render_subscription_list(_rows(), group_id=1) is None


@pytest.mark.asyncio
async def test_render_returns_none_when_render_raises(monkeypatch, _db):
    async def boom(*a, **kw):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(render, "_load_htmlkit", lambda: boom)
    assert await render.render_subscription_list(_rows(), group_id=1) is None


@pytest.mark.asyncio
async def test_render_skips_empty_rows_without_touching_htmlkit(monkeypatch, _db):
    called = []

    async def spy(*a, **kw):
        called.append(1)
        return b"x"

    monkeypatch.setattr(render, "_load_htmlkit", lambda: spy)
    assert await render.render_subscription_list([], group_id=1) is None
    assert called == []  # 空列表直接返回，交给调用方回退文本


def test_build_context_works_on_sqlite3_row_without_get(_db):
    """**核心回归**：`sqlite3.Row` 上没有 `.get()`，取字段必须照样成功。

    修复前 `r.get("author_name")` → AttributeError → 被 render 吞成
    「回退纯文本」→ 订阅列表永远出不了图，且**完全不报错**。
    """
    rows = _rows()
    assert not hasattr(rows[0], "get")  # 确认前提：真的没有 .get

    ctx = render.build_context(rows, group_id=GROUP, avatars={AVATAR_A: DATA_URI_A})
    item = ctx["items"][0]
    assert item["author_name"] == "作者甲"  # 修复前这里就炸了
    assert item["author_id"] == 111
    assert item["avatar_uri"] == DATA_URI_A
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", item["subscribed_at"])
    assert item["last_seen"] == 900


def test_row_get_handles_both_row_and_dict():
    """`_row_get` 要同时吃 Row 和 dict（别的调用点可能传 dict）。"""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("select 1 as a, 'x' as b").fetchone()
    conn.close()

    assert render._row_get(row, "a") == 1
    assert render._row_get(row, "b") == "x"
    assert render._row_get(row, "missing") is None  # 缺键不抛
    assert render._row_get(row, "missing", "d") == "d"

    assert render._row_get({"a": 1}, "a") == 1
    assert render._row_get({"a": 1}, "missing", "d") == "d"


# ══════════════════════════════════════════════════════════════════
# 模板回归：两件**实测**出来的事，用静态断言锁住
#
# 起因（用户反馈）：
#   ① 「订阅列表的宽高和px太小了，图渲染出来在QQ属于小图」
#   ② 「订阅列表的头像与右侧的各类元素的距离太近，粘在一起了」
# 实测根因：
#   · htmlkit 用原生渲染器 core.pyd（不是真 Chromium）**不支持 flex 的 `gap`** ——
#     `gap:10px` 时头像紧贴序号块（头像起点 52 = 序号块右缘）；
#     改用 margin 后头像起点右移 12px，间距才真的出现
#   · `dpi` 与 `max_width` 对输出像素**完全无效**（96/144/192、700/900 同尺寸），
#     图片宽度由内容固有宽度决定 → 想变大只能把 CSS 尺寸写大
#     （旧版 335x222 → 放大后约 700x376）
# ══════════════════════════════════════════════════════════════════


def _template_css() -> str:
    return (render.TEMPLATES_DIR / render.TEMPLATE_NAME).read_text(encoding="utf-8")


def _css_rules() -> dict[str, str]:
    """把模板 `<style>` 里的规则解析成 `{选择器: 声明块}`。

    两个都踩过的坑：
      · **必须去掉 `/* 注释 */`** —— 注释里写了「实测 `gap: 10px` 时…」，
        不去注释就会把解释性文字误判成「真的用了 gap」，测试假红
      · **规则可能跨多行**（如 `.avatar` 的 margin-right 在第二行），
        所以按 `}` 收块取声明，不能只看选择器所在那一行
    """
    css = _template_css()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # 去注释
    body = css.split("<style>", 1)[1].split("</style>", 1)[0]
    rules: dict[str, str] = {}
    for block in body.split("}"):
        if "{" not in block:
            continue
        sel, _, decls = block.partition("{")
        rules[sel.strip()] = decls.strip()
    return rules


def _px(decls: str, prop: str) -> float:
    """从声明块里取某个属性的 px 值。

    ⚠️ 冒号在属性名**后面**（`font-size: 26px`），早先用 `rstrip(":")` 从右边剥，
    剥不掉，于是 `float(": 26")` 抛 ValueError、被 `continue` 吞掉，
    报出「没找到 … 的 font-size」这种误导性失败。用正则直接匹配最省事。
    """
    m = re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([0-9.]+)px", decls)
    assert m, f"声明块里没有 {prop}: {decls!r}"
    return float(m.group(1))


def test_template_does_not_rely_on_flex_gap():
    """**核心回归**：flex 容器里不许用 `gap:` 做间距。

    本渲染器不支持 `gap`，写了等于没写 —— 元素直接贴在一起。
    必须用 margin/padding。这条挂了就说明有人又用回了 `gap`。
    """
    rules = _css_rules()
    offenders = {sel: decls for sel, decls in rules.items() if re.search(r"(?:^|;)\s*gap\s*:", decls)}
    assert not offenders, f"这些规则用了 `gap`，但本渲染器不支持（元素会粘住）：{offenders}"


def test_row_and_meta_use_margins_for_spacing():
    """间距必须来自 margin —— 这是本渲染器唯一生效的手段。"""
    rules = _css_rules()
    assert "margin-right" in rules[".avatar"]
    assert "margin-right" in rules[".idx"]
    assert "margin-bottom" in rules[".name"]


def test_avatar_has_spacing_on_the_text_side():
    """头像到文字那一侧必须有 margin（用户反馈「粘在一起」就是这里）。"""
    rules = _css_rules()
    avatar = rules[".avatar"]
    assert re.search(r"margin-right\s*:\s*[0-9.]+px", avatar), f".avatar: {avatar!r}"
    assert _px(avatar, "margin-right") >= 12, "头像与文字的间距太小（会显得粘住）"


def test_render_sizes_are_large_enough_for_qq():
    """字号与头像不能太小 —— 否则渲染出来在 QQ 里就是一张小图。"""
    rules = _css_rules()

    assert _px(rules[".name"], "font-size") >= 20, "画师名字号太小"
    assert _px(rules[".line2"], "font-size") >= 16, "副信息（ID/时间）字号太小"
    assert _px(rules[".url"], "font-size") >= 16, "链接字号太小"
    assert _px(rules[".avatar"], "width") >= 64, "头像太小"
    assert _px(rules[".card"], "width") >= 600, "卡片太窄（整图会偏小）"
    assert _px(rules[".title"], "font-size") >= 26, "标题字号太小"


# ══════════════════════════════════════════════════════════════════
# 本仓新增（移植时补的两条）
# ══════════════════════════════════════════════════════════════════


def test_template_visible_copy_says_huashi_not_zuozhe():
    """本仓口径：模板**可见文案**统一用「画师」，不许出现「作者」。

    `{{ it.author_* }}` 这类**占位符/变量名**照旧（数据结构是共用的），
    所以断言前先把 Jinja 占位符、HTML 注释、CSS 注释都剥掉，
    只看用户真正会看到的字。
    """
    html = _template_css()
    visible = re.sub(r"\{\{.*?\}\}", "", html, flags=re.S)  # Jinja 占位符
    visible = re.sub(r"/\*.*?\*/", "", visible, flags=re.S)  # CSS 注释
    visible = re.sub(r"<!--.*?-->", "", visible, flags=re.S)  # HTML 注释

    assert "画师" in visible
    assert "作者" not in visible, "模板可见文案里还有「作者」，本仓统一叫「画师」"


@pytest.mark.asyncio
async def test_real_htmlkit_renders_this_template_to_png(_db):
    """真 htmlkit 端到端：**这份模板本身**必须能渲染出 PNG。

    其余用例都 monkeypatch 掉了 `_load_htmlkit`，模板写错了（Jinja 语法 / 选择器）
    也照样全绿；这条用真库跑一遍，把模板钉住。
    htmlkit 没装 / 本机加载不了 C++ 扩展时 **skip**（与本仓可选依赖同口径），不是红。
    """
    pytest.importorskip("nonebot_plugin_htmlkit")
    out = await render.render_subscription_list(_rows(), group_id=GROUP)
    assert out is not None, "真 htmlkit 渲染返回 None（模板或渲染器有问题，看 WARNING 日志）"
    assert out.startswith(b"\x89PNG")
