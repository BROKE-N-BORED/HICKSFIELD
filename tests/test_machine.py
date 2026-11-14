import unittest

from hicksfield import machine


class MachineTests(unittest.TestCase):
    def test_parse_meminfo(self):
        text = (
            "MemTotal:        8123456 kB\n"
            "MemFree:         1234567 kB\n"
            "MemAvailable:    2345678 kB\n"
            "NotANumber:      abc kB\n"
            "NoColonLine\n"
        )
        parsed = machine.parse_meminfo(text)
        self.assertEqual(parsed["MemTotal"], 8123456)
        self.assertEqual(parsed["MemAvailable"], 2345678)
        self.assertNotIn("NotANumber", parsed)

    def test_mem_from_fixture_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "meminfo"
            fixture.write_text(
                "MemTotal:        8192000 kB\n"
                "MemAvailable:    4096000 kB\n", encoding="utf-8")
            self.assertEqual(machine.mem_total_mb(str(fixture)), 8000)
            self.assertEqual(machine.mem_available_mb(str(fixture)), 4000)

    def test_mem_missing_file_returns_zero(self):
        self.assertEqual(machine.mem_total_mb("/nonexistent/meminfo"), 0)

    def test_worker_threads_capped_at_two(self):
        self.assertLessEqual(machine.worker_threads(), 2)
        self.assertGreaterEqual(machine.worker_threads(), 1)

    def test_ram_check_ok(self):
        ok, available, message = machine.ram_check(100, available_mb=500)
        self.assertTrue(ok)
        self.assertEqual(available, 500)
        self.assertIn("500", message)

    def test_ram_check_short(self):
        ok, available, message = machine.ram_check(3072, available_mb=100)
        self.assertFalse(ok)
        self.assertIn("100", message)
        self.assertIn("3072", message)

    def test_machine_summary_has_numbers(self):
        summary = machine.machine_summary()
        self.assertIn("RAM:", summary)
        self.assertIn("CPUs:", summary)
        self.assertIn("Worker threads:", summary)


if __name__ == "__main__":
    unittest.main()
