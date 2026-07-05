from __future__ import annotations

import asyncio
import gzip
import json
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from time import monotonic

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import DiscoveryDomain, ZoneScanCandidate, ZoneScanJob
from app.services.app_settings import get_app_setting, set_app_setting
from app.services.discovery import (
    IANA_RDAP_BOOTSTRAP_URL,
    REDEMPTION_DURATION,
    check_discovery_domain_rdap,
    fetch_rdap_bootstrap,
    infer_zone,
)

ALLZONEFILES_TOKEN_KEY = "allzonefiles_api_token"
DOMAIN_PATTERN = re.compile(r"(?i)(?:https?://)?(?:www\.)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z]{2,63})+)")
VOWELS = set("aeiou")
JOB_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ZoneScanConfig:
    zone: str
    min_score: int
    limit_output: int
    max_rdap_checks: int
    concurrency: int
    rdap_timeout_seconds: float
    pending_delete_min_days: float | None
    pending_delete_max_days: float | None
    reservoir_size: int
    random_seed: int
    keep_file: bool


@dataclass(frozen=True)
class CandidateResult:
    fqdn: str
    zone: str
    lifecycle_stage: str
    status_codes: str | None
    http_status: int | None
    checked_at: datetime
    redemption_anchor_at: datetime | None
    predicted_pending_delete_at: datetime | None
    days_to_pending_delete: float | None
    score: int
    reason: str
    error: str | None


async def get_allzonefiles_token(session: AsyncSession) -> str | None:
    return await get_app_setting(session, ALLZONEFILES_TOKEN_KEY)


async def set_allzonefiles_token(session: AsyncSession, token: str | None) -> None:
    normalized = token.strip() if token else None
    await set_app_setting(session, ALLZONEFILES_TOKEN_KEY, normalized or None)


async def test_allzonefiles_connection(*, token: str, base_url: str) -> tuple[bool, str, int | None]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/zones",
                headers=build_allzonefiles_headers(token),
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return False, str(exc), None
    zones = payload.get("zones", [])
    zones_count = len(zones) if isinstance(zones, list) else None
    return True, "AllZonefiles API connection OK", zones_count


def build_allzonefiles_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "User-Agent": DEFAULT_HTTP_USER_AGENT}


def build_allzonefiles_download_url(*, base_url: str, source_type: str, zone: str, source_date: date | None) -> str:
    root = base_url.rstrip("/")
    zone = zone.lower().lstrip(".")
    if source_type == "zone_latest":
        return f"{root}/zones/{zone}/dl"
    if source_type == "zone_historic":
        if source_date is None:
            raise ValueError("source_date is required for historic zonefile")
        return f"{root}/zones/{zone}/dl/{source_date.isoformat()}"
    if source_type == "expired_latest":
        return f"{root}/expired/dl"
    if source_type == "expired_historic":
        if source_date is None:
            raise ValueError("source_date is required for historic expired list")
        return f"{root}/expired/dl/{source_date.isoformat()}"
    raise ValueError(f"Unsupported source_type: {source_type}")


def build_zone_scan_filename(*, source_type: str, zone: str, source_date: date | None) -> str:
    zone = zone.lower().lstrip(".")
    suffix = source_date.isoformat() if source_date else "latest"
    return f"{source_type}-{zone}-{suffix}.txt.gz"


async def run_zone_scan_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    settings: Settings,
) -> None:
    try:
        async with session_factory() as session:
            job = await session.get(ZoneScanJob, job_id)
            if job is None or job.status in JOB_TERMINAL_STATUSES:
                return
            token = await get_allzonefiles_token(session)
            if not token:
                await mark_zone_scan_job_failed(session, job, "AllZonefiles API token is not configured")
                return
            config = zone_scan_config_from_job(job)
            download_url = build_allzonefiles_download_url(
                base_url=settings.allzonefiles_base_url,
                source_type=job.source_type,
                zone=job.zone,
                source_date=job.source_date,
            )
            storage_dir = Path(settings.zone_scan_storage_dir)
            storage_dir.mkdir(parents=True, exist_ok=True)
            file_name = build_zone_scan_filename(source_type=job.source_type, zone=job.zone, source_date=job.source_date)
            file_path = storage_dir / file_name
            job.status = "downloading"
            job.started_at = job.started_at or utcnow()
            job.download_url = download_url
            job.file_name = file_name
            job.file_path = str(file_path)
            job.last_error = None
            await session.commit()

        await download_zone_scan_file(
            session_factory,
            job_id=job_id,
            token=token,
            url=download_url,
            file_path=file_path,
        )
        if await zone_scan_job_is_cancelled(session_factory, job_id):
            await cleanup_zone_scan_file(session_factory, job_id=job_id, force=not config.keep_file)
            return
        await mark_zone_scan_job_scanning(session_factory, job_id)
        reservoir = await collect_reservoir_candidates(session_factory, job_id=job_id, file_path=file_path, config=config)
        if await zone_scan_job_is_cancelled(session_factory, job_id):
            await cleanup_zone_scan_file(session_factory, job_id=job_id, force=not config.keep_file)
            return
        await scan_rdap_candidates(session_factory, job_id=job_id, domains=reservoir, config=config)
        await mark_zone_scan_job_completed(session_factory, job_id)
        await cleanup_zone_scan_file(session_factory, job_id=job_id, force=not config.keep_file)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await fail_zone_scan_job_by_id(session_factory, job_id=job_id, message=str(exc))


