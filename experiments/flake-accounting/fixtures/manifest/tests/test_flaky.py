import random
import unittest

FLAKE_FAILURE_RATE = 0.35


class FlakyTest(unittest.TestCase):
    def test_intermittent_race(self) -> None:
        # Real, unseeded entropy: each fresh interpreter (each repeat is its
        # own subprocess) draws independently, like a genuinely racy test.
        self.assertGreaterEqual(random.random(), FLAKE_FAILURE_RATE)


if __name__ == "__main__":
    unittest.main()
