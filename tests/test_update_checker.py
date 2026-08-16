import unittest

from chatter.update_checker import is_newer, parse_version


class UpdateCheckerTests(unittest.TestCase):
    def test_parse_release_versions(self):
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertIsNone(parse_version("latest"))

    def test_only_newer_releases_are_updates(self):
        self.assertTrue(is_newer("1.0.3", "1.0.2"))
        self.assertFalse(is_newer("1.0.2", "1.0.2"))
        self.assertFalse(is_newer("0.9.9", "1.0.2"))


if __name__ == "__main__":
    unittest.main()
