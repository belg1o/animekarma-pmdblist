import os
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from animelists import __main__ as cli
from animelists import jobs


class EnvironmentTests(unittest.TestCase):
    def invoke(self, root, env, argv=None):
        def capture(names, opts, settings):
            self.names = names or [j for j in jobs.JOBS
                                   if any(jobs.list_id(j, b) for b in jobs.BACKENDS)]
            self.opts = opts
            self.settings = settings
            return 0

        with patch.dict(os.environ, env, clear=True), \
                patch.object(cli, "__file__", str(root / "animelists" / "__main__.py")), \
                patch.object(cli.jobs, "run", side_effect=capture), \
                patch.object(cli.core, "request", side_effect=AssertionError("Unexpected HTTP")):
            self.assertEqual(cli.main(argv or ["run"]), 0)

    def test_project_env_loads_job_ids_keys_and_cli_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text(
                '\ufeffMDBLIST_API_KEY="test-key"\n'
                'MDBLIST_LIST_ID_KARMA=123 # inline comment\n'
                'MDBLIST_LIST_ID_RECENT=456\n'
                'MIN_KARMA=321\nWINDOW_DAYS=3.5\nDRY_RUN=1\n', encoding="utf-8")
            self.invoke(root, {})
            self.assertEqual(self.names, ["karma", "recent"])
            self.assertEqual(self.settings.mdblist_key, "test-key")
            self.assertTrue(self.settings.dry_run)
            self.assertEqual(self.opts.min_karma, 321)
            self.assertEqual(self.opts.window_days, 3.5)
            self.assertIsNone(self.opts.hours)

    def test_shell_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text(
                'MDBLIST_API_KEY=file-key\nMDBLIST_LIST_ID_KARMA=123\n'
                'MIN_KARMA=321\nDRY_RUN=1\n', encoding="utf-8")
            self.invoke(root, {"MDBLIST_API_KEY": "shell-key", "MIN_KARMA": "999",
                               "MDBLIST_LIST_ID_KARMA": "", "DRY_RUN": "0"})
            self.assertEqual(self.names, [])
            self.assertEqual(self.settings.mdblist_key, "shell-key")
            self.assertEqual(self.opts.min_karma, 999)
            self.assertFalse(self.settings.dry_run)

    def test_missing_env_file_allows_shell_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            self.invoke(Path(folder), {"PMDB_API_KEY": "shell-key", "PMDB_LIST_ID": "123"})
            self.assertEqual(self.names, ["karma"])
            self.assertEqual(self.settings.pmdb_key, "shell-key")

    def test_kuroiru_filters_load_from_env_and_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text('MIN_MEMBERS=10000\nMIN_SCORE=7.5\n', encoding="utf-8")
            self.invoke(root, {})
            self.assertEqual(self.opts.min_members, 10000)
            self.assertEqual(self.opts.min_score, 7.5)
            self.invoke(root, {"MIN_MEMBERS": "20000", "MIN_SCORE": "8"})
            self.assertEqual(self.opts.min_members, 20000)
            self.assertEqual(self.opts.min_score, 8)
            self.invoke(root, {}, ["run", "--min-members", "5000", "--min-score", "6.5"])
            self.assertEqual(self.opts.min_members, 5000)
            self.assertEqual(self.opts.min_score, 6.5)

    def test_kuroiru_filters_are_disabled_when_unset_or_blank(self):
        with tempfile.TemporaryDirectory() as folder:
            for env in ({}, {"MIN_MEMBERS": "", "MIN_SCORE": " "}):
                with self.subTest(env=env):
                    self.invoke(Path(folder), env)
                    self.assertIsNone(self.opts.min_members)
                    self.assertIsNone(self.opts.min_score)

    def test_invalid_kuroiru_thresholds_fail_before_running_jobs(self):
        invalid = {"MIN_MEMBERS": ("-1", "1.5", "many"),
                   "MIN_SCORE": ("-1", "10.1", "nan", "inf", "good")}
        with tempfile.TemporaryDirectory() as folder:
            for name, values in invalid.items():
                for value in values:
                    with self.subTest(name=name, value=value), redirect_stderr(io.StringIO()), \
                            self.assertRaises(SystemExit) as exc:
                        self.invoke(Path(folder), {name: value})
                    self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
