import unittest


class BrokenTest(unittest.TestCase):
    def test_real_regression(self) -> None:
        # Deterministic: a real bug, not a race. Every repeat fails the same way.
        self.assertEqual(1 + 1, 3)


if __name__ == "__main__":
    unittest.main()
