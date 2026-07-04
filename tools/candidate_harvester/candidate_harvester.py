from __future__ import annotations

import argparse
import collections
import csv
import gzip
import json
import random
import re
import sys
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    redemption_anchor_at: str | None
    predicted_pending_delete_at: str | None
    days_to_pending_delete: float | None
    score: int
    reason: str
    error: str | None = None


@dataclass
class ProgressStats:
    scanned_lines: int = 0
    parsed_domains: int = 0
    filtered_candidates: int = 0
    submitted_rdap: int = 0
    completed_rdap: int = 0
    written_candidates: int = 0
    written_redemption_debug: int = 0
    started_at: float = 0.0
    last_log_at: float = 0.0


RESULT_FIELDNAMES = [
    "domain",
    "tld",
    "lifecycle",
    "status_codes",
    "http_status",
    "checked_at",
    "redemption_anchor_at",
    "predicted_pending_delete_at",
    "days_to_pending_delete",
    "score",
    "reason",
    "error",
]


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


def iter_domains(inputs: list[Path], stats: ProgressStats | None = None) -> Iterable[str]:
    for input_path in inputs:
        paths = sorted(input_path.rglob("*")) if input_path.is_dir() else [input_path]
        for path in paths:
            if not path.is_file():
                continue
            for line in _iter_lines(path):
                if stats:
                    stats.scanned_lines += 1
                domain = normalize_domain(line)
                if domain:
                    if stats:
                        stats.parsed_domains += 1
                    yield domain


