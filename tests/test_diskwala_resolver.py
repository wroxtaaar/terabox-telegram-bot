import unittest

from vps.diskwala_resolver import DISKWALA_HOSTS, extract_diskwala_id, is_diskwala_url


class DiskwalaResolverTests(unittest.TestCase):
    def test_supported_hosts(self):
        self.assertIn("diskwala.com", DISKWALA_HOSTS)
        self.assertIn("www.diskwala.com", DISKWALA_HOSTS)
        self.assertTrue(
            is_diskwala_url(
                "https://www.diskwala.com/app/67986402b822b800041a11e4"
            )
        )

    def test_reject_other_hosts(self):
        self.assertFalse(
            is_diskwala_url(
                "https://example.com/app/67986402b822b800041a11e4"
            )
        )

    def test_extract_share_id(self):
        value = "https://www.diskwala.com/app/67986402b822b800041a11e4"
        self.assertEqual(
            extract_diskwala_id(value),
            "67986402b822b800041a11e4",
        )


if __name__ == "__main__":
    unittest.main()