def zone_scan_config_from_job(job: ZoneScanJob) -> ZoneScanConfig:
    return ZoneScanConfig(
        zone=job.zone.lower().lstrip("."),
        min_score=max(int(job.min_score), 0),
        limit_output=max(int(job.limit_output), 1),
        max_rdap_checks=max(int(job.max_rdap_checks), 1),
        concurrency=max(int(job.concurrency), 1),
        rdap_timeout_seconds=max(float(job.rdap_timeout_seconds), 1.0),
        pending_delete_min_days=job.pending_delete_min_days,
        pending_delete_max_days=job.pending_delete_max_days,
        reservoir_size=max(int(job.reservoir_size), 1),
        random_seed=max(int(job.random_seed), 0),
        keep_file=bool(job.keep_file),
    )


async def download_zone_scan_file(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    token: str,
    url: str,
    file_path: Path,
) -> None:
    downloaded = 0
    last_update = monotonic()
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=build_allzonefiles_headers(token)) as response:
            response.raise_for_status()
            total_header = response.headers.get("content-length")
            await update_zone_scan_job(
                session_factory,
                job_id,
                file_size_bytes=int(total_header) if total_header and total_header.isdigit() else None,
            )
            with file_path.open("wb") as handle:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    now = monotonic()
                    if now - last_update >= 2.0:
                        if await zone_scan_job_is_cancelled(session_factory, job_id):
                            return
                        await update_zone_scan_job(session_factory, job_id, downloaded_bytes=downloaded)
                        last_update = now
    await update_zone_scan_job(session_factory, job_id, downloaded_bytes=downloaded)


async def collect_reservoir_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    file_path: Path,
    config: ZoneScanConfig,
) -> list[str]:
    rng = random.Random(config.random_seed)
    sample: list[str] = []
    filtered_seen = 0
    scanned_lines = 0
    parsed_domains = 0
    filtered_candidates = 0
    last_update = monotonic()

    with open_zone_scan_text(file_path) as handle:
        for raw_line in handle:
            scanned_lines += 1
            domain = normalize_scan_domain(raw_line)
            if domain is not None:
                parsed_domains += 1
                if should_consider_scan_domain(domain, tld=config.zone, min_score=config.min_score):
                    filtered_candidates += 1
                    filtered_seen += 1
                    if len(sample) < config.reservoir_size:
                        sample.append(domain)
                    else:
                        index = rng.randrange(filtered_seen)
                        if index < config.reservoir_size:
                            sample[index] = domain
            if scanned_lines % 10000 == 0:
                await asyncio.sleep(0)
                now = monotonic()
                if now - last_update >= 2.0:
                    await update_zone_scan_job(
                        session_factory,
                        job_id,
                        scanned_lines=scanned_lines,
                        parsed_domains=parsed_domains,
                        filtered_candidates=filtered_candidates,
                    )
                    if await zone_scan_job_is_cancelled(session_factory, job_id):
                        return sample
                    last_update = now
    await update_zone_scan_job(
        session_factory,
        job_id,
        scanned_lines=scanned_lines,
        parsed_domains=parsed_domains,
        filtered_candidates=filtered_candidates,
    )
    return sample[: config.max_rdap_checks]


def open_zone_scan_text(file_path: Path):
    if file_path.suffix.lower() == ".gz":
        return gzip.open(file_path, "rt", encoding="utf-8", errors="ignore")
    return file_path.open("r", encoding="utf-8", errors="ignore")


