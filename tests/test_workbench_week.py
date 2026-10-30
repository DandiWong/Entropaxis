"""Tech-175 M1-10：GUI 调 `aggregate_worklog.py` 的周边界（Q-11 / D-16，AC-19）。"""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import workbench  # noqa: E402
import workbench_view as wv  # noqa: E402

CST = dt.timezone(dt.timedelta(hours=8))


class WeekBoundaryTests(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=CST)  # 周二

    def test_since_is_iso_monday_in_cst(self):
        self.assertEqual(wv.week_since(self.NOW), "2026-11-02")

    def test_since_converts_utc_now_to_cst(self):
        # 周日 17:00Z = 周一 01:00 +08:00 → 已是新的一周
        self.assertEqual(wv.week_since(dt.datetime(2026, 11, 1, 17, 0, tzinfo=dt.timezone.utc)), "2026-11-02")
        self.assertEqual(wv.week_since(dt.datetime(2026, 11, 1, 15, 59, tzinfo=dt.timezone.utc)), "2026-10-26")

    def test_ac19_boundaries(self):
        b = lambda ts: wv.week_bucket(ts, self.NOW)  # noqa: E731
        self.assertEqual(b("2026-11-01T23:59:59+08:00"), "last")
        self.assertEqual(b("2026-11-02T00:00:00+08:00"), "this")
        self.assertEqual(b("2026-11-01T16:30:00+00:00"), "this")
        self.assertEqual(b("2026-11-09T00:00:00+08:00"), "other")
        self.assertEqual(b("2026-10-25T23:59:59+08:00"), "other")

    def test_ac19_gui_argv_always_carries_since(self):
        seen = []
        with mock.patch.object(workbench, "_run_tool", side_effect=lambda argv: seen.append(argv) or {}):
            workbench._aggregate(Path("/tmp"), workbench._default_since(), workbench._today())
            workbench._capsule_index(Path("/tmp"), workbench._default_since(), workbench._today(), None, None)
        self.assertTrue(seen)
        for argv in seen:
            self.assertIn("--since", argv)
            self.assertEqual(argv[argv.index("--since") + 1], wv.week_since())

    def test_default_since_ignores_local_timezone(self):
        with mock.patch.object(wv, "_now", return_value=dt.datetime(2026, 11, 1, 17, 0, tzinfo=dt.timezone.utc)):
            self.assertEqual(workbench._default_since(), "2026-11-02")
            self.assertEqual(workbench._today(), "2026-11-02")


if __name__ == "__main__":
    unittest.main()
