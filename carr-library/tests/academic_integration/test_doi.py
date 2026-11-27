"""Synthetic metadata and real loopback HTTP; no public API or credentials."""
from dataclasses import FrozenInstanceError
from dataclasses import asdict
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
from io import BytesIO
import json
import base64
import os
from pathlib import Path
import ssl
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from carr_library.academic_integration import doi as module
from carr_library.academic_integration.doi import (
    DoiMetadataClient, DoiMetadataError, MetadataRequest, MetadataResponse, normalize_doi,
)


DOI = "10.5555/synthetic.source-2026"
EMAIL = "synthetic-person+metadata@example.org"


def crossref(**changes):
    message = {
        "DOI": DOI, "type": "journal-article", "title": ["Дослідження світла"],
        "subtitle": ["Перевірений опис"], "container-title": ["Synthetic Research"],
        "author": [{"family": "Коваленко", "given": "Олена"}, {"name": "Synthetic Research Group"}],
        "issued": {"date-parts": [[2026, 7]]}, "volume": "9", "issue": "2", "page": "234–245",
        "publisher": "Synthetic Press", "ISSN": ["1234-5678"], "edition-number": "2",
        "abstract": "This excluded abstract is not a citation locator or consent to import.",
        "reference": [{"article-title": "Excluded cited document"}],
        "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/", "content-version": "vor",
                     "start": {"date-parts": [[2026, 7, 1]], "date-time": "2026-07-01T00:00:00Z"}}],
        "link": [{"URL": "https://publisher.example.org/article.pdf", "content-type": "application/pdf",
                  "content-version": "vor", "intended-application": "text-mining"}],
    }
    message.update(changes)
    return {"status": "ok", "message-type": "work", "message-version": "1.0.0", "message": message}


def unpaywall(**changes):
    location = {
        "url": "https://repository.example.org/article/accepted.pdf",
        "url_for_pdf": "https://repository.example.org/article/accepted.pdf",
        "url_for_landing_page": "https://repository.example.org/article/record",
        "license": "cc-by", "version": "acceptedVersion", "host_type": "repository", "is_best": True,
        "oa_date": "2026-07-01",
    }
    result = {
        "doi": DOI, "genre": "journal-article", "title": "Дослідження світла: Перевірений опис",
        "journal_name": "Synthetic Research", "published_date": "2026-07-01", "year": 2026,
        "publisher": "Synthetic Press", "is_oa": True, "oa_status": "green",
        "best_oa_location": location, "oa_locations": [location], "oa_locations_embargoed": [],
        "z_authors": [{"given": "Deprecated", "family": "Name"}],
    }
    result.update(changes)
    return result


def raw_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Transport:
    def __init__(self, value=None, *, status=200, headers=None, url=None):
        self.value = crossref() if value is None else value
        self.status = status
        self.headers = headers
        self.url = url
        self.requests = []

    def __call__(self, request, *, cancelled):
        self.requests.append(request)
        body = self.value if isinstance(self.value, bytes) else raw_json(self.value)
        headers = self.headers if self.headers is not None else {"Content-Type": "application/json", "Content-Length": str(len(body))}
        return MetadataResponse(self.status, headers, body, self.url if self.url is not None else request.url)


def client(transport, **settings):
    return DoiMetadataClient(allow_network=True, transport=transport, min_interval_seconds=0, **settings)


@pytest.fixture
def server():
    state = {"crossref": crossref(), "unpaywall": unpaywall(), "status": 200,
             "type": "application/json", "headers": {}}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers), self.headers.get("Content-Length")))
            provider = "crossref" if self.path.startswith("/works/") else "unpaywall"
            value = state[provider]
            raw = value if isinstance(value, bytes) else raw_json(value)
            self.send_response(state["status"])
            self.send_header("Content-Type", state["type"])
            self.send_header("Content-Length", str(len(raw)))
            for key, value in state["headers"].items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()

    def bridge(request, *, cancelled):
        # Explicit trusted TEST transport only. Production never accepts an
        # endpoint override or HTTP; its host and TLS context are checked below.
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            connection.request("GET", request.path, headers={"Host": request.host, "Accept": "application/json"})
            response = connection.getresponse()
            return MetadataResponse(response.status, tuple(response.getheaders()),
                                    response.read(request.max_response_bytes + 1), request.url)
        finally:
            connection.close()

    try:
        yield state, requests, bridge
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)


