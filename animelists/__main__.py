"""Command line:
  python -m animelists run [jobs ...]      jobs: karma recent popular trending (default: all configured)
  python -m animelists schedule [jobs ...] preview airing-aware UTC schedule
  python -m animelists tick [jobs ...]     sync only due jobs, using a cached schedule
  python -m animelists kuroiru-vars        list the JSON variables embedded in kuroiru.co/app
"""
import argparse
import math
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import core, jobs, schedule
from .sources.kuroiru import Kuroiru


def _min_members(value: str) -> int:
    try:
        members = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("minimum members must be a non-negative integer")
    if members < 0:
        raise argparse.ArgumentTypeError("minimum members must be a non-negative integer")
    return members


def _min_score(value: str) -> float:
    try:
        score = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("minimum score must be between 0 and 10")
    if not math.isfinite(score) or not 0 <= score <= 10:
        raise argparse.ArgumentTypeError("minimum score must be between 0 and 10")
    return score


def _positive(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def _utc_time(value: str) -> str:
    try:
        hour, minute = map(int, value.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UTC time in HH:MM format")
    return f"{hour:02}:{minute:02}"


def _job_arguments(parser):
    parser.add_argument("jobs", nargs="*", metavar="job", help=f"any of: {' '.join(jobs.JOBS)}")
    parser.add_argument("--min-karma", type=int, default=int(os.getenv("MIN_KARMA", "200")))
    parser.add_argument("--window-days", type=float, default=float(os.getenv("WINDOW_DAYS", "7")))
    parser.add_argument("--hours", type=_positive, default=None,
                        help="override recent membership with a broadcast-age window; default: Kuroiru panel")
    parser.add_argument("--top", type=int, default=30, help="size of 'popular' and 'trending'")
    parser.add_argument("--min-members", type=_min_members,
                        default=os.getenv("MIN_MEMBERS", "").strip() or None,
                        help="minimum Kuroiru members (MIN_MEMBERS in .env; default: disabled)")
    parser.add_argument("--min-score", type=_min_score,
                        default=os.getenv("MIN_SCORE", "").strip() or None,
                        help="minimum Kuroiru score, 0-10 (MIN_SCORE in .env; default: disabled)")


def _schedule_arguments(parser):
    parser.add_argument("--delay-minutes", type=_positive, default=50,
                        help="wait after broadcast before checking recent (default: 50; respects provider entry time)")
    parser.add_argument("--batch-minutes", type=int, choices=(5, 10, 15, 20, 30, 60), default=30,
                        help="round recent checks up to a UTC interval (default: 30)")
    parser.add_argument("--refresh-hours", type=_positive, default=24,
                        help="refresh airing plan at most this often (default: 24)")
    for job, default in (("karma", 6), ("popular", 12)):
        parser.add_argument(f"--{job}-hours", type=int, choices=(1, 2, 3, 4, 6, 8, 12, 24),
                            default=default, help=f"ranking check interval (default: {default})")
    parser.add_argument("--retry-minutes", type=_positive, default=30,
                        help="backoff for failed jobs and plan refreshes (default: 30)")
    parser.add_argument("--trending-time", type=_utc_time, default="18:15",
                        help="daily trend snapshot in UTC (default: 18:15)")


def main(argv=None) -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env",
                override=False, encoding="utf-8-sig")
    parser = argparse.ArgumentParser(prog="animelists", description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="build the lists and sync them to PMDB / MDBList")
    _job_arguments(run)
    run.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    run.add_argument("--force", action="store_true", help="ignore the local cache this run")
    run.add_argument("--extract-only", action="store_true", help="print each job's items as JSON")
    preview = sub.add_parser("schedule", help="preview the airing-aware schedule; no backend access")
    _job_arguments(preview)
    _schedule_arguments(preview)
    preview.add_argument("--json", action="store_true", help="print the full plan as JSON")
    tick = sub.add_parser("tick", help="run only due jobs; cache the schedule between cron checks")
    _job_arguments(tick)
    _schedule_arguments(tick)
    tick.add_argument("--dry-run", action="store_true", help="report due jobs without syncing or saving state")
    tick.add_argument("--schedule-file", default=None, help="scheduler state (default: beside STATE_FILE)")
    sub.add_parser("kuroiru-vars", help="list JSON variables embedded in kuroiru.co/app")
    args = parser.parse_args(argv)

    if args.cmd == "kuroiru-vars":
        Kuroiru().print_vars()
        return 0

    bad = [j for j in args.jobs if j not in jobs.JOBS]
    if bad:
        parser.error(f"unknown job(s): {', '.join(bad)} (choose from {', '.join(jobs.JOBS)})")
    settings = core.Settings.from_env()
    settings.dry_run |= getattr(args, "dry_run", False)
    settings.force |= getattr(args, "force", False)
    opts = jobs.Options(min_karma=args.min_karma, window_days=args.window_days,
                        hours=args.hours, top=args.top, extract_only=getattr(args, "extract_only", False),
                        min_members=args.min_members, min_score=args.min_score)
    if args.cmd in ("schedule", "tick"):
        if args.refresh_hours > 168:
            parser.error("--refresh-hours must be at most 168 (plan horizon is 8 days)")
        policy = schedule.Policy(**{name: getattr(args, name)
                                    for name in schedule.Policy.__dataclass_fields__})
        if args.cmd == "schedule":
            names = schedule.configured_jobs(args.jobs)
            if not names:
                parser.error("no jobs configured: set list IDs in .env or specify jobs")
            now = schedule.time.time()
            plan = schedule.fetch_plan(names, opts, policy, now, Kuroiru())
            schedule.print_plan(plan, now, as_json=args.json)
            return 0
        state_file = args.schedule_file or str(Path(settings.state_file).with_name("schedule_state.json"))
        return schedule.tick(args.jobs, opts, settings, policy, state_file)
    return jobs.run(args.jobs, opts, settings)


if __name__ == "__main__":
    sys.exit(main())
