from __future__ import annotations

import csv
import ipaddress
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import tldextract

from katnip.config import PostprocessConfig
from katnip.technology import default_catalog
from katnip.utils import strip_url_query, utc_now_iso, write_json


_EXTRACT = tldextract.TLDExtract(suffix_list_urls=None)

logger = logging.getLogger("katnip.report")


@dataclass
class RequestRecord:
    timestamp: str
    method: str
    scheme: str
    host: str
    port: int | None
    path: str
    url: str
    cookies: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DomainStats:
    host: str
    registrable_domain: str

    technology_id: str | None = None
    technology_name: str | None = None
    technology_company: str | None = None
    technology_category: str | None = None
    technology_description: str | None = None
    technology_confidence: str | None = None
    technology_matched_by: str | None = None

    request_count: int = 0
    methods: set[str] = field(default_factory=set)
    schemes: set[str] = field(default_factory=set)
    first_seen: str | None = None
    last_seen: str | None = None
    sample_urls: list[str] = field(default_factory=list)

    requests_with_cookies: int = 0
    sent_cookie_count: int = 0
    sent_cookie_names: set[str] = field(default_factory=set)
    sent_cookie_name_values: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["methods"] = sorted(self.methods)
        data["schemes"] = sorted(self.schemes)
        data["sent_cookie_names"] = sorted(self.sent_cookie_names)
        data["sent_cookie_name_values"] = sorted(self.sent_cookie_name_values)
        return data


def registrable_domain(host: str) -> str:
    clean = host.strip().lower().rstrip(".").strip("[]")
    if not clean:
        return ""

    try:
        ipaddress.ip_address(clean)
        return clean
    except ValueError:
        pass

    extracted = _EXTRACT(clean)
    return extracted.top_domain_under_public_suffix or clean


def _parse_request_cookie_header(
    header_value: str,
) -> list[tuple[str, str]]:
    cookies: list[tuple[str, str]] = []

    if not header_value:
        return cookies

    for piece in header_value.split(";"):
        piece = piece.strip()

        if not piece or "=" not in piece:
            continue

        name, value = piece.split("=", 1)
        name = name.strip()

        if not name:
            continue

        cookies.append((name, value.strip()))

    return cookies

def _load_capture_boundary(
    run_dir: Path,
) -> float | None:
    path = (
        run_dir
        / "mitm"
        / "post_consent_start.txt"
    )

    if not path.is_file():
        return None

    try:
        return float(
            path.read_text(
                encoding="utf-8"
            ).strip()
        )
    except (
        ValueError,
        OSError,
    ):
        return None

def _record_from_json(
    data: dict[str, object],
) -> RequestRecord | None:
    host = str(data.get("host") or "").strip().lower()
    url = str(data.get("url") or "")

    if not host and url:
        host = (urlsplit(url).hostname or "").lower()

    if not host:
        return None

    port_value = data.get("port")

    try:
        port = int(port_value) if port_value is not None else None
    except (TypeError, ValueError):
        port = None

    cookies: list[tuple[str, str]] = []
    raw_cookies = data.get("cookies")

    if isinstance(raw_cookies, dict):
        cookies = [
            (str(name), str(value))
            for name, value in raw_cookies.items()
        ]
    elif isinstance(raw_cookies, list):
        for item in raw_cookies:
            if isinstance(item, dict):
                name = item.get("name")
                value = item.get("value")
                if name is not None:
                    cookies.append((str(name), str(value or "")))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                cookies.append((str(item[0]), str(item[1])))
    else:
        cookie_header = str(
            data.get("cookie_header")
            or data.get("cookie")
            or ""
        )
        cookies = _parse_request_cookie_header(cookie_header)

    return RequestRecord(
        timestamp=str(data.get("timestamp") or ""),
        method=str(data.get("method") or "GET"),
        scheme=str(data.get("scheme") or urlsplit(url).scheme or ""),
        host=host,
        port=port,
        path=str(data.get("path") or urlsplit(url).path or "/"),
        url=url,
        cookies=cookies,
    )


def read_jsonl_requests(
    path: Path,
) -> list[RequestRecord]:
    records: list[RequestRecord] = []

    if not path.exists():
        return records

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                raw = json.loads(line)
                record = _record_from_json(raw)

                if record is not None:
                    records.append(record)

            except json.JSONDecodeError as exc:
                logging.getLogger("katnip.report").warning(
                    "Skipping invalid JSONL line %s in %s: %s",
                    line_number,
                    path,
                    exc,
                )

    return records