def test_disabled_by_default_never_opens_transport_or_network(monkeypatch):
    calls = []
    monkeypatch.setattr(http.client, "HTTPSConnection", lambda *a, **k: calls.append("https"))
    transport = Transport()
    instance = DoiMetadataClient(transport=transport)
    with pytest.raises(PermissionError, match="disabled"):
        instance.lookup(DOI)
    assert not calls and not transport.requests


@pytest.mark.parametrize("settings", [
    {"allow_network": 1}, {"providers": []}, {"providers": ("crossref", "crossref")},
    {"providers": ("https://elsewhere.example.org",)}, {"providers": "crossref"},
    {"providers": ({},)}, {"providers": ("unpaywall",)}, {"unpaywall_email": "private\r\nheader@example.org"},
    {"timeout": float("nan")}, {"timeout": 0}, {"timeout": 61}, {"max_response_bytes": 100},
    {"max_response_bytes": True}, {"cache_seconds": -1}, {"min_interval_seconds": float("inf")},
])
def test_invalid_configuration_is_rejected_without_network(settings):
    transport = Transport()
    with pytest.raises(ValueError):
        DoiMetadataClient(transport=transport, **settings)
    assert not transport.requests


@pytest.mark.parametrize(("given", "expected"), [
    ("  DOI:10.5555/Mixed.Case  ", "10.5555/mixed.case"),
    ("https://doi.org/10.5555%2FMixed.Case", "10.5555/mixed.case"),
    ("http://dx.doi.org/10.5555/Article(Part);A:B", "10.5555/article(part);a:b"),
    ("10.5555/suffix?literal#part", "10.5555/suffix?literal#part"),
    ("https://doi.org/10.5555/suffix%3Fliteral%23part", "10.5555/suffix?literal#part"),
    ("10.5555/100%25-source", "10.5555/100%25-source"),
    ("https://doi.org/10.5555/100%2525-source", "10.5555/100%25-source"),
    ("10.5555/СВІТЛО", "10.5555/світло"),
])
def test_identifier_normalization_preserves_meaning_and_punctuation(given, expected):
    assert normalize_doi(given) == expected


@pytest.mark.parametrize("given", [
    "https://doi.org.evil.example.org/10.5555/x", "https://email:secret@doi.org/10.5555/x",
    "https://doi.org:444/10.5555/x", "https://doi.org/10.5555/x?email=secret",
    "https://doi.org/10.5555/x#part", "https://doi.org/10.5555/%FF",
    "10.5555/../private", "10.5555/./private", "10.5555/a\\b", "10.5555/a\r\nInjected: yes",
    "10.5555/a b", "10.5555/", "10.55/x", "10.5555/\ud800", "10.5555/a\u200eb",
    "10.5555/" + "x" * 2048, "10.５５５５/x", None,
])
def test_invalid_or_ambiguous_identifiers_cannot_trigger_requests(given):
    transport = Transport()
    with pytest.raises(ValueError):
        client(transport).lookup(given)
    assert not transport.requests


def test_endpoint_quote_prevents_doi_query_path_and_host_injection():
    request = MetadataRequest("crossref", "10.5555/a?mailto=intruder@example.org#fragment")
    parsed = urlsplit(request.url)
    assert parsed.hostname == "api.crossref.org" and not parsed.query and not parsed.fragment
    assert request.path == "/works/10.5555%2Fa%3Fmailto%3Dintruder%40example.org%23fragment"
    assert "email=" not in request.url


