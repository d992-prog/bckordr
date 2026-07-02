from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

IANA_RDAP_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
DOMAIN_PATTERN = re.compile(r"(?i)(?:https?://)?(?:www\.)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z]{2,63})+)")
VOWELS = set("aeiou")


@dataclass(frozen=True)
class HarvesterResult:
    domain: str
    tld: str
    lifecycle: str
    status_codes: str
    http_status: int | None
    checked_at: str
    score: int
    reason: str
    error: str | None = None


def normalize_domain(line: str) -> str | None:
    match = DOMAIN_PATTERN.search(line.strip())
    if not match:
        return None
    domain = match.group(1).lower().rstrip(".")
    labels = domain.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return None
    return domain


def low_value_score(domain: str) -> int:
    label = domain.split(".", 1)[0].lower()
    if not label:
        return 0

    digits = sum(character.isdigit() for character in label)
    hyphens = label.count("-")
    letters = sum(character.isalpha() for character in label)
    vowels = sum(character in VOWELS for character in label)
    unique_ratio = len(set(label)) / max(len(label), 1)
    vowel_ratio = vowels / max(letters, 1)

    score = 0
    if len(label) >= 12:
        score += 25
    if len(label) >= 16:
        score += 10
    if digits:
        score += min(30, digits * 5)
    if hyphens:
        score += min(20, hyphens * 10)
    if vowel_ratio < 0.22 and letters >= 5:
        score += 15
    if unique_ratio > 0.65 and len(label) >= 10:
        score += 10
    if label.isdigit():
        score += 20

    if len(label) <= 5:
        score -= 35
    if digits == 0 and hyphens == 0 and 6 <= len(label) <= 12 and vowel_ratio >= 0.25:
        score -= 25

    return max(0, min(score, 100))


def should_consider_domain(domain: str, *, tld: str, min_score: int) -> bool:
    if domain.rsplit(".", 1)[-1] != tld.lower().lstrip("."):
        return False
    return low_value_score(domain) >= min_score


def classify_lifecycle(status_codes: list[str], http_status: int | None) -> str:
    normalized = {_normalize_status_code(item) for item in status_codes}
    if http_status == 404:
        return "not_found"
    if "pendingdelete" in normalized:
        return "pending_delete"
    if "redemptionperiod" in normalized:
        return "redemption"
    if status_codes:
        return "registered"
    return "unknown"


def resolve_rdap_domain_url(fqdn: str, bootstrap_payload: dict) -> str:
    zone = fqdn.rsplit(".", 1)[-1].lower()
    for service in bootstrap_payload.get("services", []):
        if not isinstance(service, list) or len(service) < 2:
            continue
        zones, urls = service[0], service[1]
        if zone not in {str(item).lower() for item in zones}:
            continue
        base_url = next((str(item) for item in urls if item), "")
        if base_url:
            return f"{base_url.rstrip('/')}/domain/{fqdn}"
    raise ValueError(f"No RDAP endpoint for .{zone}")


def iter_domains(inputs: list[Path]) -> Iterable[str]:
    for input_path in inputs:
        paths = sorted(input_path.rglob("*")) if input_path.is_dir() else [input_path]
        for path in paths:
            if not path.is_file():
                continue
            for line in _iter_lines(path):
                domain = normalize_domain(line)
                if domain:
                    yield domain