def _flow_timestamp(
    flow: object,
) -> str:
    request = getattr(flow, "request", None)
    timestamp = getattr(request, "timestamp_start", None)

    if timestamp is None:
        return ""

    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(
            float(timestamp),
            tz=timezone.utc,
        ).isoformat()

    except (TypeError, ValueError, OSError):
        return ""


def _mitm_request_cookies(
    request,
) -> list[tuple[str, str]]:
    """
    Read request cookies through mitmproxy's parsed cookie collection.

    Duplicate names are preserved when the installed mitmproxy version
    supports MultiDict.items(multi=True).
    """
    logger = logging.getLogger("katnip.report")
    cookies: list[tuple[str, str]] = []

    try:
        try:
            items = request.cookies.items(multi=True)
        except TypeError:
            items = request.cookies.items()

        for name, value in items:
            cookies.append((str(name), str(value)))

        return cookies

    except Exception as exc:
        logger.debug(
            "Unable to read parsed request cookies "
            "| host=%s | url=%s | error=%s",
            getattr(request, "pretty_host", ""),
            getattr(request, "pretty_url", ""),
            exc,
        )

    # Last-resort fallback to raw Cookie headers.
    try:
        cookie_headers = request.headers.get_all("cookie")
    except Exception:
        cookie_headers = []

    if not cookie_headers:
        try:
            raw = request.headers.get("cookie", "")
            if raw:
                cookie_headers = [str(raw)]
        except Exception:
            pass

    for header_value in cookie_headers:
        cookies.extend(
            _parse_request_cookie_header(
                str(header_value)
            )
        )

    return cookies


def read_mitm_requests(
    path: Path,
    *,
    minimum_timestamp: float | None = None,
) -> list[RequestRecord]:
    if not path.exists():
        return []

    from mitmproxy import io
    from mitmproxy.exceptions import FlowReadException

    requests: list[RequestRecord] = []
    skipped_before_boundary = 0

    try:
        with path.open("rb") as handle:
            reader = io.FlowReader(handle)

            for flow in reader.stream():
                request = getattr(flow, "request", None)

                if request is None:
                    continue
                
                request_timestamp = getattr(
                    request,
                    "timestamp_start",
                    None,
                )

                if (
                    minimum_timestamp is not None
                    and request_timestamp is not None
                    and float(request_timestamp)
                    < minimum_timestamp
                ):
                    skipped_before_boundary += 1
                    continue
                                
                host = request.pretty_host.lower().rstrip(".")

                requests.append(
                    RequestRecord(
                        timestamp=_flow_timestamp(flow),
                        method=request.method,
                        scheme=request.scheme,
                        host=host,
                        port=request.port,
                        path=request.path,
                        url=request.pretty_url,
                        cookies=_mitm_request_cookies(request),
                    )
                )

    except FlowReadException as exc:
        raise RuntimeError(
            f"Unable to parse mitm flow file {path}: {exc}"
        ) from exc
        
    logger.info(
        "MITM capture boundary filtering "
        "| boundary=%s "
        "| excluded_requests=%d "
        "| included_requests=%d",
        minimum_timestamp,
        skipped_before_boundary,
        len(requests),
    )

    return requests


def load_requests(
    run_dir: Path,
) -> tuple[list[RequestRecord], str]:
    flow_path = run_dir / "mitm" / "flows.mitm"
    
    boundary = _load_capture_boundary(
        run_dir
    )

    flow_requests = read_mitm_requests(
        flow_path,
        minimum_timestamp=boundary,
    )

    if flow_requests:
        return flow_requests, str(flow_path)

    jsonl_path = run_dir / "mitm" / "capture_requests.jsonl"
    return read_jsonl_requests(jsonl_path), str(jsonl_path)


def _is_ignored(
    host: str,
    ignored: Iterable[str],
) -> bool:
    host = host.lower().rstrip(".")

    return any(
        host == item.lower()
        or host.endswith("." + item.lower())
        for item in ignored
    )