async def scan_rdap_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    domains: list[str],
    config: ZoneScanConfig,
) -> None:
    submitted = 0
    completed = 0
    found = 0
    errors = 0
    pending: set[asyncio.Task[CandidateResult]] = set()
    domain_iter = iter(domains[: config.max_rdap_checks])
    source_exhausted = False
    last_update = monotonic()
    async with httpx.AsyncClient(timeout=config.rdap_timeout_seconds) as client:
        bootstrap = await fetch_rdap_bootstrap(client, IANA_RDAP_BOOTSTRAP_URL)

        def submit_next() -> bool:
            nonlocal submitted, source_exhausted
            if source_exhausted or submitted >= config.max_rdap_checks:
                return False
            try:
                domain = next(domain_iter)
            except StopIteration:
                source_exhausted = True
                return False
            pending.add(asyncio.create_task(check_zone_scan_candidate(domain, client=client, bootstrap=bootstrap)))
            submitted += 1
            return True

        while len(pending) < config.concurrency and submit_next():
            pass

        while pending:
            done, pending = await asyncio.wait(pending, timeout=2.0, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                await update_zone_scan_job(
                    session_factory,
                    job_id,
                    submitted_rdap=submitted,
                    completed_rdap=completed,
                    found_candidates=found,
                    error_count=errors,
                )
                continue
            for task in done:
                completed += 1
                result = task.result()
                if result.error:
                    errors += 1
                if result_is_zone_scan_candidate(result, config):
                    saved = await save_zone_scan_candidate(session_factory, job_id=job_id, result=result)
                    if saved:
                        found += 1
                if monotonic() - last_update >= 2.0:
                    await update_zone_scan_job(
                        session_factory,
                        job_id,
                        submitted_rdap=submitted,
                        completed_rdap=completed,
                        found_candidates=found,
                        error_count=errors,
                    )
                    if await zone_scan_job_is_cancelled(session_factory, job_id):
                        for pending_task in pending:
                            pending_task.cancel()
                        return
                    last_update = monotonic()
            while found < config.limit_output and len(pending) < config.concurrency and submit_next():
                pass
            if found >= config.limit_output:
                for pending_task in pending:
                    pending_task.cancel()
                pending.clear()
        await update_zone_scan_job(
            session_factory,
            job_id,
            submitted_rdap=submitted,
            completed_rdap=completed,
            found_candidates=found,
            error_count=errors,
        )


async def check_zone_scan_candidate(domain: str, *, client: httpx.AsyncClient, bootstrap: dict) -> CandidateResult:
    score = low_value_score(domain)
    reason = describe_score(domain)
    checked_at = datetime.now(timezone.utc)
    transient = DiscoveryDomain(fqdn=domain, zone=infer_zone(domain))
    observation = await check_discovery_domain_rdap(
        transient,
        client=client,
        bootstrap_payload=bootstrap,
        timeout_seconds=client.timeout.connect or 5.0,
    )
    redemption_anchor_at = observation.rdap_updated_at
    predicted_pending_delete_at = (
        redemption_anchor_at + REDEMPTION_DURATION
        if observation.lifecycle_stage == "redemption" and redemption_anchor_at is not None
        else None
    )
    days_to_pending_delete = (
        (predicted_pending_delete_at - checked_at).total_seconds() / 86400
        if predicted_pending_delete_at is not None
        else None
    )
    return CandidateResult(
        fqdn=domain,
        zone=infer_zone(domain),
        lifecycle_stage=observation.lifecycle_stage or "unknown",
        status_codes=json.dumps(observation.status_codes, ensure_ascii=True) if observation.status_codes else None,
        http_status=observation.http_status,
        checked_at=checked_at,
        redemption_anchor_at=redemption_anchor_at,
        predicted_pending_delete_at=predicted_pending_delete_at,
        days_to_pending_delete=round(days_to_pending_delete, 4) if days_to_pending_delete is not None else None,
        score=score,
        reason=reason,
        error=observation.error,
    )


def result_is_zone_scan_candidate(result: CandidateResult, config: ZoneScanConfig) -> bool:
    if result.lifecycle_stage != "redemption":
        return False
    if result.days_to_pending_delete is None:
        return False
    if config.pending_delete_min_days is not None and result.days_to_pending_delete < config.pending_delete_min_days:
        return False
    if config.pending_delete_max_days is not None and result.days_to_pending_delete > config.pending_delete_max_days:
        return False
    return True


async def save_zone_scan_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    result: CandidateResult,
) -> bool:
    async with session_factory() as session:
        candidate = ZoneScanCandidate(
            job_id=job_id,
            fqdn=result.fqdn,
            zone=result.zone,
            lifecycle_stage=result.lifecycle_stage,
            status_codes=result.status_codes,
            http_status=result.http_status,
            checked_at=result.checked_at,
            redemption_anchor_at=result.redemption_anchor_at,
            predicted_pending_delete_at=result.predicted_pending_delete_at,
            days_to_pending_delete=result.days_to_pending_delete,
            score=result.score,
            reason=result.reason,
            error=result.error,
        )
        session.add(candidate)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
        return True


