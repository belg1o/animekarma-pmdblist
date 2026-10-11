import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from animelists import core, ids
from animelists.backends.mdblist import API, MDBList


class MappingCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "state.json")
        self.now = 2_000_000_000
        self.item = core.Item("Unmapped anime", mal=64008)

    def run_lookup(self, now, *, force=False, dry_run=False, response=None):
        settings = core.Settings(state_file=self.path, mdblist_key="test-key",
                                 pause=0, force=force, dry_run=dry_run)
        output = io.StringIO()
        response = response or Mock(status_code=404)
        with patch.object(core.time, "time", return_value=now), \
                patch.object(core, "request", return_value=response) as request, \
                patch.object(ids, "_download_fribb", return_value=({}, {})) as fribb, \
                redirect_stdout(output):
            state = core.State(settings)
            backend = MDBList(settings, state)
            targets = ids.Mapper(state, [backend.resolve]).build_targets([self.item])
        return targets, request, fribb, output.getvalue()

    def test_yesterdays_miss_survives_restart_without_another_lookup(self):
        targets, request, _, output = self.run_lookup(self.now)
        self.assertEqual(targets, [])
        self.assertEqual([call.args[1] for call in request.call_args_list],
                         [f"{API}/mal/show/64008", f"{API}/mal/movie/64008"])
        self.assertNotIn("cached miss", output)

        targets, request, fribb, output = self.run_lookup(self.now + 86400)
        self.assertEqual(targets, [])
        request.assert_not_called()
        fribb.assert_not_called()
        self.assertIn("cached miss; lookup skipped", output)
        saved = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(saved["miss"]["mal:64008"], self.now)

    def test_expired_miss_is_retried_after_three_days(self):
        self.run_lookup(self.now)
        _, request, fribb, output = self.run_lookup(self.now + 3 * 86400)
        self.assertEqual(request.call_count, 2)
        fribb.assert_called_once()
        self.assertNotIn("cached miss", output)

    def test_force_bypasses_a_recent_miss(self):
        self.run_lookup(self.now)
        _, request, _, output = self.run_lookup(self.now + 86400, force=True)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("cached miss", output)

    def test_dry_run_reuses_a_recent_miss(self):
        self.run_lookup(self.now, dry_run=True)
        _, request, _, output = self.run_lookup(self.now + 86400, dry_run=True)
        request.assert_not_called()
        self.assertIn("cached miss; lookup skipped", output)

    def test_successful_lookup_is_reused_the_next_day(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ids": {"tmdb": 123}}
        targets, request, _, _ = self.run_lookup(self.now, response=response)
        self.assertEqual([target.tmdb for target in targets], [123])
        self.assertEqual(request.call_count, 1)
        targets, request, fribb, _ = self.run_lookup(self.now + 86400)
        self.assertEqual([target.tmdb for target in targets], [123])
        request.assert_not_called()
        fribb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
