from nonebot_plugin_pixiv_illust.config import Config


def test_defaults():
    c = Config()
    assert c.pixiv_illust_poll_interval == 600
    assert c.pixiv_illust_max_push_per_poll == 3
    assert c.pixiv_illust_max_pages == 4
    assert c.pixiv_illust_blur_r18 is True
    assert c.pixiv_illust_blur_radius == 9
    assert c.pixiv_illust_r18_push_enabled is True
    assert c.pixiv_illust_admin_only is False
    assert c.pixiv_illust_group_whitelist == []
    assert c.pixiv_illust_url_hook_cooldown == 60
    assert c.pixiv_illust_refresh_token == ""


def test_group_list_normalized_dropping_garbage():
    c = Config(pixiv_illust_group_whitelist=["868258211", "abc", 868258211])
    assert c.pixiv_illust_group_whitelist == [868258211]