def fetch_bootstrap(url: str = IANA_RDAP_BOOTSTRAP_URL, timeout: float = 15.0) -> dict:
    request = Request(url, headers={"User-Agent": "drop-window-candidate-harvester/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_rdap(domain: str, bootstrap: dict, *, timeout: float = 8.0) -> HarvesterResult:
    checked_at = datetime.now(timezone.utc).isoformat()
    score = low_value_score(domain)
    reason = describe_score(domain)
    try:
        rdap_url = resolve_rdap_domain_url(domain, bootstrap)
        request = Request(rdap_url, headers={"User-Agent": "drop-window-candidate-harvester/1.0"})
        with urlopen(request, timeout=timeout) as response:
            http_status = response.status
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        status_codes = [str(item) for item in payload.get("status", []) if item]
        lifecycle = classify_lifecycle(status_codes, http_status)
        return HarvesterResult(
            domain=domain,
            tld=domain.rsplit(".", 1)[-1],
            lifecycle=lifecycle,
            status_codes=" ".join(status_codes),
            http_status=http_status,
            checked_at=checked_at,
            score=score,
            reason=reason,
        )
    except HTTPError as exc:
        lifecycle = classify_lifecycle([], exc.code)
        return HarvesterResult(
            domain=domain,
            tld=domain.rsplit(".", 1)[-1],
            lifecycle=lifecycle,
            status_codes="",
            http_status=exc.code,
            checked_at=checked_at,
            score=score,
            reason=reason,
            error=str(exc) if lifecycle != "not_found" else None,
        )
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return HarvesterResult(
            domain=domain,
            tld=domain.rsplit(".", 1)[-1],
            lifecycle="error",
            status_codes="",
            http_status=None,
            checked_at=checked_at,
            score=score,
            reason=reason,
            error=str(exc),
        )


def describe_score(domain: str) -> str:
    label = domain.split(".", 1)[0].lower()
    reasons: list[str] = []
    if len(label) >= 12:
        reasons.append("long")
    if any(character.isdigit() for character in label):
        reasons.append("numeric")
    if "-" in label:
        reasons.append("hyphen")
    letters = sum(character.isalpha() for character in label)
    vowels = sum(character in VOWELS for character in label)
    if letters >= 5 and vowels / max(letters, 1) < 0.22:
        reasons.append("low_vowel_ratio")
    return "+".join(reasons) or "low_priority"


def run(args: argparse.Namespace) -> int:
    bootstrap = fetch_bootstrap(args.bootstrap_url, timeout=args.bootstrap_timeout)
    inputs = [Path(item) for item in args.input]
    accepted_lifecycles = set(args.accept_lifecycle)
    submitted = 0
    written = 0
    futures = []
    started_at = time.monotonic()

    with open(args.output, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "domain",
                "tld",
                "lifecycle",
                "status_codes",
                "http_status",
                "checked_at",
                "score",
                "reason",
                "error",
            ],
        )
        writer.writeheader()

        txt_file = open(args.output_txt, "w", encoding="utf-8") if args.output_txt else None
        try:
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                for domain in iter_domains(inputs):
                    if submitted >= args.max_rdap_checks or written >= args.limit_output:
                        break
                    if not should_consider_domain(domain, tld=args.tld, min_score=args.min_score):
                        continue
                    futures.append(executor.submit(check_rdap, domain, bootstrap, timeout=args.rdap_timeout))
                    submitted += 1
                    if len(futures) >= args.concurrency * 4:
                        written += _drain_futures(
                            futures,
                            writer,
                            txt_file,
                            accepted_lifecycles=accepted_lifecycles,
                            remaining=args.limit_output - written,
                        )
                        futures = []

                if futures and written < args.limit_output:
                    written += _drain_futures(
                        futures,
                        writer,
                        txt_file,
                        accepted_lifecycles=accepted_lifecycles,
                        remaining=args.limit_output - written,
                    )
        finally:
            if txt_file:
                txt_file.close()

    elapsed = max(time.monotonic() - started_at, 0.001)
    print(f"submitted_rdap_checks={submitted}")
    print(f"written_candidates={written}")
    print(f"elapsed_seconds={elapsed:.2f}")
    print(f"output={args.output}")
    if args.output_txt:
        print(f"output_txt={args.output_txt}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find low-value pre-pending domain candidates for drop-window research.")
    parser.add_argument("--input", nargs="+", required=True, help="Input files or folders: txt/csv/gz/zip are supported.")
    parser.add_argument("--tld", required=True, help="Target TLD, for example com, net, org.")
    parser.add_argument("--output", required=True, help="CSV output path.")
    parser.add_argument("--output-txt", default=None, help="Optional TXT output with domains only.")
    parser.add_argument("--limit-output", type=int, default=50, help="Stop after this many accepted candidates.")
    parser.add_argument("--max-rdap-checks", type=int, default=5000, help="Maximum RDAP checks per run.")
    parser.add_argument("--concurrency", type=int, default=20, help="Parallel RDAP checks.")
    parser.add_argument("--min-score", type=int, default=60, help="Low-value score threshold from 0 to 100.")
    parser.add_argument("--accept-lifecycle", nargs="+", default=["redemption"], help="Lifecycle values to write.")
    parser.add_argument("--rdap-timeout", type=float, default=8.0, help="Per-domain RDAP timeout seconds.")
    parser.add_argument("--bootstrap-url", default=IANA_RDAP_BOOTSTRAP_URL, help="IANA RDAP bootstrap URL.")
    parser.add_argument("--bootstrap-timeout", type=float, default=15.0, help="RDAP bootstrap timeout seconds.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.tld = args.tld.lower().lstrip(".")
    args.concurrency = max(1, args.concurrency)
    args.limit_output = max(1, args.limit_output)
    args.max_rdap_checks = max(1, args.max_rdap_checks)
    return run(args)


def _iter_lines(path: Path) -> Iterable[str]:
    suffix = path.suffix.lower()
    if suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as handle:
            yield from handle
        return
    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                with archive.open(name) as member:
                    for raw_line in member:
                        yield raw_line.decode("utf-8", errors="ignore")
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        yield from handle


def _drain_futures(
    futures,
    writer: csv.DictWriter,
    txt_file,
    *,
    accepted_lifecycles: set[str],
    remaining: int,
) -> int:
    written = 0
    for future in as_completed(futures):
        if written >= remaining:
            break
        result = future.result()
        if result.lifecycle not in accepted_lifecycles:
            continue
        writer.writerow(result.__dict__)
        if txt_file:
            txt_file.write(f"{result.domain}\n")
        written += 1
    return written


def _normalize_status_code(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