async def add_zone_scan_candidate_to_discovery(
    session: AsyncSession,
    *,
    candidate_id: int,
    check_interval_seconds: int = 21600,
) -> DiscoveryDomain:
    candidate = await session.get(ZoneScanCandidate, candidate_id)
    if candidate is None:
        raise ValueError("Candidate not found")
    existing = await session.scalar(select(DiscoveryDomain).where(DiscoveryDomain.fqdn == candidate.fqdn))
    if existing is None:
        existing = DiscoveryDomain(
            fqdn=candidate.fqdn,
            zone=candidate.zone,
            check_interval_seconds=check_interval_seconds,
            source_mode="zone_scan",
            notes=f"Imported from zone scan job #{candidate.job_id}",
            next_check_at=utcnow(),
        )
        session.add(existing)
        await session.flush()
    candidate.discovery_domain_id = existing.id
    await session.flush()
    return existing


async def ignore_zone_scan_candidate(session: AsyncSession, *, candidate_id: int) -> ZoneScanCandidate:
    candidate = await session.get(ZoneScanCandidate, candidate_id)
    if candidate is None:
        raise ValueError("Candidate not found")
    candidate.is_ignored = True
    await session.flush()
    return candidate


async def cleanup_zone_scan_file(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    force: bool,
) -> bool:
    async with session_factory() as session:
        job = await session.get(ZoneScanJob, job_id)
        if job is None or not job.file_path:
            return False
        if not force and job.keep_file:
            return False
        path = Path(job.file_path)
        deleted = False
        if path.exists() and path.is_file():
            path.unlink()
            deleted = True
        if deleted:
            job.file_path = None
            job.file_name = None
            await session.commit()
        return deleted


async def update_zone_scan_job(session_factory: async_sessionmaker[AsyncSession], job_id: int, **values) -> None:
    async with session_factory() as session:
        job = await session.get(ZoneScanJob, job_id)
        if job is None:
            return
        for key, value in values.items():
            setattr(job, key, value)
        job.updated_at = utcnow()
        await session.commit()


async def mark_zone_scan_job_scanning(session_factory: async_sessionmaker[AsyncSession], job_id: int) -> None:
    await update_zone_scan_job(session_factory, job_id, status="scanning", updated_at=utcnow())


async def mark_zone_scan_job_completed(session_factory: async_sessionmaker[AsyncSession], job_id: int) -> None:
    await update_zone_scan_job(session_factory, job_id, status="completed", finished_at=utcnow(), updated_at=utcnow())


async def mark_zone_scan_job_failed(session: AsyncSession, job: ZoneScanJob, message: str) -> None:
    job.status = "failed"
    job.last_error = message
    job.finished_at = utcnow()
    await session.commit()


async def fail_zone_scan_job_by_id(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    message: str,
) -> None:
    async with session_factory() as session:
        job = await session.get(ZoneScanJob, job_id)
        if job is None:
            return
        await mark_zone_scan_job_failed(session, job, message)


async def zone_scan_job_is_cancelled(session_factory: async_sessionmaker[AsyncSession], job_id: int) -> bool:
    async with session_factory() as session:
        status = await session.scalar(select(ZoneScanJob.status).where(ZoneScanJob.id == job_id))
    return status == "cancelled"


def normalize_scan_domain(line: str) -> str | None:
    match = DOMAIN_PATTERN.search(line.strip())
    if not match:
        return None
    domain = match.group(1).lower().rstrip(".")
    labels = domain.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return None
    return domain


def should_consider_scan_domain(domain: str, *, tld: str, min_score: int) -> bool:
    return domain.rsplit(".", 1)[-1] == tld.lower().lstrip(".") and low_value_score(domain) >= min_score


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