def test_real_loopback_metadata_and_exact_bibliographic_mapping(server, tmp_path):
    state, requests, bridge = server
    before = raw_json(state["crossref"])
    preview = client(bridge).lookup("https://doi.org/" + DOI.upper())
    item = preview.csl_item
    assert item["type"] == "article-journal"
    assert item["title"] == "Дослідження світла: Перевірений опис"
    assert item["author"] == [{"family": "Коваленко", "given": "Олена"}, {"literal": "Synthetic Research Group"}]
    assert item["issued"] == {"date-parts": [[2026, 7]]}
    assert item["page"] == "234–245" and item["edition"] == "2"
    assert item["id"] == "doi-" + sha256(DOI.encode()).hexdigest()
    assert "abstract" not in item and "reference" not in item and "locator" not in item
    assert preview.review_required and not preview.from_cache
    assert preview.open_access is None and preview.oa_status is None
    assert len(preview.sources) == 1
    assert preview.sources[0].response_sha256 == sha256(before).hexdigest()
    assert preview.sources[0].response_bytes == len(before)
    assert preview.sources[0].endpoint == "https://api.crossref.org/works/10.5555%2Fsynthetic.source-2026"
    assert preview.sources[0].retrieved_at.endswith("+00:00")
    assert dict(preview.field_sources)["author"] == "crossref"
    assert preview.licenses[0].applies_to == "vor"
    assert preview.licenses[0].start_date == "2026-07-01T00:00:00Z"
    assert not preview.licenses[0].permission_verified
    assert preview.locations[0].version == "vor" and not preview.locations[0].permission_verified
    assert preview.locations[0].intended_application == "text-mining"
    assert any("extent" in warning for warning in preview.warnings)
    assert requests[0][0].startswith("/works/10.5555%2F") and requests[0][2] is None
    assert requests[0][1]["Host"] == "api.crossref.org"
    assert raw_json(state["crossref"]) == before
    assert not list(tmp_path.iterdir())


def test_preview_is_immutable_and_returns_independent_nested_csl_copies():
    preview = client(Transport()).lookup(DOI)
    with pytest.raises(FrozenInstanceError):
        preview.doi = "10.5555/changed"
    with pytest.raises(FrozenInstanceError):
        preview.licenses[0].permission_verified = True
    changed = preview.csl_item
    changed["author"][0]["family"] = "Changed"
    changed["issued"]["date-parts"][0].append(1)
    assert preview.csl_item["author"][0]["family"] == "Коваленко"
    assert preview.csl_item["issued"] == {"date-parts": [[2026, 7]]}
    snapshot = preview.to_dict()
    snapshot["csl_item"]["title"] = "Changed"
    assert preview.csl_item["title"].startswith("Дослідження")


def test_two_providers_share_stable_identity_and_email_stays_in_explicit_request_only(server):
    state, requests, bridge = server
    state["unpaywall"]["title"] = "Conflicting provider title"
    instance = client(bridge, providers=("unpaywall", "crossref"), unpaywall_email=EMAIL)
    preview = instance.lookup(DOI)
    assert len(requests) == 2
    assert parse_qs(urlsplit(requests[0][0]).query) == {"email": [EMAIL]}
    assert not urlsplit(requests[1][0]).query
    assert preview.csl_item["title"].startswith("Дослідження")
    assert preview.csl_item["author"][0]["family"] == "Коваленко"
    assert preview.csl_item["issued"] == {"date-parts": [[2026, 7]]}
    assert preview.open_access is True and preview.oa_status == "green"
    assert any("disagree on title" in warning for warning in preview.warnings)
    assert len(preview.sources) == 2
    output = json.dumps(preview.to_dict(), ensure_ascii=False)
    assert EMAIL not in output and "email=" not in output
    assert "Deprecated" not in output and "Excluded cited document" not in output
    assert all("?" not in source.endpoint for source in preview.sources)
    assert all(not row.permission_verified and row.review_required for row in preview.locations)


def test_unpaywall_alone_does_not_promote_stale_authors_to_verified_metadata():
    preview = client(Transport(unpaywall()), providers=("unpaywall",), unpaywall_email=EMAIL).lookup(DOI)
    assert "author" not in preview.csl_item
    assert preview.csl_item["issued"] == {"date-parts": [[2026, 7, 1]]}
    assert preview.licenses[0].identifier == "cc-by" and preview.licenses[0].url is None
    assert preview.locations[0].version == "acceptedVersion"
    assert any("deprecated author" in warning for warning in preview.warnings)
    assert any("oa_date" in warning for warning in preview.warnings)