def _build_stats(
    *,
    host: str,
    root: str,
    technology,
) -> DomainStats:
    return DomainStats(
        host=host,
        registrable_domain=root,
        technology_id=(technology.technology_id if technology else None),
        technology_name=(technology.name if technology else None),
        technology_company=(technology.company if technology else None),
        technology_category=(technology.category if technology else None),
        technology_description=(technology.description if technology else None),
        technology_confidence=(technology.confidence if technology else None),
        technology_matched_by=(technology.matched_by if technology else None),
    )


def generate_report(
    run_dir: Path,
    website: str,
    config: PostprocessConfig,
) -> dict[str, object]:
    logger = logging.getLogger("katnip.report")
    records, source = load_requests(run_dir)

    first_party_host = (urlsplit(website).hostname or "").lower()
    first_party_domain = registrable_domain(first_party_host)
    catalog = default_catalog()

    grouped: dict[tuple[str, str], DomainStats] = {}
    all_hosts: set[str] = set()
    third_party_domains: set[str] = set()

    total_third_party_requests_with_cookies = 0
    all_sent_cookie_keys: set[tuple[str, str]] = set()
    all_sent_cookie_name_values: set[tuple[str, str, str]] = set()

    for record in records:
        host = record.host.lower().rstrip(".")

        if not host or _is_ignored(host, config.ignore_hosts):
            continue

        all_hosts.add(host)
        root = registrable_domain(host)

        if root == first_party_domain:
            continue

        third_party_domains.add(root)

        technology = catalog.identify(
            host=host,
            registrable_domain=root,
            url=record.url,
            path=record.path,
        )

        technology_id = (
            technology.technology_id
            if technology is not None
            else "unclassified"
        )

        group_key = (host, technology_id)

        if group_key not in grouped:
            grouped[group_key] = _build_stats(
                host=host,
                root=root,
                technology=technology,
            )

        stats = grouped[group_key]
        stats.request_count += 1
        stats.methods.add(record.method)

        if record.scheme:
            stats.schemes.add(record.scheme)

        if record.timestamp:
            if stats.first_seen is None or record.timestamp < stats.first_seen:
                stats.first_seen = record.timestamp

            if stats.last_seen is None or record.timestamp > stats.last_seen:
                stats.last_seen = record.timestamp

        sample = (
            record.url
            if config.include_query_in_sample_urls
            else strip_url_query(record.url)
        )

        if (
            sample
            and sample not in stats.sample_urls
            and len(stats.sample_urls) < config.max_sample_urls_per_domain
        ):
            stats.sample_urls.append(sample)

        if record.cookies:
            stats.requests_with_cookies += 1
            total_third_party_requests_with_cookies += 1

            for name, value in record.cookies:
                stats.sent_cookie_names.add(name)
                stats.sent_cookie_name_values.add(f"{name}={value}")

                all_sent_cookie_keys.add((host, name))
                all_sent_cookie_name_values.add((host, name, value))

            stats.sent_cookie_count = len(stats.sent_cookie_names)

    rows = sorted(
        grouped.values(),
        key=lambda item: (
            -item.request_count,
            -item.sent_cookie_count,
            item.technology_name or "",
            item.host,
        ),
    )

    report_dir = run_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    identified_rows = [
        row
        for row in rows
        if row.technology_name
    ]

    identified_request_count = sum(
        row.request_count
        for row in identified_rows
    )

    summary: dict[str, object] = {
        "generated_at": utc_now_iso(),
        "website": website,
        "first_party_host": first_party_host,
        "first_party_registrable_domain": first_party_domain,
        "source": source,
        "total_captured_requests": len(records),
        "unique_request_hosts": len(all_hosts),
        "third_party_request_count": sum(
            row.request_count
            for row in rows
        ),
        "third_party_requests_with_cookies":
            total_third_party_requests_with_cookies,
        "unique_third_party_hosts": len(
            {
                row.host
                for row in rows
            }
        ),
        "unique_third_party_registrable_domains":
            len(third_party_domains),
        "identified_technology_count": len(
            {
                row.technology_id
                for row in identified_rows
                if row.technology_id
            }
        ),
        "identified_third_party_request_count":
            identified_request_count,
        "sent_cookie_count":
            len(all_sent_cookie_keys),
        "sent_cookie_name_value_count":
            len(all_sent_cookie_name_values),
        "sent_cookie_names": sorted(
            {
                name
                for _, name in all_sent_cookie_keys
            }
        ),
        "third_parties": [
            row.to_dict()
            for row in rows
        ],
    }

    write_json(
        report_dir / "third_parties.json",
        summary,
    )

    with (
        report_dir / "third_parties.csv"
    ).open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.writer(handle)

        writer.writerow(
            [
                "technology_name",
                "technology_company",
                "technology_category",
                "host",
                "registrable_domain",
                "request_count",
                "requests_with_cookies",
                "sent_cookie_count",
                "sent_cookie_names",
                "sent_cookie_name_values",
                "technology_confidence",
                "technology_matched_by",
                "methods",
                "schemes",
                "first_seen",
                "last_seen",
                "sample_urls",
                "technology_description",
            ]
        )

        for row in rows:
            writer.writerow(
                [
                    row.technology_name or "Unclassified",
                    row.technology_company or "",
                    row.technology_category or "unknown",
                    row.host,
                    row.registrable_domain,
                    row.request_count,
                    row.requests_with_cookies,
                    row.sent_cookie_count,
                    " | ".join(
                        sorted(row.sent_cookie_names)
                    ),
                    " | ".join(
                        sorted(row.sent_cookie_name_values)
                    ),
                    row.technology_confidence or "",
                    row.technology_matched_by or "",
                    ",".join(sorted(row.methods)),
                    ",".join(sorted(row.schemes)),
                    row.first_seen or "",
                    row.last_seen or "",
                    " | ".join(row.sample_urls),
                    row.technology_description or "",
                ]
            )

    lines = [
        "# KATNIP Third-Party Technology and Cookie Report",
        "",
        f"- Website: `{website}`",
        f"- First-party domain: `{first_party_domain}`",
        f"- Captured requests: **{len(records)}**",
        f"- Third-party requests: **{summary['third_party_request_count']}**",
        (
            "- Third-party requests carrying cookies: "
            f"**{summary['third_party_requests_with_cookies']}**"
        ),
        (
            "- Unique third-party hosts: "
            f"**{summary['unique_third_party_hosts']}**"
        ),
        (
            "- Unique cookies sent to third-party hosts: "
            f"**{summary['sent_cookie_count']}**"
        ),
        "",
        "## Third-party technologies, hosts, and sent cookies",
        "",
        (
            "| Technology | Company | Host | Requests | "
            "Requests with cookies | Cookies sent | "
            "Cookie names | Cookie values |"
        ),
        "|---|---|---|---:|---:|---:|---|---|",
    ]

    for row in rows:
        names = ", ".join(
            sorted(row.sent_cookie_names)
        ) or ""

        values = "<br>".join(
            value.replace("|", "\\|")
            for value in sorted(
                row.sent_cookie_name_values
            )
        )

        lines.append(
            f"| {row.technology_name or 'Unclassified'} "
            f"| {row.technology_company or ''} "
            f"| `{row.host}` "
            f"| {row.request_count} "
            f"| {row.requests_with_cookies} "
            f"| {row.sent_cookie_count} "
            f"| {names} "
            f"| {values} |"
        )

    if not rows:
        lines.append(
            "| _None found_ |  |  | 0 | 0 | 0 |  |  |"
        )

    lines.extend(
        [
            "",
            "## Cookie handling notes",
            "",
            (
                "Cookie metrics are based on cookies actually transmitted "
                "by the browser in HTTP `Cookie` request headers to third-party "
                "hosts during the captured portion of the journey."
            ),
            "",
            (
                "Cookies that exist in browser storage but are never sent to "
                "a captured third-party request are not included."
            ),
            "",
            (
                "Duplicate cookie names are preserved while reading mitmproxy "
                "flows. The CSV's per-row cookie count is based on distinct "
                "cookie names associated with each technology/host row."
            ),
            "",
            (
                "Cookie values are embedded directly in `third_parties.csv`. "
                "These values can contain persistent identifiers, session "
                "material, or other sensitive data. Treat report files as "
                "sensitive artifacts."
            ),
            "",
            (
                "Sample request URLs omit query strings by default to reduce "
                "accidental retention of identifiers."
            ),
            "",
        ]
    )

    (
        report_dir / "report.md"
    ).write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    logger.info(
        "Report written to %s",
        report_dir,
    )

    return summary
