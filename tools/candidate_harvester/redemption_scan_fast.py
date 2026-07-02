from __future__ import annotations

import sys
from pathlib import Path

from candidate_harvester import main

DEFAULT_LIMIT_OUTPUT = "100"
DEFAULT_MAX_RDAP_CHECKS = "200000"
DEFAULT_CONCURRENCY = "100"
DEFAULT_MIN_SCORE = "35"
DEFAULT_RESERVOIR_SIZE = "200000"


def build_args(argv: list[str]) -> list[str]:
    if len(argv) < 2:
        raise SystemExit(
            "Usage: python redemption_scan_fast.py <tld> <zonefile-or-folder> [more-inputs...]\n"
            "Example: python redemption_scan_fast.py com ./com.2026-07-02.txt"
        )

    tld = argv[0].lower().lstrip(".")
    inputs = argv[1:]
    output_prefix = f"redemption-candidates-{tld}-fast"
    return [
        "--input",
        *inputs,
        "--tld",
        tld,
        "--output",
        str(Path(f"{output_prefix}.csv")),
        "--output-txt",
        str(Path(f"{output_prefix}.txt")),
        "--limit-output",
        DEFAULT_LIMIT_OUTPUT,
        "--max-rdap-checks",
        DEFAULT_MAX_RDAP_CHECKS,
        "--concurrency",
        DEFAULT_CONCURRENCY,
        "--min-score",
        DEFAULT_MIN_SCORE,
        "--sample-mode",
        "reservoir",
        "--reservoir-size",
        DEFAULT_RESERVOIR_SIZE,
        "--accept-lifecycle",
        "redemption",
    ]


if __name__ == "__main__":
    raise SystemExit(main(build_args(sys.argv[1:])))