@pytest.mark.parametrize("license_id", [None, "implied-oa"])
def test_unknown_or_implied_oa_license_never_becomes_permission(license_id):
    value = unpaywall()
    value["oa_locations"][0]["license"] = license_id
    preview = client(Transport(value), providers=("unpaywall",), unpaywall_email=EMAIL).lookup(DOI)
    assert preview.open_access is True and preview.review_required
    assert preview.locations[0].license == license_id and not preview.locations[0].permission_verified
    assert any("no explicit license" in warning for warning in preview.warnings)
    if license_id:
        assert preview.licenses[0].declaration == "implied"
    else:
        assert not preview.licenses


def test_embargoed_copy_stays_distinct_and_unknown_dates_are_not_used_as_expiry():
    row = unpaywall()["oa_locations"][0]
    row["is_best"] = False
    row["oa_date"] = "1900-01-01"
    value = unpaywall(is_oa=False, oa_status="closed", best_oa_location=None, oa_locations=[], oa_locations_embargoed=[row])
    preview = client(Transport(value), providers=("unpaywall",), unpaywall_email=EMAIL).lookup(DOI)
    assert preview.locations[0].embargoed and not preview.locations[0].permission_verified
    assert preview.open_access is False
    assert any("embargoed location" in warning for warning in preview.warnings)
    assert any("oa_date" in warning for warning in preview.warnings)


def test_missing_bibliography_and_rights_are_explicit_without_fabrication():
    transport = Transport(crossref(title=[], subtitle=[], author=[], issued={}, license=[], page=None))
    preview = client(transport).lookup(DOI)
    assert not {"title", "author", "issued", "page"} & set(preview.csl_item)
    assert not preview.licenses
    assert any("No title" in warning for warning in preview.warnings)
    assert any("No current author" in warning for warning in preview.warnings)
    assert any("No valid publication date" in warning for warning in preview.warnings)
    assert any("permissions are unknown" in warning for warning in preview.warnings)


def test_created_date_is_not_a_publication_date_and_article_number_is_not_a_page():
    value = crossref(issued=None, page=None, **{"article-number": "e309", "created": {"date-parts": [[2020, 1, 1]]}})
    item = client(Transport(value)).lookup(DOI).csl_item
    assert item["number"] == "e309" and "page" not in item and "issued" not in item


def test_invalid_date_uses_actual_alternative_without_filling_missing_parts():
    value = crossref(issued={"date-parts": [[2026, 2, 30]]}, published={"date-parts": [[2025]]})
    preview = client(Transport(value)).lookup(DOI)
    assert preview.csl_item["issued"] == {"date-parts": [[2025]]}
    assert any("invalid provider date" in warning for warning in preview.warnings)


def test_title_markup_is_plain_data_and_active_elements_do_not_survive():
    value = crossref(title=["A <i>small</i><br>study &amp; x &lt; y<script>unsafe()</script>"], subtitle=[])
    preview = client(Transport(value)).lookup(DOI)
    assert preview.csl_item["title"] == "A small study & x < y"
    assert "unsafe" not in json.dumps(preview.to_dict())
    assert any("markup" in warning for warning in preview.warnings)


@pytest.mark.parametrize("address", [
    "file:///private/document.pdf", "javascript:alert(1)", "data:application/pdf,abc", "ftp://publisher.example.org/doc",
    "https://person:secret@publisher.example.org/doc", "http://127.0.0.1/doc", "http://127.1/doc",
    "http://0x7f.0x0.0x0.0x1/doc", "http://169.254.169.254/latest/meta-data", "http://[::1]/doc",
    "http://localhost/doc", "http://host.local/doc", "http://metadata.internal/doc",
    "https://publisher.example.org:9999/doc", "https://publisher.example.org/with space", "https://bad..example.org/doc",
])
def test_unsafe_provider_links_are_not_exposed_as_acquisition_candidates(address):
    value = crossref(link=[{"URL": address}], license=[])
    preview = client(Transport(value)).lookup(DOI)
    assert not preview.locations
    assert address not in json.dumps(preview.to_dict())
    assert any("link was omitted" in warning for warning in preview.warnings)


@pytest.mark.parametrize(("value", "settings"), [
    (crossref(DOI="10.5555/different"), {}),
    (unpaywall(doi="10.5555/different"), {"providers": ("unpaywall",), "unpaywall_email": EMAIL}),
])
def test_returned_doi_must_match_exact_normalized_identity(value, settings):
    with pytest.raises(DoiMetadataError) as caught:
        client(Transport(value), **settings).lookup(DOI)
    assert caught.value.code == "doi_mismatch"


