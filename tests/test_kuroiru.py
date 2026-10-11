import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from animelists import core, jobs
from animelists.sources.kuroiru import (Kuroiru, WEEK_SECONDS, RECENT_DELAY_SECONDS,
                                      RECENT_WINDOW_SECONDS, _ranked, recent_rows)


class KuroiruFilterTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000
        # Feed order deliberately differs from hit count and release order.
        self.rows = [
            {"malid": 1, "title": "Below members", "hits": 500, "members": 9999, "score": 9},
            {"malid": 2, "title": "At both minimums", "hits": 300, "members": "10000", "score": "7.5"},
            {"malid": 3, "title": "Below score", "hits": 400, "members": 20000, "score": 7.49},
            {"malid": 4, "title": "Above both", "hits": 200, "members": 20000, "score": 8},
            {"malid": 5, "title": "Unknown values", "hits": 100},
        ]
        for row in self.rows:
            row.update(time=self.now + WEEK_SECONDS - row["malid"] * 3600, lastep=2)

    def ids(self, rows):
        return [row["id"] for row in rows]

    def test_defaults_preserve_unfiltered_rankings(self):
        self.assertEqual(self.ids(_ranked(self.rows, 30)), [1, 3, 2, 4, 5])
        self.assertEqual(self.ids(recent_rows({"airing": self.rows}, self.now, 96)),
                         [1, 2, 3, 4, 5])

    def test_each_filter_can_be_used_independently(self):
        self.assertEqual(self.ids(_ranked(self.rows, 30, min_members=10000)), [3, 2, 4])
        self.assertEqual(self.ids(_ranked(self.rows, 30, min_score=7.5)), [1, 2, 4])

    def test_combined_minimums_are_inclusive_and_apply_before_top(self):
        self.assertEqual(self.ids(_ranked(self.rows, 2, min_members=10000, min_score=7.5)),
                         [2, 4])

    def test_recent_filters_preserve_release_order_and_window(self):
        rows = recent_rows({"airing": self.rows}, self.now, 96,
                           min_members=10000, min_score=7.5)
        self.assertEqual(self.ids(rows), [2, 4])
        rows = recent_rows({"airing": self.rows}, self.now, 3,
                           min_members=10000, min_score=7.5)
        self.assertEqual(self.ids(rows), [2])

    def test_enabled_filters_exclude_missing_and_invalid_values(self):
        for field, minimum in (("members", 10000), ("score", 7.5)):
            for value in (None, "", "unknown", "nan", "inf", "-inf", True, [], {}):
                row = dict(self.rows[1], **{field: value})
                with self.subTest(field=field, value=value):
                    self.assertEqual(_ranked([row], 30, **{f"min_{field}": minimum}), [])
                    self.assertEqual(recent_rows({"airing": [row]}, self.now, 96,
                                                 **{f"min_{field}": minimum}), [])

    def test_extract_only_wires_filters_to_all_kuroiru_jobs(self):
        source = Kuroiru()
        source._data = {"airing": self.rows, "popular": self.rows}
        response = Mock()
        response.json.return_value = self.rows
        output = io.StringIO()
        opts = jobs.Options(top=2, extract_only=True, min_members=10000, min_score=7.5)
        karma = [core.Item(name="Karma unaffected", mal=99)]
        with patch.object(jobs, "Kuroiru", return_value=source), \
                patch("animelists.sources.kuroiru.time.time", return_value=self.now), \
                patch.object(core, "request", return_value=response) as request, \
                patch.object(jobs.animekarma, "fetch", return_value=karma), \
                patch.object(jobs, "configured", side_effect=AssertionError("Unexpected backend")), \
                redirect_stdout(output):
            self.assertEqual(jobs.run(list(jobs.JOBS), opts, core.Settings()), 0)
        extracted = json.loads(output.getvalue())
        for name in ("recent", "popular", "trending"):
            self.assertEqual([item["mal"] for item in extracted[name]], [2, 4])
        self.assertEqual(extracted["karma"][0]["mal"], 99)
        request.assert_called_once()

    def test_default_matches_provider_panel_entry_and_exit_boundaries(self):
        ages = [1800, RECENT_DELAY_SECONDS, RECENT_DELAY_SECONDS + 1,
                RECENT_WINDOW_SECONDS, RECENT_WINDOW_SECONDS + RECENT_DELAY_SECONDS,
                RECENT_WINDOW_SECONDS + RECENT_DELAY_SECONDS + 1]
        rows = [dict(malid=i, title=f"Show {i}", time=self.now + WEEK_SECONDS - age, lastep=2)
                for i, age in enumerate(ages, 1)]
        picked = recent_rows({"airing": rows}, self.now)
        self.assertEqual(self.ids(picked), [3, 4, 5])
        self.assertEqual(picked[0]["hours_ago"], 0)
        self.assertEqual(picked[-1]["hours_ago"], 96)
        # An explicit custom window retains the old broadcast-age behavior.
        self.assertEqual(self.ids(recent_rows({"airing": rows}, self.now, 96)), [1, 2, 3, 4])

    def test_provider_panel_membership_then_quality_filters(self):
        rows = [dict(self.rows[1], lastep=0), self.rows[3],
                dict(self.rows[1], malid=6, lastep=12, totalep=12)]
        self.assertEqual(self.ids(recent_rows({"airing": rows}, self.now,
                                            min_members=10000, min_score=7.5)), [6, 4])


if __name__ == "__main__":
    unittest.main()