def fetch_bootstrap(url: str = IANA_RDAP_BOOTSTRAP_URL, timeout: float = 15.0) -> dict:
    request = Request(url, headers={"User-Agent": "drop-window-candidate-harvester/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_rdap(domain: str, bootstrap: dict, *, timeout: float = 8.0) -> HarvesterResult:
    checked_at_dt = datetime.now(timezone.utc)
    checked_at = checked_at_dt.isoformat()
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
        redemption_anchor_at = extract_rdap_updated_at(payload)
        predicted_pending_delete_at = (
            redemption_anchor_at + timedelta(days=30)
            if lifecycle == "redemption" and redemption_anchor_at is not None
            else None
        )
        days_to_pending_delete = (
            (predicted_pending_delete_at - checked_at_dt).total_seconds() / 86400
            if predicted_pending_delete_at is not None
            else None
        )
        return HarvesterResult(
            domain=domain,
            tld=domain.rsplit(".", 1)[-1],
            lifecycle=lifecycle,
            status_codes=" ".join(status_codes),
            http_status=http_status,
            checked_at=checked_at,
            redemption_anchor_at=redemption_anchor_at.isoformat() if redemption_anchor_at else None,
            predicted_pending_delete_at=predicted_pending_delete_at.isoformat() if predicted_pending_delete_at else None,
            days_to_pending_delete=round(days_to_pending_delete, 4) if days_to_pending_delete is not None else None,
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
            redemption_anchor_at=None,
            predicted_pending_delete_at=None,
            days_to_pending_delete=None,
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
            redemption_anchor_at=None,
            predicted_pending_delete_at=None,
            days_to_pending_delete=None,
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
    started_at = time.monotonic()
    stats = ProgressStats(started_at=started_at, last_log_at=started_at)
    lifecycle_counts: collections.Counter[str] = collections.Counter()

    with open(args.output, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=RESULT_FIELDNAMES)
        writer.writeheader()

        txt_file = open(args.output_txt, "w", encoding="utf-8") if args.output_txt else None
        debug_file = open(args.redemption_debug_output, "w", newline="", encoding="utf-8") if args.redemption_debug_output else None
        debug_writer = csv.DictWriter(debug_file, fieldnames=RESULT_FIELDNAMES) if debug_file else None
        if debug_writer:
            debug_writer.writeheader()
        try:
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                if args.sample_mode == "reservoir":
                    domain_source = collect_reservoir_candidates(inputs, args, stats)
                else:
                    domain_source = _iter_first_filtered_candidates(inputs, args, stats)

                submitted, written = _run_rdap_pool(
                    domain_source,
                    executor,
                    bootstrap,
                    writer,
                    txt_file,
                    stats,
                    lifecycle_counts,
                    args,
                    accepted_lifecycles=accepted_lifecycles,
                    debug_writer=debug_writer,
                )
        finally:
            if txt_file:
                txt_file.close()
            if debug_file:
                debug_file.close()

    elapsed = max(time.monotonic() - started_at, 0.001)
    _log_progress(stats, args, force=True)
    print(f"submitted_rdap_checks={submitted}")
    print(f"written_candidates={written}")
    print(f"elapsed_seconds={elapsed:.2f}")
    print(f"output={args.output}")
    if args.output_txt:
        print(f"output_txt={args.output_txt}")
    if args.redemption_debug_output:
        print(f"redemption_debug_output={args.redemption_debug_output}")
        print(f"written_redemption_debug={stats.written_redemption_debug}")
    print(build_diagnosis(stats, lifecycle_counts=dict(lifecycle_counts), accepted_lifecycles=accepted_lifecycles))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find low-value pre-pending domain candidates for drop-window research.")
    parser.add_argument("--input", nargs="+", required=True, help="Input files or folders: txt/csv/gz/zip are supported.")
    parser.add_argument("--tld", required=True, help="Target TLD, for example com, net, org.")
    parser.add_argument("--output", required=True, help="CSV output path.")
    parser.add_argument("--output-txt", default=None, help="Optional TXT output with domains only.")
    parser.add_argument(
        "--redemption-debug-output",
        default=None,
        help="Optional CSV with every redemption RDAP hit, even when it misses the pendingDelete date window.",
    )
    parser.add_argument(
        "--redemption-debug-limit",
        type=int,
        default=1000,
        help="Maximum redemption rows to write to --redemption-debug-output.",
    )
    parser.add_argument("--limit-output", type=int, default=50, help="Stop after this many accepted candidates.")
    parser.add_argument("--max-rdap-checks", type=int, default=5000, help="Maximum RDAP checks per run.")
    parser.add_argument("--concurrency", type=int, default=20, help="Parallel RDAP checks.")
    parser.add_argument("--min-score", type=int, default=60, help="Low-value score threshold from 0 to 100.")
    parser.add_argument("--accept-lifecycle", nargs="+", default=["redemption"], help="Lifecycle values to write.")
    parser.add_argument(
        "--pending-delete-min-days",
        type=float,
        default=None,
        help="Only write redemption candidates whose predicted pendingDelete is at least this many days away.",
    )
    parser.add_argument(
        "--pending-delete-max-days",
        type=float,
        default=None,
        help="Only write redemption candidates whose predicted pendingDelete is at most this many days away.",
    )
    parser.add_argument("--rdap-timeout", type=float, default=8.0, help="Per-domain RDAP timeout seconds.")
    parser.add_argument("--bootstrap-url", default=IANA_RDAP_BOOTSTRAP_URL, help="IANA RDAP bootstrap URL.")
    parser.add_argument("--bootstrap-timeout", type=float, default=15.0, help="RDAP bootstrap timeout seconds.")
    parser.add_argument("--progress-interval", type=float, default=5.0, help="Progress log interval seconds.")
    parser.add_argument(
        "--sample-mode",
        choices=["first", "reservoir"],
        default="first",
        help="first checks early filtered domains; reservoir samples across the whole file before RDAP checks.",
    )
    parser.add_argument("--reservoir-size", type=int, default=50000, help="Reservoir sample size for --sample-mode reservoir.")
    parser.add_argument("--random-seed", type=int, default=42, help="Stable random seed for reservoir sampling.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.tld = args.tld.lower().lstrip(".")
    args.concurrency = max(1, args.concurrency)
    args.limit_output = max(1, args.limit_output)
    args.max_rdap_checks = max(1, args.max_rdap_checks)
    args.reservoir_size = max(1, args.reservoir_size)
    args.redemption_debug_limit = max(1, args.redemption_debug_limit)
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


def _run_rdap_pool(
    domain_source: Iterable[str],
    executor: ThreadPoolExecutor,
    bootstrap: dict,
    writer: csv.DictWriter,
    txt_file,
    stats: ProgressStats,
    lifecycle_counts: collections.Counter[str],
    args: argparse.Namespace,
    *,
    accepted_lifecycles: set[str],
    debug_writer: csv.DictWriter | None,
):
    submitted = 0
    written = 0
    pending = set()
    domains = iter(domain_source)
    source_exhausted = False
    target_pending = max(1, args.concurrency)

    def submit_next() -> bool:
        nonlocal source_exhausted, submitted
        if source_exhausted or submitted >= args.max_rdap_checks:
            return False
        try:
            domain = next(domains)
        except StopIteration:
            source_exhausted = True
            return False
        pending.add(executor.submit(check_rdap, domain, bootstrap, timeout=args.rdap_timeout))
        submitted += 1
        stats.submitted_rdap = submitted
        return True

    while len(pending) < target_pending and submit_next():
        pass

    while pending:
        done, pending = wait(pending, timeout=args.progress_interval, return_when=FIRST_COMPLETED)
        if not done:
            _log_progress(stats, args, force=True)
            continue
        written += _process_completed_futures(
            done,
            writer,
            txt_file,
            stats,
            lifecycle_counts,
            args,
            accepted_lifecycles=accepted_lifecycles,
            debug_writer=debug_writer,
            remaining=args.limit_output - written,
        )
        stats.written_candidates = written
        while written < args.limit_output and len(pending) < target_pending and submit_next():
            pass
        _log_progress(stats, args, force=False)
    return submitted, written


def _process_completed_futures(
    futures,
    writer: csv.DictWriter,
    txt_file,
    stats: ProgressStats,
    lifecycle_counts: collections.Counter[str],
    args: argparse.Namespace,
    *,
    accepted_lifecycles: set[str],
    debug_writer: csv.DictWriter | None,
    remaining: int,
) -> int:
    written = 0
    for future in futures:
        stats.completed_rdap += 1
        result = future.result()
        lifecycle_counts[result.lifecycle] += 1
        _write_redemption_debug(result, debug_writer, stats, args)
        if written >= remaining or not result_is_accepted(result, args, accepted_lifecycles=accepted_lifecycles):
            continue
        writer.writerow(result.__dict__)
        if txt_file:
            txt_file.write(f"{result.domain}\n")
        written += 1
    return written


def _write_redemption_debug(
    result: HarvesterResult,
    debug_writer: csv.DictWriter | None,
    stats: ProgressStats,
    args: argparse.Namespace,
) -> None:
    if debug_writer is None:
        return
    if result.lifecycle != "redemption":
        return
    if stats.written_redemption_debug >= args.redemption_debug_limit:
        return
    debug_writer.writerow(result.__dict__)
    stats.written_redemption_debug += 1


def result_is_accepted(
    result: HarvesterResult,
    args: argparse.Namespace,
    *,
    accepted_lifecycles: set[str],
) -> bool:
    if result.lifecycle not in accepted_lifecycles:
        return False
    min_days = getattr(args, "pending_delete_min_days", None)
    max_days = getattr(args, "pending_delete_max_days", None)
    if min_days is None and max_days is None:
        return True
    if result.lifecycle != "redemption":
        return True
    if result.days_to_pending_delete is None:
        return False
    if min_days is not None and result.days_to_pending_delete < float(min_days):
        return False
    if max_days is not None and result.days_to_pending_delete > float(max_days):
        return False
    return True


def collect_reservoir_candidates(
    inputs: list[Path],
    args: argparse.Namespace,
    stats: ProgressStats,
) -> list[str]:
    rng = random.Random(args.random_seed)
    sample: list[str] = []
    filtered_seen = 0
    for domain in iter_domains(inputs, stats):
        _log_progress(stats, args, force=False)
        if not should_consider_domain(domain, tld=args.tld, min_score=args.min_score):
            continue
        stats.filtered_candidates += 1
        filtered_seen += 1
        if len(sample) < args.reservoir_size:
            sample.append(domain)
            continue
        index = rng.randrange(filtered_seen)
        if index < args.reservoir_size:
            sample[index] = domain
    _log_progress(stats, args, force=True)
    return sample


def _iter_first_filtered_candidates(
    inputs: list[Path],
    args: argparse.Namespace,
    stats: ProgressStats,
) -> Iterable[str]:
    for domain in iter_domains(inputs, stats):
        _log_progress(stats, args, force=False)
        if not should_consider_domain(domain, tld=args.tld, min_score=args.min_score):
            continue
        stats.filtered_candidates += 1
        yield domain


def build_diagnosis(
    stats: ProgressStats,
    *,
    lifecycle_counts: dict[str, int],
    accepted_lifecycles: set[str],
) -> str:
    parts = [
        "diagnosis:",
        f"lifecycles={_format_counts(lifecycle_counts)}",
    ]
    if stats.parsed_domains == 0:
        parts.append("No valid domains were parsed. Check file format/path.")
    elif stats.filtered_candidates == 0:
        parts.append("No domains passed the low-value filter. Lower --min-score or check TLD.")
    elif stats.submitted_rdap == 0:
        parts.append("No RDAP checks were submitted. Check --max-rdap-checks and filter settings.")
    elif stats.completed_rdap == 0:
        parts.append("RDAP checks were submitted but did not complete. Check network/timeouts.")
    elif stats.written_candidates == 0:
        seen_accepted = any(lifecycle_counts.get(item, 0) for item in accepted_lifecycles)
        if not seen_accepted:
            parts.append(
                "RDAP checks worked, but accepted lifecycle was not seen. "
                "For a full zonefile this usually means checked domains are still registered; "
                "try an expired list or temporarily add --accept-lifecycle registered pending_delete redemption for debugging."
            )
        elif getattr(stats, "written_candidates", 0) == 0:
            parts.append(
                "Accepted lifecycle was seen, but no candidate matched the pendingDelete date window. "
                "Widen --pending-delete-min-days/--pending-delete-max-days or increase --max-rdap-checks."
            )
        else:
            parts.append("Accepted lifecycle was seen, but output limit/write path should be checked.")
    else:
        parts.append("Candidates were found. Paste the TXT output into control discovery.")
    return "\n".join(parts)


def _log_progress(stats: ProgressStats, args: argparse.Namespace, *, force: bool) -> None:
    now = time.monotonic()
    if not force and now - stats.last_log_at < args.progress_interval:
        return
    stats.last_log_at = now
    elapsed = max(now - stats.started_at, 0.001)
    lines_per_second = stats.scanned_lines / elapsed
    rdap_per_second = stats.completed_rdap / elapsed
    print(
        "progress "
        f"lines={stats.scanned_lines} "
        f"parsed={stats.parsed_domains} "
        f"filtered={stats.filtered_candidates} "
        f"rdap={stats.completed_rdap}/{stats.submitted_rdap} "
        f"written={stats.written_candidates}/{args.limit_output} "
        f"redemption_debug={stats.written_redemption_debug} "
        f"speed_lines_s={lines_per_second:.0f} "
        f"speed_rdap_s={rdap_per_second:.1f}",
        flush=True,
    )


def _normalize_status_code(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def extract_rdap_updated_at(payload: dict) -> datetime | None:
    events = payload.get("events", [])
    if not isinstance(events, list):
        return None
    preferred_actions = {"last changed", "last update", "last update of rdap database"}
    for event in events:
        if not isinstance(event, dict):
            continue
        action = str(event.get("eventAction", "")).strip().lower()
        if action not in preferred_actions:
            continue
        event_date = event.get("eventDate")
        if not isinstance(event_date, str):
            continue
        parsed = _parse_rdap_datetime(event_date)
        if parsed is not None:
            return parsed
    return None


def _parse_rdap_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