@pytest.mark.parametrize(("transport", "code"), [
    (Transport(status=301, headers={"Location": "https://elsewhere.example.org/?email=" + EMAIL}), "redirect"),
    (Transport(status=404), "not_found"), (Transport(status=503), "http_status"),
    (Transport(url="https://elsewhere.example.org/works/" + DOI), "destination"),
    (Transport(url="http://api.crossref.org/works/" + DOI), "destination"),
    (Transport(headers={"Content-Type": "text/html"}), "content_type"),
    (Transport(headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}), "content_type"),
    (Transport(headers={"Content-Type": "application/json", "Content-Length": "9000000"}), "response_size"),
    (Transport(headers={"Content-Type": "application/json", "Content-Length": "3"}), "invalid_response"),
    (Transport(headers=(("Content-Type", "application/json"), ("content-type", "text/html"))), "invalid_response"),
    (Transport(headers={"Content-Type": "application/json", "X-Header": "private\r\nsecret"}), "invalid_response"),
    (Transport(b'{"message":NaN}'), "invalid_response"), (Transport(b'{"message":1,"message":2}'), "invalid_response"),
    (Transport(b'{"message":"\xff"}'), "invalid_response"), (Transport(b'[]'), "invalid_response"),
    (Transport(b'{"message":"\\ud800"}'), "invalid_response"),
    (Transport(crossref(license={})), "invalid_response"),
    (Transport(crossref(link=False)), "invalid_response"),
    (Transport(crossref(author=["no object"])), "invalid_response"),
])
def test_protocol_and_schema_failures_are_bounded_and_redacted(transport, code):
    with pytest.raises(DoiMetadataError) as caught:
        client(transport).lookup(DOI)
    assert caught.value.code == code
    assert EMAIL not in str(caught.value) and "secret" not in str(caught.value)
    assert len(transport.requests) == 1


def test_actual_loopback_redirect_is_not_followed_and_error_body_not_used(server):
    state, requests, bridge = server
    state.update(status=302, headers={"Location": "https://elsewhere.example.org/?email=" + EMAIL})
    state["crossref"] = {"error": EMAIL}
    with pytest.raises(DoiMetadataError) as caught:
        client(bridge).lookup(DOI)
    assert caught.value.code == "redirect" and len(requests) == 1
    assert EMAIL not in str(caught.value)


def test_actual_loopback_oversize_response_is_rejected(server):
    state, requests, bridge = server
    state["crossref"] = b" " * 5000
    with pytest.raises(DoiMetadataError) as caught:
        client(bridge, max_response_bytes=1024).lookup(DOI)
    assert caught.value.code == "response_size" and len(requests) == 1


def test_json_nesting_limit_is_checked_even_when_byte_budget_is_small():
    value = {"x": 1}
    for _ in range(30):
        value = {"x": value}
    with pytest.raises(DoiMetadataError) as caught:
        client(Transport(value)).lookup(DOI)
    assert caught.value.code == "response_size"


def test_cache_deduplicates_doi_variants_and_refresh_is_an_explicit_second_call():
    transport = Transport()
    instance = client(transport)
    first = instance.lookup(DOI)
    cached = instance.lookup("https://doi.org/" + DOI.upper())
    assert len(transport.requests) == 1 and cached.from_cache and not first.from_cache
    assert first.reference_id == cached.reference_id and first.sources == cached.sources
    refreshed = instance.lookup(DOI, refresh=True)
    assert len(transport.requests) == 2 and not refreshed.from_cache
    instance.clear_cache()
    instance.lookup(DOI)
    assert len(transport.requests) == 3


def test_network_disable_applies_even_if_a_cached_preview_exists():
    transport = Transport()
    instance = client(transport)
    instance.lookup(DOI)
    instance.allow_network = False
    with pytest.raises(PermissionError):
        instance.lookup(DOI)
    assert len(transport.requests) == 1


