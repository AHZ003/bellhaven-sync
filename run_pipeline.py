#!/usr/bin/env python3
"""Daily entry point. Scrapes the website, reads the CRM, and queues proposals for review.

    python run_pipeline.py             # live website + CRM (needs CRM_API_TOKEN in .env)
    python run_pipeline.py --offline   # fixture data, for rehearsal (never touches the real CRM)

Exit code is non-zero if the run failed, so cron / CI can alert on it.
"""
import os
import sys

if "--offline" in sys.argv:          # must be set before config is imported
    os.environ["BELLHAVEN_OFFLINE"] = "1"

from bellhaven_sync import config
from bellhaven_sync.matcher import KIND_LABELS
from bellhaven_sync.pipeline import run_pipeline


def main() -> int:
    try:
        s = run_pipeline()
    except Exception as exc:
        print("PIPELINE FAILED: {}".format(exc), file=sys.stderr)
        return 1
    print("Run #{} ok{} - {} website locations, {} CRM accounts".format(
        s["run_id"], " [OFFLINE]" if config.OFFLINE else "", s["locations"], s["accounts"]))
    print("  new proposals: {new_proposals} | still pending: {still_pending} | "
          "already decided (not re-proposed): {skipped_decided} | went stale: {went_stale}".format(**s))
    by_kind = {}
    for p in s["proposals"]:
        by_kind[p["kind"]] = by_kind.get(p["kind"], 0) + 1
    for kind, n in by_kind.items():
        print("    {:>2} x {}".format(n, KIND_LABELS[kind]))
    for w in s["warnings"]:
        print("  warning: " + w)
    print("Review at http://127.0.0.1:5050  (python review_app.py{})".format(" --offline" if config.OFFLINE else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
