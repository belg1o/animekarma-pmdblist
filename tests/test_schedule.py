import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from animelists import core, jobs, schedule
from animelists import __main__ as cli
from animelists.sources.kuroiru import Kuroiru, WEEK_SECONDS


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000 // schedule.DAY * schedule.DAY  # UTC midnight
        self.opts = jobs.Options(min_members=10000, min_score=7.5)
        self.policy = schedule.Policy()

    def row(self, mal=1, **values):
        return dict(malid=mal, title=f"Show {mal}", time=self.now + 3600, lastep=2,
                    members=20000, score=8, **values)

    def plan(self, rows, names=None, opts=None):
        return schedule.build_plan({"airing": rows}, names or ["recent"],
                                   opts or self.opts, self.policy, self.now)

    def test_filters_and_premieres_match_custom_list_rules(self):
        rows = [self.row(), dict(self.row(2), members=9999),
                dict(self.row(3), score=7.49), dict(self.row(4), lastep=0),
                dict(self.row(5), members=None), dict(self.row(6), score="nan")]
        self.assertEqual([r["mal"] for r in self.plan(rows)["eligible_airing"]], [1, 4])

    def test_nearby_releases_round_up_and_share_one_check(self):
        plan = self.plan([dict(self.row(), time=self.now + 3600 + 60),
                          dict(self.row(2), time=self.now + 3600 + 5 * 60)])
        events = [e for e in plan["events"] if any(r.startswith("airing:") for r in e["reasons"])]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["at"], self.now + 2 * 3600)
        self.assertEqual(len(events[0]["reasons"]), 2)

    def test_inclusive_window_expires_after_boundary(self):
        # Previous broadcast was two hours ago; a four-hour window expires in two hours.
        row = dict(self.row(), time=self.now + WEEK_SECONDS - 2 * 3600)
        plan = self.plan([row], opts=replace(self.opts, hours=4))
        expires = [e for e in plan["events"] if "look-back expires: Show 1" in e["reasons"]]
        self.assertEqual(expires[0]["at"], self.now + 2 * 3600 + 30 * 60)

    def test_does_not_invent_weekly_airings(self):
        plan = self.plan([self.row()])
        events = [e for e in plan["events"] if "airing: Show 1" in e["reasons"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["at"], self.now + 2 * 3600)

    def test_native_plan_waits_for_provider_entry_even_with_short_custom_delay(self):
        self.policy.delay_minutes = 5
        row = dict(self.row(), time=self.now + 10 * 60)
        plan = self.plan([row])
        release = next(e for e in plan["events"] if "airing: Show 1" in e["reasons"])
        # At 01:00 it is still exactly on the provider's strict entry boundary.
        self.assertEqual(release["at"], self.now + 90 * 60)
        exit_event = next(e for e in plan["events"] if "leaves Kuroiru recent panel: Show 1" in e["reasons"])
        self.assertEqual(exit_event["at"], self.now + 4 * schedule.DAY + 90 * 60)

    def test_invalid_schedule_rows_do_not_break_plan(self):
        rows = [None, {}, dict(self.row(), time="bad"), dict(self.row(), time="inf"),
                dict(self.row(), lastep="nan"), dict(self.row(), malid=None)]
        self.assertEqual(self.plan(rows)["eligible_airing"], [])

    def test_empty_filtered_feed_still_has_daily_discovery(self):
        plan = self.plan([])
        future = [e for e in plan["events"] if self.now < e["at"] <= self.now + 7 * schedule.DAY]
        self.assertEqual(len(future), 7)

    def test_rankings_use_independent_cadences_and_utc(self):
        plan = self.plan([], names=list(jobs.JOBS))
        today = [e for e in plan["events"] if self.now < e["at"] < self.now + schedule.DAY]
        counts = {j: sum(e["job"] == j for e in today) for j in jobs.JOBS}
        self.assertEqual(counts, {"karma": 4, "recent": 1, "popular": 2, "trending": 1})
        trend = next(e for e in today if e["job"] == "trending")
        self.assertEqual(trend["at"], self.now + 18 * 3600 + 15 * 60)
        self.assertTrue(trend["cron_utc"].startswith("15 18 "))

    def test_karma_only_plan_does_not_fetch_kuroiru(self):
        source = Mock()
        type(source).data = unittest.mock.PropertyMock(side_effect=AssertionError("Unexpected feed"))
        plan = schedule.fetch_plan(["karma"], self.opts, self.policy, self.now, source)
        self.assertEqual(plan["jobs"], ["karma"])


class TickTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "schedule_state.json"
        self.now = 1_800_000_000 // schedule.DAY * schedule.DAY
        self.opts, self.policy, self.settings = jobs.Options(), schedule.Policy(), core.Settings()
        self.source = Kuroiru()
        self.source._data = {"airing": []}
        self.enterContext(patch.object(schedule, "Kuroiru", return_value=self.source))
        self.enterContext(patch.object(jobs, "check_distinct_list_ids"))
        self.enterContext(patch.object(core, "request", side_effect=AssertionError("Unexpected HTTP")))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.calls = []

    def run_jobs(self, names, opts, settings, *, results, kuroiru):
        self.calls.append(names)
        results.update({name: 0 for name in names})
        self.assertIs(kuroiru, self.source)
        return 0

    def invoke(self, now=None, names=None, dry=False, runner=None):
        with patch.object(schedule.time, "time", return_value=self.now if now is None else now), \
                patch.object(jobs, "run", side_effect=runner or self.run_jobs):
            return schedule.tick(names or ["recent"], self.opts, replace(self.settings, dry_run=dry),
                                 self.policy, str(self.path))

    def test_startup_runs_once_then_idle_tick_has_no_source_or_backend_access(self):
        self.assertEqual(self.invoke(), 0)
        with patch.object(schedule, "fetch_plan", side_effect=AssertionError("Unexpected refresh")):
            self.assertEqual(self.invoke(self.now + 300), 0)
        self.assertEqual(self.calls, [["recent"]])

    def test_only_due_job_runs_and_downtime_coalesces_events(self):
        self.invoke(names=["recent", "karma"])
        self.calls.clear()
        self.invoke(self.now + 5 * 3600, names=["recent", "karma"])
        self.assertEqual(self.calls, [["karma"]])
        self.calls.clear()
        self.invoke(self.now + 20 * 3600, names=["recent", "karma"])
        self.assertEqual(self.calls, [["recent", "karma"]])

    def test_failure_backoff_does_not_repeat_successful_job(self):
        def fail(names, opts, settings, *, results, kuroiru):
            self.calls.append(names)
            results.update(recent=0, karma=1)
            return 1
        self.assertEqual(self.invoke(names=["recent", "karma"], runner=fail), 1)
        self.invoke(self.now + 300, names=["recent", "karma"])
        self.invoke(self.now + 1800, names=["recent", "karma"])
        self.assertEqual(self.calls, [["recent", "karma"], ["karma"]])

    def test_plan_refreshes_daily_and_filter_changes_invalidate_it(self):
        self.invoke()
        with patch.object(schedule, "fetch_plan", wraps=schedule.fetch_plan) as fetch:
            self.invoke(self.now + schedule.DAY)
            self.assertEqual(fetch.call_count, 1)
            self.opts.min_score = 8
            self.invoke(self.now + schedule.DAY + 300)
            self.assertEqual(fetch.call_count, 2)

    def test_dry_run_leaves_completion_and_plan_state_untouched(self):
        self.invoke(dry=True)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])
        self.invoke()
        before = self.path.read_bytes()
        self.invoke(self.now + schedule.DAY, dry=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [["recent"]])

    def test_refresh_failure_retries_later_and_cannot_sync_without_plan(self):
        with patch.object(schedule, "fetch_plan", side_effect=ValueError("offline")) as fetch:
            self.assertEqual(self.invoke(), 1)
            self.assertEqual(self.invoke(self.now + 300), 1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(self.invoke(self.now + 1800), 1)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.calls, [])

    def test_refresh_failure_keeps_using_unexpired_cached_plan(self):
        self.invoke()
        with patch.object(schedule, "fetch_plan", side_effect=ValueError("offline")):
            self.assertEqual(self.invoke(self.now + schedule.DAY), 1)
        self.assertEqual(self.calls, [["recent"], ["recent"]])

    def test_overlapping_tick_exits_without_doing_work(self):
        with schedule._lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.calls, [])

    def test_corrupt_cache_recovers_with_fresh_plan(self):
        self.path.write_text('{"broken":', encoding="utf-8")
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(json.loads(self.path.read_text())["version"], schedule.VERSION)


