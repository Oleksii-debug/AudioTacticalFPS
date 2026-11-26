"""Explicit, bounded DOI metadata previews; no document acquisition or uploads.

Crossref and Unpaywall are the only network destinations.  Provider text is
data, never a command, a download authorization, or a verified quotation.
The standard-library HTTPS worker is supervised so cancellation and the time
budget also cover DNS resolution and a peer that sends an endless slow body.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import base64
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
from threading import Lock
import time
import unicodedata
from urllib.parse import quote, unquote, urlencode, urlsplit


_HOSTS = {"crossref": "api.crossref.org", "unpaywall": "api.unpaywall.org"}
_ERRORS = {
    "connection": "The DOI metadata service could not be reached. No automatic retry was made.",
    "timeout": "The DOI metadata request exceeded its time budget.",
    "redirect": "The DOI metadata service attempted a redirect; it was not followed.",
    "destination": "The DOI metadata response came from an unexpected destination.",
    "rate_limited": "The DOI metadata service requested a pause. Retry only after the indicated delay.",
    "not_found": "The selected DOI metadata service has no record for this DOI.",
    "http_status": "The DOI metadata service returned an unsuccessful HTTP status.",
    "content_type": "The DOI metadata service did not return an uncompressed JSON response.",
    "response_size": "The DOI metadata response exceeded its byte or structure limit.",
    "invalid_response": "The DOI metadata service returned an invalid or incomplete response.",
    "doi_mismatch": "The returned metadata identifies a different DOI; it was rejected.",
    "worker": "The isolated DOI metadata worker failed without returning a valid result.",
}
_TYPE_MAP = {
    "journal-article": "article-journal", "proceedings-article": "paper-conference",
    "book": "book", "monograph": "book", "edited-book": "book",
    "reference-book": "book", "book-series": "book", "book-set": "book",
    "book-chapter": "chapter", "book-section": "chapter", "book-part": "chapter",
    "reference-entry": "entry-encyclopedia", "report": "report",
    "report-series": "report", "dissertation": "thesis", "posted-content": "article",
    "peer-review": "review", "dataset": "dataset", "standard": "standard",
    "standard-series": "standard", "component": "document", "journal": "periodical",
    "journal-issue": "periodical", "proceedings": "book", "proceedings-series": "book",
}


class DoiMetadataError(RuntimeError):
    """User-safe error: no response body, email, request URL, or provider exception."""

    def __init__(self, code: str, provider: str, *, retry_after_seconds: int | None = None):
        if code not in _ERRORS or provider not in _HOSTS:
            raise ValueError("Invalid DOI metadata error category.")
        self.code = code
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds
        super().__init__(_ERRORS[code])


def normalize_doi(value: str) -> str:
    """Normalize a bare DOI, ``doi:`` label, or an explicit doi.org URL.

    Punctuation in a DOI suffix is retained.  Percent decoding is performed
    exactly once, only for resolver URLs; a bare DOI's percent sign is literal.
    Resolver queries/fragments are ambiguous and must be pasted as a bare DOI
    or percent-encoded in the path.  No resolver is contacted.
    """
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Enter a DOI or a doi.org DOI URL of at most 4096 characters.")
    value = value.strip()
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    if re.match(r"https?://", value, re.I):
        try:
            parsed = urlsplit(value)
            if (parsed.netloc.lower() not in ("doi.org", "dx.doi.org")
                    or parsed.query or parsed.fragment or not parsed.path.startswith("/")):
                raise ValueError
            value = unquote(parsed.path[1:], encoding="utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise ValueError("Use an unambiguous doi.org URL without credentials, a query, or a fragment.") from None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError("The DOI contains invalid Unicode.") from None
    if (not 7 <= len(encoded) <= 2048 or not re.fullmatch(r"10\.[0-9]{4,9}/.+", value)
            or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)
            or "\\" in value or any(part in (".", "..") for part in value.split("/"))):
        raise ValueError("Enter a valid DOI with a 10. prefix and a nonempty suffix.")
    return value.lower()


def _email(value: str | None) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not 3 <= len(value) <= 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", value)):
        raise ValueError("Configure a valid contact email before enabling Unpaywall.")
    return value


def _cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InterruptedError("DOI metadata lookup cancelled; no document was imported.")


@dataclass(frozen=True, slots=True)
class MetadataRequest:
    """Fixed-destination transport envelope.  Email is excluded from repr()."""

    provider: str
    doi: str = field(repr=False)
    timeout: float = 15.0
    max_response_bytes: int = 2_000_000
    email: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.provider not in _HOSTS:
            raise ValueError("Select Crossref or Unpaywall explicitly.")
        object.__setattr__(self, "doi", normalize_doi(self.doi))
        if (type(self.timeout) not in (float, int) or not math.isfinite(self.timeout)
                or not 0.1 <= self.timeout <= 60):
            raise ValueError("The DOI request time budget must be between 0.1 and 60 seconds.")
        if type(self.max_response_bytes) is not int or not 1024 <= self.max_response_bytes <= 8_000_000:
            raise ValueError("The DOI response budget must be between 1024 and 8000000 bytes.")
        email = _email(self.email)
        if self.provider == "unpaywall" and email is None:
            raise ValueError("Unpaywall requires your explicitly configured contact email.")
        if self.provider == "crossref" and email is not None:
            raise ValueError("The Crossref public metadata adapter does not transmit an email.")

    @property
    def host(self) -> str:
        return _HOSTS[self.provider]

    @property
    def path(self) -> str:
        prefix = "/works/" if self.provider == "crossref" else "/v2/"
        result = prefix + quote(self.doi, safe="")
        if self.provider == "unpaywall":
            result += "?" + urlencode({"email": self.email})
        return result

    @property
    def url(self) -> str:
        return "https://" + self.host + self.path

    @property
    def public_url(self) -> str:
        """Provenance endpoint without the contact email query."""
        return "https://" + self.host + self.path.split("?", 1)[0]


@dataclass(frozen=True, slots=True)
class MetadataResponse:
    status: int
    headers: tuple[tuple[str, str], ...] | Mapping[str, str]
    body: bytes = field(repr=False)
    url: str = field(repr=False)

    def __post_init__(self):
        source = self.headers.items() if isinstance(self.headers, Mapping) else self.headers
        object.__setattr__(self, "headers", tuple(tuple(pair) for pair in source))


@dataclass(frozen=True, slots=True)
class MetadataProvenance:
    provider: str
    endpoint: str
    retrieved_at: str
    response_sha256: str
    response_bytes: int
    mapped_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LicenseNotice:
    provider: str
    identifier: str | None = None
    url: str | None = None
    applies_to: str | None = None
    start_date: str | None = None
    location_url: str | None = None
    declaration: str = "declared"
    permission_verified: bool = field(default=False, init=False)
    review_required: bool = field(default=True, init=False)


@dataclass(frozen=True, slots=True)
class AccessLocation:
    provider: str
    url: str
    pdf_url: str | None = None
    landing_page_url: str | None = None
    host_type: str | None = None
    version: str | None = None
    license: str | None = None
    content_type: str | None = None
    intended_application: str | None = None
    is_best: bool = False
    embargoed: bool = False
    permission_verified: bool = field(default=False, init=False)
    review_required: bool = field(default=True, init=False)


@dataclass(frozen=True, slots=True)
class DoiMetadataPreview:
    """An immutable bibliographic preview, never an import or reuse permission."""

    doi: str
    reference_id: str
    _csl_json: str = field(repr=False)
    sources: tuple[MetadataProvenance, ...]
    field_sources: tuple[tuple[str, str], ...]
    licenses: tuple[LicenseNotice, ...]
    locations: tuple[AccessLocation, ...]
    open_access: bool | None
    oa_status: str | None
    warnings: tuple[str, ...]
    from_cache: bool = False
    review_required: bool = field(default=True, init=False)

    @property
    def csl_item(self) -> dict:
        """A new JSON copy; modifying it cannot change the stored preview."""
        return json.loads(self._csl_json)

    def to_dict(self) -> dict:
        return {
            "schema": "carr-doi-preview/1", "doi": self.doi,
            "reference_id": self.reference_id, "csl_item": self.csl_item,
            "sources": [asdict(row) for row in self.sources],
            "field_sources": dict(self.field_sources),
            "licenses": [asdict(row) for row in self.licenses],
            "locations": [asdict(row) for row in self.locations],
            "open_access": self.open_access, "oa_status": self.oa_status,
            "warnings": list(self.warnings), "from_cache": self.from_cache,
            "review_required": True,
        }


def _headers(response: MetadataResponse, request: MetadataRequest) -> dict[str, str]:
    if not isinstance(response, MetadataResponse) or type(response.status) is not int:
        raise DoiMetadataError("invalid_response", request.provider)
    if response.url != request.url:
        raise DoiMetadataError("destination", request.provider)
    if not 100 <= response.status <= 599 or len(response.headers) > 100:
        raise DoiMetadataError("invalid_response", request.provider)
    result: dict[str, str] = {}
    size = 0
    for row in response.headers:
        if len(row) != 2 or any(not isinstance(part, str) for part in row):
            raise DoiMetadataError("invalid_response", request.provider)
        name, value = row
        size += len(name) + len(value)
        if size > 16_384:
            raise DoiMetadataError("response_size", request.provider)
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name) or any(c in value for c in "\r\n\x00"):
            raise DoiMetadataError("invalid_response", request.provider)
        name = name.lower()
        if name in result and name in ("content-type", "content-length", "content-encoding", "retry-after"):
            raise DoiMetadataError("invalid_response", request.provider)
        result[name] = value.strip()
    return result


def _retry_after(value: str | None) -> int:
    if value and value.isascii() and value.isdecimal() and len(value) <= 10:
        return max(1, min(86_400, int(value)))
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is not None:
                return max(1, min(86_400, math.ceil((parsed - datetime.now(timezone.utc)).total_seconds())))
        except (TypeError, ValueError, OverflowError):
            pass
    return 60


def _check_response(response: MetadataResponse, request: MetadataRequest) -> dict[str, str]:
    headers = _headers(response, request)
    status = response.status
    if 300 <= status <= 399:
        raise DoiMetadataError("redirect", request.provider)
    if status == 429:
        raise DoiMetadataError("rate_limited", request.provider, retry_after_seconds=_retry_after(headers.get("retry-after")))
    if status == 404:
        raise DoiMetadataError("not_found", request.provider)
    if status != 200:
        raise DoiMetadataError("http_status", request.provider)
    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if (not (media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json")))
            or headers.get("content-encoding", "identity").lower() != "identity"):
        raise DoiMetadataError("content_type", request.provider)
    if not isinstance(response.body, bytes) or len(response.body) > request.max_response_bytes:
        raise DoiMetadataError("response_size", request.provider)
    length = headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > request.max_response_bytes:
            raise DoiMetadataError("response_size", request.provider)
        if int(length) != len(response.body):
            raise DoiMetadataError("invalid_response", request.provider)
    return headers


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def _json_response(response: MetadataResponse, request: MetadataRequest) -> dict:
    _check_response(response, request)
    try:
        value = json.loads(response.body.decode("utf-8-sig"), object_pairs_hook=_no_duplicates,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise DoiMetadataError("invalid_response", request.provider) from None
    if type(value) is not dict:
        raise DoiMetadataError("invalid_response", request.provider)
    count = 0
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 24 or count > 50_000:
            raise DoiMetadataError("response_size", request.provider)
        if isinstance(node, dict):
            if any(not isinstance(key, str) or len(key) > 512 for key in node):
                raise DoiMetadataError("invalid_response", request.provider)
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
        elif isinstance(node, str):
            try:
                node.encode("utf-8")
            except UnicodeError:
                raise DoiMetadataError("invalid_response", request.provider) from None
            if len(node) > 256_000:
                raise DoiMetadataError("response_size", request.provider)
    return value


class _MetadataText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.had_markup = False

    def handle_starttag(self, tag, attrs):
        self.had_markup = True
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li") and not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        self.had_markup = True
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "li") and not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value, *, provider: str, maximum: int = 16_000, numeric: bool = False) -> str | None:
    if value is None or value == "":
        return None
    if numeric and type(value) is int:
        value = str(value)
    if not isinstance(value, str) or len(value) > maximum:
        raise DoiMetadataError("invalid_response", provider)
    if any(unicodedata.category(c) in ("Cs", "Cc") and c not in "\t\r\n" for c in value):
        raise DoiMetadataError("invalid_response", provider)
    return value.strip() or None


def _plain(value, provider: str, warnings: list[str]) -> str | None:
    source = _text(value, provider=provider)
    if source is None:
        return None
    parser = _MetadataText()
    try:
        parser.feed(source)
        parser.close()
    except (ValueError, RecursionError):
        raise DoiMetadataError("invalid_response", provider) from None
    if parser.had_markup:
        warnings.append("Provider markup was converted to plain text; review the displayed title and names.")
    result = " ".join("".join(parser.parts).split())
    return result or None


def _first_text(value, provider: str, warnings: list[str]) -> str | None:
    if value is None or value == []:
        return None
    if not isinstance(value, list) or len(value) > 100:
        raise DoiMetadataError("invalid_response", provider)
    for entry in value:
        _text(entry, provider=provider)
    if len(value) > 1:
        warnings.append("The provider supplied alternative titles; the first title is shown for review.")
    return _plain(value[0], provider, warnings) if value else None


def _preview_url(value, provider: str, warnings: list[str]) -> str | None:
    value = _text(value, provider=provider, maximum=4096)
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in ("https", "http") or not host or parsed.username is not None
                or parsed.password is not None or "\\" in value or any(c.isspace() for c in value)
                or parsed.port not in (None, 80, 443)):
            raise ValueError
        ascii_host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if ("." not in ascii_host or ascii_host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
                or ascii_host in ("localhost", "localhost.localdomain")):
            raise ValueError
        try:
            address = ipaddress.ip_address(ascii_host)
        except ValueError:
            labels = ascii_host.split(".")
            if (len(ascii_host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                    or labels[-1].isdecimal() or re.fullmatch(r"0x[0-9a-f]+", labels[-1])):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        return value
    except (ValueError, UnicodeError):
        warnings.append("A provider link was omitted because it was not an eligible HTTP(S) preview URL without credentials.")
        return None


def _date_parts(value, provider: str, warnings: list[str]) -> dict | None:
    if value is None:
        return None
    try:
        if type(value) is not dict or type(value.get("date-parts")) is not list:
            raise ValueError
        rows = value["date-parts"]
        if not 1 <= len(rows) <= 2:
            raise ValueError
        result = []
        for row in rows:
            if not isinstance(row, list) or not 1 <= len(row) <= 3 or any(type(part) is not int for part in row):
                raise ValueError
            if not 1 <= row[0] <= 9999 or (len(row) > 1 and not 1 <= row[1] <= 12):
                raise ValueError
            if len(row) == 3:
                date(*row)
            result.append(row[:])
        return {"date-parts": result}
    except (ValueError, TypeError, OverflowError):
        warnings.append("An incomplete or invalid provider date was omitted; no month or day was invented.")
        return None


def _authors(value, provider: str, warnings: list[str]) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 2048:
        raise DoiMetadataError("response_size", provider)
    result = []
    for entry in value:
        if type(entry) is not dict:
            raise DoiMetadataError("invalid_response", provider)
        parts = {key: _plain(entry.get(key), provider, warnings) for key in ("family", "given", "suffix")}
        name = {key: value for key, value in parts.items() if value}
        if not name:
            literal = _plain(entry.get("name"), provider, warnings)
            if literal:
                name = {"literal": literal}
        if name:
            result.append(name)
        else:
            warnings.append("A provider contributor had no usable name and was omitted.")
    return result


def _work_type(value, provider: str, warnings: list[str]) -> str:
    kind = _text(value, provider=provider, maximum=100)
    if kind in _TYPE_MAP:
        return _TYPE_MAP[kind]
    warnings.append("The publication type is unknown; review the generic CSL document type before citing it.")
    return "document"


def _identifiers(value, provider: str) -> str | None:
    if value is None or value == []:
        return None
    if not isinstance(value, list) or len(value) > 100:
        raise DoiMetadataError("invalid_response", provider)
    parts = [_text(row, provider=provider, maximum=128) for row in value]
    return "; ".join(dict.fromkeys(part for part in parts if part)) or None


def _crossref(value: dict, request: MetadataRequest):
    provider = request.provider
    if value.get("status") != "ok" or value.get("message-type") != "work" or type(value.get("message")) is not dict:
        raise DoiMetadataError("invalid_response", provider)
    message = value["message"]
    _matching_doi(message.get("DOI"), request)
    warnings: list[str] = []
    item: dict = {"type": _work_type(message.get("type"), provider, warnings)}
    title = _first_text(message.get("title"), provider, warnings)
    subtitle = _first_text(message.get("subtitle"), provider, warnings)
    if title:
        item["title"] = title + (": " + subtitle if subtitle else "")
    elif subtitle:
        item["title"] = subtitle
        warnings.append("Only a subtitle was supplied; review the publication title.")
    for source, target in (("container-title", "container-title"), ("short-container-title", "container-title-short"), ("short-title", "title-short")):
        text = _first_text(message.get(source), provider, warnings)
        if text:
            item[target] = text
    for source, target in (("publisher", "publisher"), ("publisher-location", "publisher-place"), ("language", "language")):
        text = _plain(message.get(source), provider, warnings)
        if text:
            item[target] = text
    for source, target in (("volume", "volume"), ("issue", "issue"), ("page", "page"), ("edition-number", "edition")):
        text = _text(message.get(source), provider=provider, maximum=256, numeric=True)
        if text:
            item[target] = text
    article_number = _text(message.get("article-number"), provider=provider, maximum=256, numeric=True)
    if article_number:
        item["number"] = article_number
    for role in ("author", "editor", "translator"):
        names = _authors(message.get(role), provider, warnings)
        if names:
            item[role] = names
    for source in ("issued", "published", "published-print", "published-online"):
        issued = _date_parts(message.get(source), provider, warnings)
        if issued:
            item["issued"] = issued
            break
    for key in ("ISBN", "ISSN"):
        result = _identifiers(message.get(key), provider)
        if result:
            item[key] = result
    notices: list[LicenseNotice] = []
    declared = message.get("license")
    if declared is None:
        declared = []
    if not isinstance(declared, list) or len(declared) > 1000:
        raise DoiMetadataError("invalid_response", provider)
    for row in declared:
        if type(row) is not dict:
            raise DoiMetadataError("invalid_response", provider)
        license_url = _preview_url(row.get("URL"), provider, warnings)
        if not license_url:
            warnings.append("A license declaration had no usable URL; permissions remain unknown.")
            continue
        applies = _text(row.get("content-version"), provider=provider, maximum=128)
        if applies is None:
            applies = _text(row.get("applies-to"), provider=provider, maximum=128)
        start = row.get("start")
        start_date = None
        if start is not None:
            if type(start) is not dict:
                raise DoiMetadataError("invalid_response", provider)
            start_date = _text(start.get("date-time"), provider=provider, maximum=100)
            if not start_date:
                parts = _date_parts(start, provider, warnings)
                if parts:
                    start_date = "-".join(str(part).zfill(4 if index == 0 else 2) for index, part in enumerate(parts["date-parts"][0]))
        notices.append(LicenseNotice(provider, url=license_url, applies_to=applies, start_date=start_date))
    locations: list[AccessLocation] = []
    links = message.get("link")
    if links is None:
        links = []
    if not isinstance(links, list) or len(links) > 1000:
        raise DoiMetadataError("invalid_response", provider)
    for row in links:
        if type(row) is not dict:
            raise DoiMetadataError("invalid_response", provider)
        address = _preview_url(row.get("URL"), provider, warnings)
        if address:
            kind = _text(row.get("content-type"), provider=provider, maximum=128)
            locations.append(AccessLocation(provider, address, pdf_url=address if kind == "application/pdf" else None,
                version=_text(row.get("content-version"), provider=provider, maximum=128), content_type=kind,
                intended_application=_text(row.get("intended-application"), provider=provider, maximum=128)))
    if not notices:
        warnings.append("Crossref supplied no usable full-text license; reuse and download permissions are unknown.")
    if locations:
        warnings.append("Crossref content links do not establish free access; licenses and versions must be reviewed separately.")
    return item, notices, locations, None, None, warnings


def _matching_doi(value, request: MetadataRequest) -> None:
    try:
        returned = normalize_doi(value)
    except ValueError:
        raise DoiMetadataError("invalid_response", request.provider) from None
    if returned != request.doi:
        raise DoiMetadataError("doi_mismatch", request.provider)


def _unpaywall(value: dict, request: MetadataRequest):
    provider = request.provider
    _matching_doi(value.get("doi"), request)
    warnings: list[str] = []
    item = {"type": _work_type(value.get("genre"), provider, warnings)}
    for source, target in (("title", "title"), ("publisher", "publisher"), ("journal_name", "container-title")):
        text = _plain(value.get(source), provider, warnings)
        if text:
            item[target] = text
    published = _text(value.get("published_date"), provider=provider, maximum=100)
    if published:
        if re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}){0,2}", published):
            issued = _date_parts({"date-parts": [[int(part) for part in published.split("-")]]}, provider, warnings)
            if issued:
                item["issued"] = issued
        else:
            warnings.append("An unrecognized publication date was omitted; no publication date was inferred.")
    year = value.get("year")
    if "issued" not in item and year is not None:
        issued = _date_parts({"date-parts": [[year]]}, provider, warnings)
        if issued:
            item["issued"] = issued
    if value.get("z_authors") is not None:
        warnings.append("Unpaywall's deprecated author field was omitted; verify authors with Crossref or the publication.")
    issns = _text(value.get("journal_issns"), provider=provider, maximum=1024)
    if issns:
        item["ISSN"] = issns
    opened = value.get("is_oa")
    if opened is not None and type(opened) is not bool:
        raise DoiMetadataError("invalid_response", provider)
    status = _text(value.get("oa_status"), provider=provider, maximum=100)
    if status not in (None, "gold", "green", "hybrid", "bronze", "closed"):
        warnings.append("The provider reported an unrecognized open-access status; review it manually.")
    if (opened is True and status == "closed") or (opened is False and status in ("gold", "green", "hybrid", "bronze")):
        warnings.append("The provider's open-access fields conflict; no permission is inferred from them.")
    listed: list[tuple[dict, bool]] = []
    for field_name, embargoed in (("oa_locations", False), ("oa_locations_embargoed", True)):
        rows = value.get(field_name)
        if rows is None:
            rows = []
        if not isinstance(rows, list) or len(rows) > 1000 or any(type(row) is not dict for row in rows):
            raise DoiMetadataError("invalid_response", provider)
        listed.extend((row, embargoed) for row in rows)
    best = value.get("best_oa_location")
    if best is not None:
        if type(best) is not dict:
            raise DoiMetadataError("invalid_response", provider)
        if not any(row == best and not embargoed for row, embargoed in listed):
            listed.insert(0, (best, False))
    notices: list[LicenseNotice] = []
    locations: list[AccessLocation] = []
    for row, embargoed in listed:
        pdf = _preview_url(row.get("url_for_pdf"), provider, warnings)
        landing = _preview_url(row.get("url_for_landing_page"), provider, warnings)
        address = _preview_url(row.get("url"), provider, warnings) or pdf or landing
        if not address:
            continue
        license_id = _text(row.get("license"), provider=provider, maximum=2000)
        is_best = row.get("is_best", row == best)
        if type(is_best) is not bool:
            raise DoiMetadataError("invalid_response", provider)
        host_type = _text(row.get("host_type"), provider=provider, maximum=100)
        version = _text(row.get("version"), provider=provider, maximum=128)
        if host_type not in ("publisher", "repository") or version not in ("submittedVersion", "acceptedVersion", "publishedVersion"):
            warnings.append("A location has an unknown host type or manuscript version; verify the source copy manually.")
        locations.append(AccessLocation(provider, address, pdf, landing, host_type, version, license_id,
                                        is_best=is_best, embargoed=embargoed))
        if license_id:
            notices.append(LicenseNotice(provider, identifier=license_id, applies_to=version,
                location_url=address, declaration="implied" if license_id == "implied-oa" else "declared"))
        if not license_id or license_id == "implied-oa":
            warnings.append("An open-access location has no explicit license terms; permission requires review at that source.")
        if embargoed:
            warnings.append("An embargoed location is listed for preview only; current availability has not been verified.")
        if row.get("oa_date") is not None:
            warnings.append("Unpaywall oa_date is not used to determine availability or embargo expiry.")
    if not locations:
        warnings.append("Unpaywall supplied no usable public location; no document has been retrieved.")
    if opened is False and any(not row.embargoed for row in locations):
        warnings.append("The provider lists locations despite a closed status; availability requires manual review.")
    return item, notices, locations, opened, status, warnings


class DoiMetadataClient:
    """A metadata-only client whose network capability is disabled by default.

    ``transport`` is a trusted application/test boundary accepting
    ``(MetadataRequest, *, cancelled=callback) -> MetadataResponse``.  It is not
    a user-configurable URL override.  The default executes a fixed HTTPS-only
    worker with an overall deadline.  Calls never retry themselves.
    """

    def __init__(self, *, allow_network: bool = False, providers=("crossref",),
                 unpaywall_email: str | None = None, transport=None, timeout: float = 15,
                 max_response_bytes: int = 2_000_000, cache_seconds: float = 3600,
                 min_interval_seconds: float = 1):
        if type(allow_network) is not bool:
            raise ValueError("Online DOI metadata requires an explicit boolean opt-in.")
        if (not isinstance(providers, (tuple, list)) or not 1 <= len(providers) <= 2
                or any(not isinstance(provider, str) or provider not in _HOSTS for provider in providers)
                or len(set(providers)) != len(providers)):
            raise ValueError("Choose Crossref and/or Unpaywall without duplicate providers.")
        email = _email(unpaywall_email)
        for provider in providers:
            MetadataRequest(provider, "10.5555/validation", timeout, max_response_bytes,
                            email if provider == "unpaywall" else None)
        for value, maximum in ((cache_seconds, 86_400), (min_interval_seconds, 60)):
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError("Invalid DOI cache or request-spacing setting.")
        if transport is not None and not callable(transport):
            raise ValueError("The metadata transport must be callable.")
        self.allow_network = allow_network
        self.providers = tuple(providers)
        self._email = email
        self._transport = transport if transport is not None else _worker_transport
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self.cache_seconds = float(cache_seconds)
        self.min_interval_seconds = float(min_interval_seconds)
        self._cache: OrderedDict[str, tuple[float, DoiMetadataPreview]] = OrderedDict()
        self._next_call: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}
        self._lock = Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def lookup(self, doi: str, *, cancelled: Callable[[], bool] | None = None,
               refresh: bool = False) -> DoiMetadataPreview:
        if self.allow_network is not True:
            raise PermissionError("Online DOI metadata is disabled. Explicitly enable a provider before requesting metadata.")
        if type(refresh) is not bool:
            raise ValueError("The refresh choice must be a boolean.")
        if cancelled is None:
            cancelled = lambda: False
        if not callable(cancelled):
            raise ValueError("The cancellation callback must be callable.")
        normalized = normalize_doi(doi)
        _cancel(cancelled)
        # Serialize this client's calls so cache deduplication and per-provider
        # request spacing remain true under native UI background threads.
        while not self._lock.acquire(timeout=.05):
            _cancel(cancelled)
        try:
            _cancel(cancelled)
            cached = self._cache.get(normalized)
            if not refresh and cached and time.monotonic() - cached[0] < self.cache_seconds:
                self._cache.move_to_end(normalized)
                return replace(cached[1], from_cache=True)
            parsed = {}
            sources: list[MetadataProvenance] = []
            for provider in self.providers:
                _cancel(cancelled)
                cooldown = self._cooldown.get(provider, 0) - time.monotonic()
                if cooldown > 0:
                    raise DoiMetadataError("rate_limited", provider, retry_after_seconds=math.ceil(cooldown))
                while self._next_call.get(provider, 0) > time.monotonic():
                    _cancel(cancelled)
                    time.sleep(min(.05, max(0, self._next_call[provider] - time.monotonic())))
                request = MetadataRequest(provider, normalized, self.timeout, self.max_response_bytes,
                                          self._email if provider == "unpaywall" else None)
                self._next_call[provider] = time.monotonic() + self.min_interval_seconds
                try:
                    response = self._transport(request, cancelled=cancelled)
                    _cancel(cancelled)
                    value = _json_response(response, request)
                    result = (_crossref if provider == "crossref" else _unpaywall)(value, request)
                except DoiMetadataError as error:
                    if error.code == "rate_limited":
                        self._cooldown[provider] = time.monotonic() + (error.retry_after_seconds or 60)
                    raise
                except InterruptedError:
                    raise
                except Exception:
                    raise DoiMetadataError("connection", provider) from None
                parsed[provider] = result
                sources.append(MetadataProvenance(provider, request.public_url,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), sha256(response.body).hexdigest(),
                    len(response.body), tuple(sorted(result[0]))))
            item = {}
            origins = {}
            notices = []
            locations = []
            warnings = []
            opened = None
            status = None
            # Crossref remains the bibliographic authority when both are chosen;
            # Unpaywall fills missing fields and supplies access declarations.
            for provider in ("crossref", "unpaywall"):
                if provider not in parsed:
                    continue
                data, rights, copies, oa, oa_status, notes = parsed[provider]
                for key, value in data.items():
                    if key not in item:
                        item[key] = value
                        origins[key] = provider
                    elif item[key] != value:
                        warnings.append(f"Providers disagree on {key}; Crossref's value is retained for review.")
                notices.extend(rights)
                locations.extend(copies)
                warnings.extend(notes)
                if provider == "unpaywall":
                    opened, status = oa, oa_status
            reference_id = "doi-" + sha256(normalized.encode("utf-8")).hexdigest()
            item.update(id=reference_id, DOI=normalized,
                        URL="https://doi.org/" + quote(normalized, safe="/"))
            origins.update(id="normalized-doi", DOI="normalized-doi", URL="normalized-doi")
            if not item.get("title"):
                warnings.append("No title was supplied; complete the bibliographic metadata before citation export.")
            if not item.get("author"):
                warnings.append("No current author list was supplied; review the publication before citation export.")
            if not item.get("issued"):
                warnings.append("No valid publication date was supplied; a date has not been invented.")
            if item.get("page"):
                warnings.append("The page field is the publication's reported extent, not a verified quotation locator.")
            warnings.append("Review the source, edition, version, and license before any separate document acquisition or import.")
            preview = DoiMetadataPreview(normalized, reference_id,
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")), tuple(sources),
                tuple(sorted(origins.items())), tuple(dict.fromkeys(notices)), tuple(dict.fromkeys(locations)),
                opened, status, tuple(dict.fromkeys(warnings)))
            _cancel(cancelled)
            if self.cache_seconds:
                self._cache[normalized] = (time.monotonic(), preview)
                self._cache.move_to_end(normalized)
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
            return preview
        finally:
            self._lock.release()


def _https_once(request: MetadataRequest) -> MetadataResponse:
    """Worker-only HTTPS transport: no proxies, cookies, redirects or retries."""
    # Keep this directly launched isolated worker self-contained. The client
    # defaults verify certificates and hostnames without consulting the
    # environment's SSLKEYLOGFILE (unlike create_default_context).
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    connection = http.client.HTTPSConnection(request.host, port=443, timeout=request.timeout,
                                             context=context)
    deadline = time.monotonic() + request.timeout
    try:
        connection.request("GET", request.path, headers={
            "Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close",
            "User-Agent": "UniversalResearchLibrary/0.2 DOI-metadata-preview",
        })
        response = connection.getresponse()
        headers = tuple(response.getheaders())
        envelope = MetadataResponse(response.status, headers, b"", request.url)
        values = _headers(envelope, request)
        # Error/redirect bodies can contain reflected emails or very large HTML;
        # they are neither read nor passed back to the application.
        if response.status != 200:
            return envelope
        media_type = values.get("content-type", "").split(";", 1)[0].strip().lower()
        if (not (media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json")))
                or values.get("content-encoding", "identity").lower() != "identity"):
            raise DoiMetadataError("content_type", request.provider)
        declared = values.get("content-length")
        if declared is not None and (not declared.isascii() or not declared.isdecimal() or len(declared) > 10
                                     or int(declared) > request.max_response_bytes):
            raise DoiMetadataError("response_size", request.provider)
        chunks = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DoiMetadataError("timeout", request.provider)
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            block = response.read1(min(65_536, request.max_response_bytes + 1 - size))
            if not block:
                break
            size += len(block)
            if size > request.max_response_bytes:
                raise DoiMetadataError("response_size", request.provider)
            chunks.append(block)
        return MetadataResponse(response.status, headers, b"".join(chunks), request.url)
    finally:
        connection.close()


def _worker_argv() -> list[str]:
    """Frozen launchers must dispatch this exact flag before starting wx."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--carr-doi-metadata-worker"]
    return [sys.executable, "-I", str(Path(__file__).resolve()), "--metadata-worker"]


