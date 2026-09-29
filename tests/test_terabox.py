from app.terabox import is_terabox_url


def test_1024terabox_share_url():
    assert is_terabox_url("https://1024terabox.com/s/1SUlU5TvNnS2rzSsEWMiHBA")


def test_rejects_other_domains():
    assert not is_terabox_url("https://example.com/s/file")
