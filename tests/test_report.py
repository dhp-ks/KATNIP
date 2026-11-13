import json
from pathlib import Path

from katnip.config import PostprocessConfig
from katnip.report import generate_report, registrable_domain


def test_registrable_domain() -> None:
    assert registrable_domain("cdn.example.co.uk") == "example.co.uk"
    assert registrable_domain("127.0.0.1") == "127.0.0.1"


def test_generate_third_party_report(tmp_path: Path) -> None:
    mitm_dir = tmp_path / "mitm"
    mitm_dir.mkdir()
    records = [
        {
            "timestamp": "2026-07-30T12:00:00+00:00",
            "method": "GET",
            "scheme": "https",
            "host": "www.example.com",
            "port": 443,
            "path": "/",
            "url": "https://www.example.com/",
        },
        {
            "timestamp": "2026-07-30T12:00:01+00:00",
            "method": "GET",
            "scheme": "https",
            "host": "cdn.vendor.test",
            "port": 443,
            "path": "/asset.js?user=secret",
            "url": "https://cdn.vendor.test/asset.js?user=secret",
        },
    ]
    with (mitm_dir / "capture_requests.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    summary = generate_report(
        tmp_path,
        "https://www.example.com",
        PostprocessConfig(),
    )

    assert summary["unique_third_party_hosts"] == 1
    assert summary["third_party_request_count"] == 1
    output = (tmp_path / "report" / "third_party_domains.json").read_text()
    assert "user=secret" not in output
