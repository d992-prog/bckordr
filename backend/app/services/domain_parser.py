from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from fastapi import UploadFile
from openpyxl import load_workbook

DOMAIN_PATTERN = re.compile(
    r"(?<![a-z0-9-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+fr(?![a-z0-9-])",
    re.IGNORECASE,
)
ALLOWED_EXTENSIONS = {".txt", ".csv", ".xlsx"}


def normalize_domain(value: str) -> str | None:
    candidate = value.strip().lower()
    if not candidate:
        return None
    candidate = re.sub(r"^https?://", "", candidate)
    candidate = candidate.split("/")[0]
    candidate = candidate.strip(".")
    match = DOMAIN_PATTERN.fullmatch(candidate)
    return candidate if match else None


def extract_domains_from_text(content: str) -> list[str]:
    found: set[str] = set()
    for match in DOMAIN_PATTERN.finditer(content.lower()):
        normalized = normalize_domain(match.group(0))
        if normalized:
            found.add(normalized)
    return sorted(found)


def extract_domains_from_rows(rows: list[list[str]]) -> list[str]:
    found: set[str] = set()
    for row in rows:
        for cell in row:
            for match in DOMAIN_PATTERN.finditer(cell.lower()):
                normalized = normalize_domain(match.group(0))
                if normalized:
                    found.add(normalized)
    return sorted(found)


async def parse_upload(file: UploadFile, max_bytes: int) -> list[str]:
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported file type. Allowed: .txt, .csv, .xlsx")

    raw = await file.read()
    if len(raw) > max_bytes:
        raise ValueError("Uploaded file is too large")

    if extension == ".txt":
        return extract_domains_from_text(raw.decode("utf-8", errors="ignore"))

    if extension == ".csv":
        text = raw.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        rows = [[cell.strip() for cell in row] for row in reader]
        return extract_domains_from_rows(rows)

    workbook = load_workbook(filename=io.BytesIO(raw), read_only=True, data_only=True)
    rows: list[list[str]] = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            rows.append([str(cell or "").strip() for cell in row])
    return extract_domains_from_rows(rows)
