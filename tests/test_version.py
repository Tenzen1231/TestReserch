import unittest

from shop.version import VERSION


class VersionTests(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertEqual(len(VERSION.split(".")), 3)