def test_rate_limit_has_cooldown_and_never_retries_automatically():
    transport = Transport(status=429, headers={"Retry-After": "30"})
    instance = client(transport)
    for _ in range(2):
        with pytest.raises(DoiMetadataError) as caught:
            instance.lookup(DOI)
        assert caught.value.code == "rate_limited"
        assert 1 <= caught.value.retry_after_seconds <= 30
    assert len(transport.requests) == 1


def test_exception_and_request_repr_never_disclose_configured_email():
    request = MetadataRequest("unpaywall", DOI, email=EMAIL)
    assert EMAIL not in repr(request) and "email=" not in request.public_url

    def failing(request, *, cancelled):
        raise OSError("Reflected private URL: " + request.url)

    with pytest.raises(DoiMetadataError) as caught:
        client(failing, providers=("unpaywall",), unpaywall_email=EMAIL).lookup(DOI)
    assert caught.value.code == "connection"
    assert EMAIL not in str(caught.value) and "api.unpaywall.org" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_falsey_explicit_transport_is_not_replaced_with_a_real_network_call(monkeypatch):
    class FalseyTransport(Transport):
        def __bool__(self):
            return False

    def disallowed(*args, **kwargs):
        raise AssertionError("The default network transport must not be used.")

    monkeypatch.setattr(module, "_worker_transport", disallowed)
    transport = FalseyTransport()
    assert client(transport).lookup(DOI).doi == DOI
    assert len(transport.requests) == 1


def test_cancel_before_request_and_between_providers_prevents_next_transmission():
    transport = Transport()
    with pytest.raises(InterruptedError):
        client(transport).lookup(DOI, cancelled=lambda: True)
    assert not transport.requests
    cancelled = [False]

    def first_only(request, *, cancelled):
        result = transport(request, cancelled=cancelled)
        outer_cancelled[0] = True
        return result

    outer_cancelled = cancelled
    with pytest.raises(InterruptedError):
        client(first_only, providers=("crossref", "unpaywall"), unpaywall_email=EMAIL).lookup(DOI, cancelled=lambda: cancelled[0])
    assert len(transport.requests) == 1 and transport.requests[0].provider == "crossref"


def test_default_https_worker_keeps_exact_host_tls_and_never_follows_redirect(monkeypatch):
    calls = []
    body_reads = []

    class Response:
        status = 302

        def getheaders(self):
            return [("Location", "http://127.0.0.1/private"), ("Content-Length", "999999999")]

        def read1(self, *_):
            body_reads.append(True)
            raise AssertionError("An error body must not be read.")

    class Connection:
        sock = None

        def __init__(self, host, *, port, timeout, context):
            calls.append((host, port, timeout, context))

        def request(self, method, route, *, headers):
            calls.append((method, route, headers))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.org")
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    request = MetadataRequest("unpaywall", DOI, email=EMAIL)
    response = module._https_once(request)
    assert response.status == 302 and not response.body and not body_reads
    host, port, timeout, context = calls[0]
    assert host == "api.unpaywall.org" and port == 443
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert calls[1][0] == "GET" and calls[1][2]["Accept-Encoding"] == "identity"
    assert "Authorization" not in calls[1][2] and calls[-1] == "closed"
    assert len([entry for entry in calls if isinstance(entry, tuple) and entry[0] == "GET"]) == 1


def test_actual_isolated_worker_rejects_invalid_envelope_without_echoing_email():
    completed = subprocess.run([sys.executable, "-I", str(Path(module.__file__)), "--metadata-worker"],
        input=raw_json({"provider": "https://unexpected.example.org", "email": EMAIL}),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=True)
    assert json.loads(completed.stdout) == {"error": "connection"}
    assert EMAIL.encode() not in completed.stdout + completed.stderr


def test_worker_command_distinguishes_source_python_and_frozen_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert module._worker_argv() == [sys.executable, "-I", str(Path(module.__file__).resolve()), "--metadata-worker"]
    monkeypatch.setattr(sys, "frozen", True)
    incoming, reply = tmp_path / "request.json", tmp_path / "reply.json"
    assert module._worker_argv(incoming, reply) == [sys.executable, "--carr-doi-metadata-worker", str(incoming), str(reply)]
    assert EMAIL not in repr(module._worker_argv(incoming, reply))
    with pytest.raises(ValueError, match="windowed worker"):
        module._worker_argv()


