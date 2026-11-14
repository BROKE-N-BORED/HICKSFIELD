import threading
import unittest

from hicksfield.errors import CancelledError
from hicksfield.steps import run_steps


class StepsTests(unittest.TestCase):
    def test_progress_monotonic_and_weighted(self):
        seen = []

        def _report(label, fraction, detail=""):
            seen.append((label, fraction))

        def _step_a(progress, _cancel):
            progress(0.5)
            progress(1.0)
            return "a"

        def _step_b(progress, _cancel):
            progress(1.0)
            return "b"

        results = run_steps([(3, "first", _step_a), (1, "second", _step_b)],
                            report=_report)
        self.assertEqual(results, ["a", "b"])
        fractions = [f for _l, f in seen if f is not None]
        self.assertEqual(fractions, sorted(fractions))
        self.assertAlmostEqual(fractions[-1], 1.0)
        # Halfway through the first step (weight 3 of 4) is 0.375.
        self.assertIn(0.375, fractions)

    def test_indeterminate_passes_none(self):
        seen = []

        def _step(progress, _cancel):
            progress(None, "busy")
            return None

        run_steps([(1, "x", _step)],
                  report=lambda l, f, d="": seen.append(f))
        self.assertIn(None, seen)

    def test_cancel_between_steps(self):
        event = threading.Event()
        event.set()

        def _step(progress, _cancel):
            return None

        with self.assertRaises(CancelledError):
            run_steps([(1, "x", _step)], cancel_event=event)

    def test_no_report_ok(self):
        results = run_steps([(1, "x", lambda p, c: 42)])
        self.assertEqual(results, [42])


if __name__ == "__main__":
    unittest.main()
