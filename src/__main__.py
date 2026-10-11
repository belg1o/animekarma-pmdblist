"""Command line:
  python -m animelists run [jobs ...]      jobs: karma recent popular trending (default: all configured)
  python -m animelists kuroiru-vars        list the JSON variables embedded in kuroiru.co/app
"""
import argparse
import os
import sys

from . import core, jobs
from .sources.kuroiru import Kuroiru


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="animelists", description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="build the lists and sync them to PMDB / MDBList")
    run.add_argument("jobs", nargs="*", metavar="job", help=f"any of: {' '.join(jobs.JOBS)}")
    run.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    run.add_argument("--force", action="store_true", help="ignore the local cache this run")
    run.add_argument("--extract-only", action="store_true", help="print each job's items as JSON")
    run.add_argument("--min-karma", type=int, default=int(os.getenv("MIN_KARMA", "200")))
    run.add_argument("--window-days", type=float, default=float(os.getenv("WINDOW_DAYS", "7")))
    run.add_argument("--hours", type=float, default=96, help="'recent' look-back window")
    run.add_argument("--top", type=int, default=30, help="size of 'popular' and 'trending'")
    sub.add_parser("kuroiru-vars", help="list JSON variables embedded in kuroiru.co/app")
    args = parser.parse_args(argv)

    if args.cmd == "kuroiru-vars":
        Kuroiru().print_vars()
        return 0

    bad = [j for j in args.jobs if j not in jobs.JOBS]
    if bad:
        parser.error(f"unknown job(s): {', '.join(bad)} (choose from {', '.join(jobs.JOBS)})")
    settings = core.Settings.from_env()
    settings.dry_run |= args.dry_run
    settings.force |= args.force
    opts = jobs.Options(min_karma=args.min_karma, window_days=args.window_days,
                        hours=args.hours, top=args.top, extract_only=args.extract_only)
    return jobs.run(args.jobs, opts, settings)


if __name__ == "__main__":
    sys.exit(main())
