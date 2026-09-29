from app.terabox import extract_surl, is_terabox_url

TEST_SHARE = "https://1024terabox.com/s/1SUlU5TvNnS2rzSsEWMiHBA"

def test_1024terabox_share_url():
    assert is_terabox_url(TEST_SHARE)

def test_share_prefix_is_removed():
    assert extract_surl(TEST_SHARE) == "SUlU5TvNnS2rzSsEWMiHBA"

def test_query_surl_is_preserved():
    assert extract_surl("https://1024terabox.com/share/list?surl=SUlU5TvNnS2rzSsEWMiHBA") == "SUlU5TvNnS2rzSsEWMiHBA"

def test_rejects_other_domains():
    assert not is_terabox_url("https://example.com/s/file")