class ScheduleCliTests(unittest.TestCase):
    def test_cli_passes_policy_filters_and_separate_state_path(self):
        with patch.object(cli, "load_dotenv"), patch.dict("os.environ", {}, clear=True), \
                patch.object(schedule, "tick", return_value=0) as tick:
            self.assertEqual(cli.main(["tick", "recent", "--min-score", "8", "--min-members", "20000",
                                       "--delay-minutes", "60", "--batch-minutes", "15",
                                       "--schedule-file", "custom.json", "--dry-run"]), 0)
        names, opts, settings, policy, state_file = tick.call_args.args
        self.assertEqual(names, ["recent"])
        self.assertEqual((opts.min_score, opts.min_members), (8, 20000))
        self.assertEqual((policy.delay_minutes, policy.batch_minutes), (60, 15))
        self.assertTrue(settings.dry_run)
        self.assertEqual(state_file, "custom.json")

    def test_schedule_json_preview_has_no_backend_or_state_access(self):
        output = io.StringIO()
        with patch.object(cli, "load_dotenv"), patch.dict("os.environ", {}, clear=True), \
                patch.object(core, "request", side_effect=AssertionError("Unexpected HTTP")), \
                patch.object(jobs, "run", side_effect=AssertionError("Unexpected sync")), \
                redirect_stdout(output):
            self.assertEqual(cli.main(["schedule", "karma", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["jobs"], ["karma"])

    def test_invalid_schedule_options_fail_before_fetching(self):
        for option, value in (("--refresh-hours", "169"), ("--delay-minutes", "nan"),
                              ("--retry-minutes", "0"), ("--batch-minutes", "7"),
                              ("--trending-time", "24:00")):
            with self.subTest(option=option), patch.object(cli, "load_dotenv"), \
                    patch.dict("os.environ", {}, clear=True), \
                    patch.object(core, "request", side_effect=AssertionError("Unexpected HTTP")), \
                    unittest.mock.patch("sys.stderr", new=io.StringIO()), \
                    self.assertRaises(SystemExit) as exc:
                cli.main(["schedule", "recent", option, value])
            self.assertEqual(exc.exception.code, 2)


class JobResultsTests(unittest.TestCase):
    def test_completion_requires_all_backends_and_survives_other_source_failure(self):
        source = Kuroiru()
        pmdb, mdblist = Mock(), Mock()
        pmdb.sync.return_value = 0
        mdblist.sync.side_effect = [0, 1]  # popular succeeds; recent fails on MDBList
        targets = [core.Target(1, "tv", "Test")]
        results = {}
        env = {"PMDB_LIST_ID_POPULAR": "1", "MDBLIST_LIST_ID_POPULAR": "2",
               "PMDB_LIST_ID_RECENT": "3", "MDBLIST_LIST_ID_RECENT": "4",
               "PMDB_LIST_ID_KARMA": "5"}
        with patch.dict("os.environ", env, clear=True), \
                patch.object(jobs, "configured", return_value={"pmdb": pmdb, "mdblist": mdblist}), \
                patch.object(jobs, "Mapper") as mapper, \
                patch.object(jobs.core, "State"), \
                patch.dict(jobs.SOURCES, {"popular": lambda opts, k: [],
                                          "recent": lambda opts, k: [],
                                          "karma": Mock(side_effect=ValueError("offline"))}), \
                redirect_stdout(io.StringIO()):
            mapper.return_value.build_targets.return_value = targets
            self.assertEqual(jobs.run(["popular", "recent", "karma"], jobs.Options(), core.Settings(),
                                      results=results, kuroiru=source), 1)
        self.assertEqual(results, {"popular": 0, "recent": 1, "karma": 1})


if __name__ == "__main__":
    unittest.main()