def _worker_transport(request: MetadataRequest, *, cancelled: Callable[[], bool]) -> MetadataResponse:
    _cancel(cancelled)
    payload = json.dumps(asdict(request), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    process = subprocess.Popen(_worker_argv(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
        env=environment, **options)
    deadline = time.monotonic() + request.timeout
    try:
        first = True
        while True:
            _cancel(cancelled)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DoiMetadataError("timeout", request.provider)
            try:
                outgoing, _ = process.communicate(payload if first else None, timeout=min(.05, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
        _cancel(cancelled)
        if process.returncode != 0 or len(outgoing) > 4 * ((request.max_response_bytes + 2) // 3) + 65_536:
            raise DoiMetadataError("worker", request.provider)
        try:
            envelope = json.loads(outgoing)
            if "error" in envelope:
                code = envelope["error"]
                raise DoiMetadataError(code if code in _ERRORS else "worker", request.provider)
            raw = base64.b64decode(envelope["body"], validate=True)
            return MetadataResponse(envelope["status"], envelope["headers"], raw, request.url)
        except DoiMetadataError:
            raise
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DoiMetadataError("worker", request.provider) from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def _worker_main() -> int:
    provider = "crossref"
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError
        data = json.loads(raw)
        request = MetadataRequest(**data)
        provider = request.provider
        response = _https_once(request)
        result = {"status": response.status, "headers": response.headers,
                  "body": base64.b64encode(response.body).decode("ascii")}
    except DoiMetadataError as error:
        result = {"error": error.code}
    except (TimeoutError,):
        result = {"error": "timeout"}
    except Exception:
        result = {"error": "connection"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--metadata-worker"]:
        raise SystemExit("This module is an internal DOI metadata worker.")
    raise SystemExit(_worker_main())
