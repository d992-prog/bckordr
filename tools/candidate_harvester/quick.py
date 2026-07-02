from __future__ import annotations

import sys
from pathlib import Path

from candidate_harvester import main

DEFAULT_LIMIT_OUTPUT = "20"
DEFAULT_MAX_RDAP_CHECKS = "1000"
DEFAULT_CONCURRENCY = "10"


def build_args(argv: list[str]) -> list[str]:
    if len(argv) < 2:
        raise SystemExit(
            "Usage: python quick.py <tld> <input-file-or-folder> [more-inputs...]\n"
            "Example: python quick.py com ./expired-com.txt"
        )

    tld = argv[0].lower().lstrip(".")
    inputs = argv[1:]
    output_prefix = f"candidates-{tld}"
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
    ]


if __name__ == "__main__":
    raise SystemExit(main(build_args(sys.argv[1:])))
