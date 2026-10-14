"""`urls` 纯解析函数的测试 —— 被动 hook 的「认不认得出链接」全靠它。

前半是计划阶段 3 指定的 8 条用例（只把一行式写法展开成多行以满足 ruff），
后半是参照仓 `pixiv-novel/tests/test_urls.py` 同款的 URL 变体 / 反例 / 去重用例。

穷举 URL 变体的理由：这个解析器判错**不会报错**，只会「机器人对链接毫无反应」，
或者更糟「拿画师主页 ID 当作品 ID 去查」（见 urls.py 顶部注释）。
"""

import json
from dataclasses import dataclass, field

import pytest
from nonebot_plugin_pixiv_illust import urls

# ── 计划阶段 3 的 8 条 ───────────────────────────────────────────


def test_artworks_plain():
    assert urls.parse("看这个 https://www.pixiv.net/artworks/12345678") == 12345678


def test_lang_prefix():
    assert urls.parse("https://www.pixiv.net/en/artworks/900") == 900


def test_member_illust_legacy():
    assert urls.parse("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=777") == 777


def test_short_i():
    assert urls.parse("https://www.pixiv.net/i/666") == 666


def test_anchor_stripped():
    assert urls.parse("https://www.pixiv.net/artworks/42#m1") == 42


def test_earliest_wins():
    assert urls.parse("pixiv.net/artworks/2 然后 pixiv.net/artworks/1") == 2


def test_none():
    assert urls.parse("没有链接") is None


def test_candidates_reads_json_segment():
    """QQ 把链接转成分享卡片时纯文本里一个链接都没有，链接只活在 json 段里。"""
    msg = [
        FakeSeg(
            "json",
            {
                "app": "com.tencent.structmsg",
                "meta": {"detail_1": {"qqdocurl": "https://www.pixiv.net/artworks/555"}},
            },
        )
    ]
    assert urls.parse(urls.candidates(msg)) == 555


# ── artworks：各种真实写法都要认出来 ─────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "https://www.pixiv.net/artworks/29081479",
        "https://www.pixiv.net/artworks/29081479#m1",  # 锚点（第几页）
        "https://www.pixiv.net/artworks/29081479?foo=1",  # 查询串
        "https://www.pixiv.net/en/artworks/29081479",  # 语言前缀
        "https://www.pixiv.net/zh-tw/artworks/29081479",  # 带地区的语言前缀
        "http://www.pixiv.net/artworks/29081479",  # http
        "https://pixiv.net/artworks/29081479",  # 无 www
        "看看这个 https://www.pixiv.net/artworks/29081479 挺好看",  # 夹在中文里
        "https://www.pixiv.net/ARTWORKS/29081479",  # 大小写
    ],
)
def test_parses_artworks_url_variants(text):
    assert urls.parse(text) == 29081479


# ── member_illust.php：老链接，illust_id 不一定在第一个参数位 ─────


@pytest.mark.parametrize(
    "text",
    [
        "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=29081479",
        "https://www.pixiv.net/member_illust.php?illust_id=29081479",  # id 在第一个参数位
        "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=29081479#m1",  # 锚点
        "https://www.pixiv.net/en/member_illust.php?mode=medium&illust_id=29081479",
        "https://pixiv.net/member_illust.php?mode=medium&illust_id=29081479",
        "https://www.pixiv.net/MEMBER_ILLUST.PHP?mode=medium&ILLUST_ID=29081479",  # 大小写
    ],
)
def test_parses_legacy_member_illust_variants(text):
    assert urls.parse(text) == 29081479


# ── i/：老短链 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "https://www.pixiv.net/i/29081479",
        "https://www.pixiv.net/en/i/29081479",
        "https://www.pixiv.net/i/29081479#m1",
        "https://pixiv.net/i/29081479",
    ],
)
def test_parses_short_i_variants(text):
    assert urls.parse(text) == 29081479


# ── 反例：这些不该触发 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "",
        "普通聊天没有任何链接",
        "https://www.pixiv.net/users/88871942",  # 画师主页 —— 不是作品
        "https://www.pixiv.net/novel/show.php?id=12345",  # 小说 —— 本插件不管
        "https://www.pixiv.net/novel/series/12345",  # 小说系列 —— 本插件不管
        "https://www.pixiv.net/member_illust.php?mode=medium",  # 没有 illust_id
        "https://www.pixiv.net/artworks/abc",  # id 不是数字
        "https://example.com/artworks/12345",  # 别的站
        "https://www.pixivnet.com/artworks/1234",  # 相似域名（没有点）
    ],
)
def test_no_false_positives(text):
    assert urls.parse(text) is None


def test_no_digit_count_restriction_on_ids():
    """**刻意不加「ID 至少 N 位」这类规则** —— 钉住这个选择。

    理由：短 ID 若真存在，加了长度限制就会**静默不响应**（最难查的一类故障）；
    而放它过去最坏也只是打一次 API、回一句「取作品失败」，看得见。
    """
    assert urls.parse("https://www.pixiv.net/artworks/1") == 1
    assert urls.parse("https://www.pixiv.net/i/12") == 12
    assert urls.parse("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=7") == 7


# ── 一条消息里多个链接 ───────────────────────────────────────────


def test_earliest_wins_across_url_forms():
    """跨三种形态也取最靠前的那条，而不是「先匹配 artworks 再看别的」。"""
    text = "https://www.pixiv.net/i/222 先短链 后 artworks https://www.pixiv.net/artworks/111"
    assert urls.parse(text) == 222

    text2 = "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=333 然后 https://www.pixiv.net/i/444"
    assert urls.parse(text2) == 333


def test_parse_all_dedupes_and_keeps_order():
    text = "https://www.pixiv.net/artworks/111 https://www.pixiv.net/i/222 https://www.pixiv.net/artworks/111"
    assert urls.parse_all(text) == [111, 222]


def test_parse_all_on_empty_text():
    assert urls.parse_all("") == []
    assert urls.parse_all("没有链接") == []


# ── candidates()：QQ 分享卡片那条路 ──────────────────────────────


@dataclass
class FakeSeg:
    type: str
    data: dict = field(default_factory=dict)


def test_candidates_reads_text_segments():
    msg = [FakeSeg("text", {"text": "看这个 https://www.pixiv.net/artworks/999"})]
    assert "artworks/999" in urls.candidates(msg)
    assert urls.parse(urls.candidates(msg)) == 999


def test_candidates_reads_qq_share_card_data_string():
    """真实 QQ 形状：json 段的 `data` 里还有一层**已序列化**的字符串。"""
    card = FakeSeg(
        "json",
        {
            "data": json.dumps(
                {
                    "app": "com.tencent.structmsg",
                    "meta": {"detail_1": {"qqdocurl": "https://www.pixiv.net/artworks/888"}},
                }
            )
        },
    )
    msg = [FakeSeg("text", {"text": ""}), card]
    assert urls.parse(urls.candidates(msg)) == 888


def test_candidates_accepts_plain_string():
    text = "https://www.pixiv.net/artworks/5"
    assert urls.candidates(text) == text


def test_candidates_tolerates_segments_without_data():
    """图片段之类的没有 data / data 不可序列化，不能因此抛异常。"""
    msg = [FakeSeg("image"), FakeSeg("text", {"text": "https://www.pixiv.net/artworks/777"})]
    assert urls.parse(urls.candidates(msg)) == 777


def test_candidates_handles_empty_message():
    assert urls.candidates([]) == ""
    assert urls.parse(urls.candidates([])) is None