def test_file_worker_runs_without_standard_streams_and_never_overwrites_reply(tmp_path, monkeypatch):
    incoming, reply = tmp_path / "request.json", tmp_path / "reply.json"
    request = MetadataRequest("unpaywall", DOI, email=EMAIL)
    module._private_write(incoming, raw_json(asdict(request)))
    raw = raw_json(unpaywall())
    monkeypatch.setattr(module, "_https_once", lambda value: MetadataResponse(200, {"Content-Type": "application/json"}, raw, value.url))
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdin", None)
        streams.setattr(sys, "stdout", None)
        streams.setattr(sys, "stderr", None)
        status = module._worker_main([str(incoming), str(reply)])
    assert status == 0
    result = json.loads(reply.read_bytes())
    assert base64.b64decode(result["body"]) == raw
    assert EMAIL.encode() not in reply.read_bytes()
    before = reply.read_bytes()
    assert module._worker_main([str(incoming), str(reply)]) == 2
    assert reply.read_bytes() == before
    if os.name != "nt":
        assert incoming.stat().st_mode & 0o777 == reply.stat().st_mode & 0o777 == 0o600


def test_frozen_transport_uses_private_files_and_removes_them_after_completion(monkeypatch):
    paths = []
    request = MetadataRequest("unpaywall", DOI, email=EMAIL)
    raw = raw_json(unpaywall())

    class Process:
        returncode = 0

        def __init__(self, command, **settings):
            assert command[1] == "--carr-doi-metadata-worker" and len(command) == 4
            assert EMAIL not in repr(command)
            assert settings["stdin"] == settings["stdout"] == subprocess.DEVNULL
            incoming, reply = map(Path, command[2:])
            paths.extend((incoming, reply))
            assert json.loads(module._private_read(incoming, 8192))["email"] == EMAIL
            module._private_write(reply, raw_json({"status": 200, "headers": [["Content-Type", "application/json"]],
                                                   "body": base64.b64encode(raw).decode()}))

        def communicate(self, data=None, timeout=None):
            assert data is None
            return None, None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    response = module._worker_transport(request, cancelled=lambda: False)
    assert response.body == raw and len(paths) == 2
    assert all(not path.exists() and not path.parent.exists() for path in paths)


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_worker_deadline_and_cancellation_reap_blocked_process(tmp_path, monkeypatch, cancel):
    worker = tmp_path / "blocked.py"
    worker.write_text("import sys, time\nsys.stdin.buffer.read()\ntime.sleep(20)\n", encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(worker))
    original = subprocess.Popen
    processes = []

    def spawn(*args, **kwargs):
        assert args[0][1] == "-I" and EMAIL not in repr(args[0])
        assert "HTTPS_PROXY" not in kwargs["env"] and "SSLKEYLOGFILE" not in kwargs["env"]
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    began = time.monotonic()
    request = MetadataRequest("unpaywall", DOI, timeout=5 if cancel else .2, email=EMAIL)
    with pytest.raises(InterruptedError if cancel else DoiMetadataError) as caught:
        module._worker_transport(request, cancelled=lambda: cancel and time.monotonic() - began > .12)
    if not cancel:
        assert caught.value.code == "timeout"
    assert time.monotonic() - began < 2
    assert len(processes) == 1 and processes[0].poll() is not None


def test_cooldown_uses_integer_time_at_a_floating_point_boundary(monkeypatch):
    from carr_library.academic_integration import doi as module
    now = [27.420876065277096]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'monotonic_ns', lambda: round(now[0] * 1_000_000_000))
    transport = Transport(status=429, headers={"Retry-After": "30"})
    instance = client(transport)
    for _ in range(2):
        with pytest.raises(DoiMetadataError) as caught:
            instance.lookup(DOI)
        assert caught.value.retry_after_seconds == 30
    assert len(transport.requests) == 1
    now[0] += 29.5
    with pytest.raises(DoiMetadataError) as caught:
        instance.lookup(DOI)
    assert caught.value.retry_after_seconds == 1
    assert len(transport.requests) == 1
    now[0] += 0.5
    with pytest.raises(DoiMetadataError):
        instance.lookup(DOI)
    assert len(transport.requests) == 2
