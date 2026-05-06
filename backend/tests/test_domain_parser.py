from app.services.domain_parser import extract_domains_from_rows, extract_domains_from_text, normalize_domain


def test_normalize_domain_accepts_valid_fr_domain():
    assert normalize_domain("https://Example.FR/path") == "example.fr"


def test_extract_domains_from_text_deduplicates_results():
    content = "alpha.fr\nbeta.fr\nalpha.fr"
    assert extract_domains_from_text(content) == ["alpha.fr", "beta.fr"]


def test_extract_domains_from_rows_scans_all_columns():
    rows = [["name", "target"], ["one", "drop-a.fr"], ["two", "drop-b.fr"]]
    assert extract_domains_from_rows(rows) == ["drop-a.fr", "drop-b.fr"]
