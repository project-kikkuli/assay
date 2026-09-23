import unittest

from pkg.main import greet


class GreetTest(unittest.TestCase):
    def test_greet(self) -> None:
        self.assertEqual(greet("  Ada  "), "hello ada")


if __name__ == "__main__":
    unittest.main()
