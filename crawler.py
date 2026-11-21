"""
crawler.py — Backend crawling, sitemap parsing, URL normalization,
strict hostname scoping, and multi-threaded HTTP link checking.
"""

from __future__ import annotations

import gzip
import re
import threading
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

SKIP_SCHEMES = {
    "mailto",
    "tel",
    "javascript",
    "data",
    "sms",
    "whatsapp",
    "blob",
    "about",
    "ftp",
    "file",
    "skype",
}

SKIP_EXTENSIONS = {
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff", ".tif", ".avif",
    # Documents & archives
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf",
    # Audio & video
    ".mp4", ".mp3", ".wav", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ogg", ".m4a", ".m4v", ".mkv",
    # Static web/app assets
    ".css", ".js", ".mjs", ".map", ".json", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".exe", ".dmg", ".pkg", ".apk", ".ipa", ".iso", ".bin",
}

TRACKING_PARAM_EXACT = {
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "dclid",
    "ttclid",
    "yclid",
    "_ga",
    "_gl",
    "mc_cid",
    "mc_eid",
}

TRACKING_PARAM_PREFIXES = ("utm_",)

REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

DEFAULT_SOCIAL_MEDIA_DOMAINS: set[str] = {
    "facebook.com",
    "fb.com",
    "fb.me",
    "instagram.com",
    "instagr.am",
    "twitter.com",
    "x.com",
    "t.co",
    "tiktok.com",
    "linkedin.com",
    "lnkd.in",
    "youtube.com",
    "youtu.be",
    "line.me",
    "lin.ee",
    "threads.net",
    "threads.com",
    "pinterest.com",
    "pin.it",
    "whatsapp.com",
    "wa.me",
    "t.me",
    "telegram.me",
    "telegram.org",
    "snapchat.com",
    "reddit.com",
    "redd.it",
    "discord.gg",
    "discord.com",
    "wechat.com",
    "weixin.qq.com",
    "weibo.com",
    "tumblr.com",
    "vk.com",
    "flickr.com",
    "vimeo.com",
}

ProgressCallback = Optional[Callable[[str, float], None]]

_thread_local = threading.local()


def _get_session() -> requests.Session:
    """Return a thread-local requests.Session for safe connection pooling."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


@dataclass
class ExtractedLink:
    source_page: str
    anchor_text: str
    url: str
    link_type: str  # "Internal" or "External"


@dataclass
class LinkCheckResult:
    url: str
    status_code: Optional[int]
    error: str
    is_broken: bool
    is_redirect: bool
    redirect_status: str  # e.g. "301" or "301 -> 302"
    final_url: str


@dataclass
class BrokenLinkIssue:
    source_page: str
    anchor_text: str
    broken_url: str
    link_type: str
    status_code: Optional[int]
    status_category: str
    status_or_error: str
    screenshot_path: str = ""


@dataclass
class RedirectIssue:
    source_page: str
    anchor_text: str
    original_url: str
    link_type: str
    status_code: str
    redirect_category: str
    final_destination_url: str


@dataclass
class CrawlSummary:
    scan_mode: str
    target_hostname: str
    pages_scanned: int
    scanned_pages: list[str]
    manual_input_urls: list[str]
    total_links_checked: int
    extracted_links: list[ExtractedLink]
    broken_links: list[BrokenLinkIssue]
    redirected_links: list[RedirectIssue]
    skipped_social_count: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# URL Normalization, Scope & Safety Helpers
# ---------------------------------------------------------------------------

def ensure_scheme(url: str) -> str:
    """Ensure a URL has an http/https scheme."""
    cleaned = url.strip()
    if not cleaned:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned):
        cleaned = "https://" + cleaned
    return cleaned


def get_strict_hostname(url: str) -> str:
    """Return lowercase hostname without port (e.g. 'siamatsiam.com')."""
    parsed = urlparse(ensure_scheme(url))
    return (parsed.hostname or "").lower()


def is_internal_url(url: str, target_hostname: str) -> bool:
    """
    Strict hostname comparison:
    https://siamatsiam.com != https://shop.siamatsiam.com
    """
    if not target_hostname:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host == target_hostname.lower()


def _is_tracking_param(key: str) -> bool:
    key_lower = key.lower()
    if key_lower in TRACKING_PARAM_EXACT:
        return True
    return any(key_lower.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)


def strip_tracking_params(url: str) -> str:
    """
    Remove URL fragments and tracking query parameters (utm_*, fbclid, gclid, etc.)
    while preserving the path structure for accurate HTTP status / redirect checks.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = hostname
    elif port:
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_pairs = [(k, v) for k, v in query_pairs if not _is_tracking_param(k)]
    filtered_pairs.sort(key=lambda item: (item[0].lower(), item[1]))
    new_query = urlencode(filtered_pairs, doseq=True)

    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, "", new_query, ""))


def canonicalize_url_for_dedup(url: str) -> str:
    """
    Produce a canonical deduplication key:
    - strips fragments & tracking params
    - normalizes scheme, host, default port
    - normalizes trailing slash on non-root paths
    """
    cleaned = strip_tracking_params(url)
    parsed = urlparse(cleaned)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def append_query_params(url: str, query_string: str) -> str:
    """Append or overwrite query parameters (e.g. gclid & utm params) cleanly onto a URL."""
    raw_qs = query_string.strip().lstrip("?")
    if not raw_qs:
        return url

    parsed = urlparse(ensure_scheme(url))
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    extra = parse_qsl(raw_qs, keep_blank_values=True)
    for k, v in extra:
        existing[k] = v

    new_query = urlencode(list(existing.items()), doseq=True)
    path = parsed.path or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, new_query, parsed.fragment))


def should_skip_url(raw_href: str) -> bool:
    """
    Return True if href is empty, fragment-only, non-HTTP scheme,
    or points to a skipped media/document/asset file extension.
    """
    if not raw_href:
        return True

    href = raw_href.strip()
    if not href or href.startswith("#"):
        return True

    parsed = urlparse(href)
    scheme = parsed.scheme.lower()
    if scheme:
        if scheme in SKIP_SCHEMES or scheme not in ("http", "https"):
            return True

    path_lower = (parsed.path or "").lower()
    for ext in SKIP_EXTENSIONS:
        if path_lower.endswith(ext):
            return True

    return False


def parse_domain_list(raw_text: str) -> set[str]:
    """Parse comma/newline/space-separated domain names or URLs into normalized hostnames."""
    domains: set[str] = set()
    for token in re.split(r"[,\s]+", raw_text or ""):
        cleaned = token.strip().lower()
        if not cleaned:
            continue
        if "://" in cleaned:
            cleaned = (urlparse(cleaned).hostname or "").lower()
        else:
            cleaned = cleaned.split("/", 1)[0].split(":", 1)[0]
        if cleaned.startswith("www.") and cleaned.count(".") >= 2:
            cleaned = cleaned[4:]
        if cleaned:
            domains.add(cleaned)
    return domains


def is_social_media_url(url: str, social_domains: Optional[set[str]] = None) -> bool:
    """Return True if the URL hostname belongs to a social media / ignored domain or its subdomains."""
    domains = social_domains if social_domains is not None else DEFAULT_SOCIAL_MEDIA_DOMAINS
    if not domains:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == dom or host.endswith(f".{dom}") for dom in domains)


def classify_broken_status(status_code: Optional[int], error_text: str = "") -> str:
    """Classify an HTTP failure or network error into a clear, filterable status category."""
    if status_code == 404:
        return "404 Not Found"
    if status_code == 410:
        return "410 Gone"
    if status_code is not None and 500 <= status_code < 600:
        return "5xx Server Error"
    if status_code in (401, 403, 429, 999):
        return "Blocked / WAF (401/403/429)"
    if status_code is not None and 400 <= status_code < 500:
        return "Other 4xx Client Error"

    err_lower = (error_text or "").lower()
    if "timeout" in err_lower:
        return "Timeout"
    if "redirect" in err_lower:
        return "Redirect Loop"
    return "Connection / SSL Error"


def classify_redirect_status(redirect_status: str) -> str:
    """Classify redirect status string into a clear, filterable redirect category."""
    code_str = (redirect_status or "").strip()
    if "->" in code_str:
        return "Multi-Hop Chain"
    if code_str == "Redirect Loop":
        return "Redirect Loop"
    mapping = {
        "301": "301 Permanent",
        "302": "302 Temporary",
        "303": "303 See Other",
        "307": "307 Temporary Redirect",
        "308": "308 Permanent Redirect",
    }
    return mapping.get(code_str, f"Other Redirect ({code_str})" if code_str else "Redirect")


def _is_www_equivalent(host_a: str, host_b: str) -> bool:
    """True only if one host is exact 'www.' prefix of the other."""
    a = host_a.lower()
    b = host_b.lower()
    return a == f"www.{b}" or b == f"www.{a}"


def _format_status_error(status_code: int) -> str:
    reason_map = {
        400: "400 Bad Request",
        401: "401 Unauthorized",
        403: "403 Forbidden (Possible Bot/WAF Block)",
        404: "404 Not Found",
        405: "405 Method Not Allowed",
        408: "408 Request Timeout",
        410: "410 Gone",
        429: "429 Too Many Requests",
        451: "451 Unavailable For Legal Reasons",
        500: "500 Internal Server Error",
        502: "502 Bad Gateway",
        503: "503 Service Unavailable",
        504: "504 Gateway Timeout",
    }
    return reason_map.get(status_code, f"HTTP {status_code}")


def _build_check_result_from_response(original_url: str, resp: requests.Response) -> LinkCheckResult:
    history_codes = [r.status_code for r in resp.history if r.status_code in REDIRECT_STATUS_CODES]
    is_redirect = len(history_codes) > 0
    redirect_status = " -> ".join(str(code) for code in history_codes) if is_redirect else ""
    final_url = resp.url or original_url
    status_code = resp.status_code
    is_broken = status_code >= 400
    error = _format_status_error(status_code) if is_broken else ""

    return LinkCheckResult(
        url=original_url,
        status_code=status_code,
        error=error,
        is_broken=is_broken,
        is_redirect=is_redirect,
        redirect_status=redirect_status,
        final_url=final_url,
    )


# ---------------------------------------------------------------------------
# HTML Link Extraction
# ---------------------------------------------------------------------------

def _extract_anchor_text(tag) -> str:
    """Extract clean human-readable anchor text with accessible fallbacks."""
    text = " ".join(tag.get_text(" ", strip=True).split())
    if text:
        return text[:200]
    for attr in ("aria-label", "title"):
        val = tag.get(attr)
        if isinstance(val, str) and val.strip():
            return " ".join(val.split())[:200]
    img = tag.find("img", alt=True)
    if img and isinstance(img.get("alt"), str) and img["alt"].strip():
        return f"[IMG] {' '.join(img['alt'].split())[:180]}"
    return "(No Text)"


def extract_links_from_html(
    html_bytes: bytes,
    source_page_url: str,
    target_hostname: str,
) -> list[ExtractedLink]:
    """Parse HTML and extract all valid <a href='...'> links with anchor text."""
    soup = BeautifulSoup(html_bytes, "html.parser")

    # Respect <base href="..."> if present
    base_url = source_page_url
    base_tag = soup.find("base", href=True)
    if base_tag and base_tag.get("href"):
        base_url = urljoin(source_page_url, base_tag["href"].strip())

    links: list[ExtractedLink] = []
    seen_on_page: set[tuple[str, str]] = set()

    for a_tag in soup.find_all("a", href=True):
        raw_href = (a_tag.get("href") or "").strip()
        if should_skip_url(raw_href):
            continue

        resolved = urljoin(base_url, raw_href)
        if should_skip_url(resolved):
            continue

        parsed = urlparse(resolved)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            continue

        # Strip fragment but keep original path & non-tracking params
        clean_url = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        ))

        anchor_text = _extract_anchor_text(a_tag)
        dedup_key = (strip_tracking_params(clean_url), anchor_text.lower())
        if dedup_key in seen_on_page:
            continue
        seen_on_page.add(dedup_key)

        link_type = "Internal" if is_internal_url(clean_url, target_hostname) else "External"
        links.append(
            ExtractedLink(
                source_page=source_page_url,
                anchor_text=anchor_text,
                url=clean_url,
                link_type=link_type,
            )
        )

    return links


# ---------------------------------------------------------------------------
# Fast HTTP Page Fetching & Multi-threaded Link Checking
# ---------------------------------------------------------------------------

def _fetch_page_html(
    url: str,
    timeout: int = 10,
) -> tuple[LinkCheckResult, Optional[bytes], str]:
    """
    Fetch a webpage for crawling/parsing, returning:
    (LinkCheckResult, html_bytes_or_None, effective_final_url)
    """
    session = _get_session()
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        result = _build_check_result_from_response(url, resp)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        is_html = ("text/html" in content_type) or ("application/xhtml+xml" in content_type) or (content_type == "")
        html_bytes = resp.content if (resp.status_code < 400 and is_html) else None
        return result, html_bytes, (resp.url or url)
    except requests.exceptions.Timeout:
        return (
            LinkCheckResult(url, None, f"Timeout ({timeout}s)", True, False, "", url),
            None,
            url,
        )
    except requests.exceptions.TooManyRedirects:
        return (
            LinkCheckResult(url, None, "Too Many Redirects", True, True, "Redirect Loop", url),
            None,
            url,
        )
    except requests.exceptions.SSLError as exc:
        return (
            LinkCheckResult(url, None, f"SSL Error: {str(exc)[:120]}", True, False, "", url),
            None,
            url,
        )
    except requests.exceptions.ConnectionError:
        return (
            LinkCheckResult(url, None, "Connection Error", True, False, "", url),
            None,
            url,
        )
    except requests.exceptions.RequestException as exc:
        return (
            LinkCheckResult(url, None, f"Request Error: {str(exc)[:120]}", True, False, "", url),
            None,
            url,
        )


def check_single_url(url: str, timeout: int = 10) -> LinkCheckResult:
    """
    Check HTTP status code & redirect history using streaming GET
    (reads headers without downloading full response body; avoids HEAD 405/403 false positives).
    """
    session = _get_session()
    try:
        with session.get(url, timeout=timeout, allow_redirects=True, stream=True) as resp:
            return _build_check_result_from_response(url, resp)
    except requests.exceptions.Timeout:
        return LinkCheckResult(url, None, f"Timeout ({timeout}s)", True, False, "", url)
    except requests.exceptions.TooManyRedirects:
        return LinkCheckResult(url, None, "Too Many Redirects", True, True, "Redirect Loop", url)
    except requests.exceptions.SSLError as exc:
        return LinkCheckResult(url, None, f"SSL Error: {str(exc)[:120]}", True, False, "", url)
    except requests.exceptions.ConnectionError:
        return LinkCheckResult(url, None, "Connection Error", True, False, "", url)
    except requests.exceptions.RequestException as exc:
        return LinkCheckResult(url, None, f"Request Error: {str(exc)[:120]}", True, False, "", url)


def check_links_concurrently(
    links: list[ExtractedLink],
    check_external: bool = True,
    ignore_social_media: bool = True,
    ignored_domains: Optional[set[str]] = None,
    max_workers: int = 12,
    timeout: int = 10,
    prefetched_cache: Optional[dict[str, LinkCheckResult]] = None,
    progress_cb: ProgressCallback = None,
) -> tuple[list[BrokenLinkIssue], list[RedirectIssue], int, int]:
    """
    Check all extracted links concurrently using ThreadPoolExecutor.
    Deduplicates HTTP requests by tracking-stripped URL for high speed.
    Returns (broken_issues, redirect_issues, total_links_checked, skipped_social_count).
    """
    social_domains = ignored_domains if ignored_domains is not None else DEFAULT_SOCIAL_MEDIA_DOMAINS
    eligible_links = [
        lnk for lnk in links if check_external or lnk.link_type == "Internal"
    ]

    filtered_links: list[ExtractedLink] = []
    skipped_social_count = 0
    for lnk in eligible_links:
        if ignore_social_media and is_social_media_url(lnk.url, social_domains):
            skipped_social_count += 1
            continue
        filtered_links.append(lnk)

    if not filtered_links:
        return [], [], 0, skipped_social_count

    cache: dict[str, LinkCheckResult] = dict(prefetched_cache or {})

    # Map normalized check key -> first representative URL to request
    urls_to_check: dict[str, str] = {}
    for lnk in filtered_links:
        key = strip_tracking_params(lnk.url)
        if key not in cache and key not in urls_to_check:
            urls_to_check[key] = key

    total_to_fetch = len(urls_to_check)
    if total_to_fetch > 0:
        completed = 0
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 20))) as executor:
            future_map = {
                executor.submit(check_single_url, req_url, timeout): key
                for key, req_url in urls_to_check.items()
            }
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    cache[key] = future.result()
                except Exception as exc:
                    cache[key] = LinkCheckResult(key, None, f"Error: {str(exc)[:120]}", True, False, "", key)
                completed += 1
                if progress_cb:
                    progress_cb(
                        f"กำลังตรวจสอบสถานะลิงก์ ({completed}/{total_to_fetch} URL ที่ไม่ซ้ำ)...",
                        completed / total_to_fetch,
                    )

    broken_issues: list[BrokenLinkIssue] = []
    redirect_issues: list[RedirectIssue] = []

    seen_broken: set[tuple[str, str, str]] = set()
    seen_redirect: set[tuple[str, str, str]] = set()

    for lnk in filtered_links:
        key = strip_tracking_params(lnk.url)
        res = cache.get(key)
        if not res:
            continue

        if res.is_redirect:
            r_sig = (lnk.source_page, lnk.url, res.final_url)
            if r_sig not in seen_redirect:
                seen_redirect.add(r_sig)
                redirect_issues.append(
                    RedirectIssue(
                        source_page=lnk.source_page,
                        anchor_text=lnk.anchor_text,
                        original_url=lnk.url,
                        link_type=lnk.link_type,
                        status_code=res.redirect_status,
                        redirect_category=classify_redirect_status(res.redirect_status),
                        final_destination_url=res.final_url,
                    )
                )

        if res.is_broken:
            # If an internal/short link legitimately redirected to a social media URL
            # and the social platform blocked our bot (400/401/403/429/999), do not flag as broken.
            if (
                ignore_social_media
                and res.is_redirect
                and is_social_media_url(res.final_url, social_domains)
                and res.status_code in (400, 401, 403, 429, 999)
            ):
                continue

            b_sig = (lnk.source_page, lnk.url, res.error)
            if b_sig not in seen_broken:
                seen_broken.add(b_sig)
                err_msg = res.error or f"HTTP {res.status_code}"
                broken_issues.append(
                    BrokenLinkIssue(
                        source_page=lnk.source_page,
                        anchor_text=lnk.anchor_text,
                        broken_url=lnk.url,
                        link_type=lnk.link_type,
                        status_code=res.status_code,
                        status_category=classify_broken_status(res.status_code, err_msg),
                        status_or_error=err_msg,
                    )
                )

    return broken_issues, redirect_issues, len(filtered_links), skipped_social_count


# ---------------------------------------------------------------------------
# Sitemap Discovery & Parsing
# ---------------------------------------------------------------------------

def _local_tag(tag_name: str) -> str:
    """Strip XML namespace from an ElementTree tag name."""
    if "}" in tag_name:
        return tag_name.split("}", 1)[1].lower()
    return tag_name.lower()


def _fetch_xml_bytes(url: str, timeout: int = 10) -> Optional[bytes]:
    session = _get_session()
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code >= 400:
            return None
        data = resp.content
        if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return data
    except requests.exceptions.RequestException:
        return None


def _discover_sitemaps_from_robots(origin: str, timeout: int = 10) -> list[str]:
    session = _get_session()
    sitemaps: list[str] = []
    try:
        resp = session.get(f"{origin}/robots.txt", timeout=timeout, allow_redirects=True)
        if resp.status_code < 400:
            for line in resp.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm_url = line.split(":", 1)[1].strip()
                    if sm_url:
                        sitemaps.append(sm_url)
    except requests.exceptions.RequestException:
        pass
    return sitemaps


def discover_and_parse_sitemap(
    sitemap_input: str,
    max_pages: int = 150,
    timeout: int = 10,
    progress_cb: ProgressCallback = None,
) -> tuple[list[str], str, list[str]]:
    """
    Parse URLs from a sitemap.xml (or auto-discover /sitemap.xml from a site URL).
    Supports <urlset> and nested <sitemapindex>, plus .xml.gz.
    Enforces strict hostname scope based on the input URL's hostname.
    """
    normalized_input = ensure_scheme(sitemap_input)
    parsed = urlparse(normalized_input)
    target_hostname = (parsed.hostname or "").lower()
    origin = f"{parsed.scheme}://{parsed.netloc}"
    warnings: list[str] = []

    candidate_sitemaps: list[str] = []
    lower_path = (parsed.path or "").lower()
    if lower_path.endswith(".xml") or lower_path.endswith(".xml.gz") or "sitemap" in lower_path:
        candidate_sitemaps.append(normalized_input)
    else:
        candidate_sitemaps.append(f"{origin}/sitemap.xml")
        candidate_sitemaps.append(f"{origin}/sitemap_index.xml")

    queue: deque[str] = deque()
    for cand in candidate_sitemaps:
        data = _fetch_xml_bytes(cand, timeout=timeout)
        if data:
            try:
                ET.fromstring(data)
                queue.append(cand)
                if cand != normalized_input:
                    warnings.append(f"ค้นพบ Sitemap อัตโนมัติที่: {cand}")
                break
            except ET.ParseError:
                continue

    if not queue:
        robots_sitemaps = _discover_sitemaps_from_robots(origin, timeout=timeout)
        for sm in robots_sitemaps:
            queue.append(sm)
            warnings.append(f"ค้นพบ Sitemap จาก robots.txt: {sm}")

    if not queue:
        warnings.append(f"ไม่พบไฟล์ sitemap.xml ที่ถูกต้องสำหรับ {sitemap_input}")
        return [], target_hostname, warnings

    visited_sitemaps: set[str] = set()
    seen_page_keys: set[str] = set()
    page_urls: list[str] = []
    adopted_www = False

    while queue and len(page_urls) < max_pages:
        sm_url = queue.popleft()
        if sm_url in visited_sitemaps:
            continue
        visited_sitemaps.add(sm_url)

        if progress_cb:
            progress_cb(
                f"กำลังอ่าน Sitemap ({len(page_urls)}/{max_pages} หน้า): {sm_url}",
                min(0.95, len(page_urls) / max(1, max_pages)),
            )

        xml_bytes = _fetch_xml_bytes(sm_url, timeout=timeout)
        if not xml_bytes:
            warnings.append(f"ไม่สามารถดาวน์โหลด Sitemap ได้: {sm_url}")
            continue

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            warnings.append(f"ไฟล์ XML ไม่ถูกต้อง: {sm_url}")
            continue

        root_tag = _local_tag(root.tag)
        if root_tag == "sitemapindex":
            for elem in root:
                if _local_tag(elem.tag) == "sitemap":
                    for child in elem:
                        if _local_tag(child.tag) == "loc" and child.text:
                            child_sm = child.text.strip()
                            if child_sm not in visited_sitemaps:
                                queue.append(child_sm)
        elif root_tag == "urlset":
            for elem in root:
                if len(page_urls) >= max_pages:
                    break
                if _local_tag(elem.tag) == "url":
                    for child in elem:
                        if _local_tag(child.tag) == "loc" and child.text:
                            loc_url = child.text.strip()
                            if should_skip_url(loc_url):
                                continue
                            loc_host = get_strict_hostname(loc_url)
                            # Allow exact www canonicalization if user omitted/added www
                            if not adopted_www and loc_host and loc_host != target_hostname and _is_www_equivalent(loc_host, target_hostname):
                                target_hostname = loc_host
                                adopted_www = True
                                warnings.append(
                                    f"URL ใน Sitemap ใช้ Hostname '{target_hostname}' ระบบจึงปรับขอบเขต Strict Hostname ให้ตรงกัน"
                                )
                            if not is_internal_url(loc_url, target_hostname):
                                continue
                            dedup_key = canonicalize_url_for_dedup(loc_url)
                            if dedup_key not in seen_page_keys:
                                seen_page_keys.add(dedup_key)
                                page_urls.append(loc_url)

    return page_urls, target_hostname, warnings


# ---------------------------------------------------------------------------
# Scan Mode Runners
# ---------------------------------------------------------------------------

def run_auto_crawl(
    start_url: str,
    max_depth: int = 3,
    max_pages: int = 150,
    check_external: bool = True,
    ignore_social_media: bool = True,
    ignored_domains: Optional[set[str]] = None,
    max_workers: int = 12,
    timeout: int = 10,
    progress_cb: ProgressCallback = None,
) -> CrawlSummary:
    """
    Mode 1: Auto-Crawl Mode
    BFS crawl starting from start_url within strict hostname scope up to max_depth & max_pages.
    """
    seed_url = ensure_scheme(start_url)
    target_hostname = get_strict_hostname(seed_url)
    warnings: list[str] = []

    visited_keys: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(seed_url, 0)])
    scanned_pages: list[str] = []
    all_extracted_links: list[ExtractedLink] = []
    prefetched_cache: dict[str, LinkCheckResult] = {}
    first_page = True

    while queue and len(scanned_pages) < max_pages:
        current_url, depth = queue.popleft()
        dedup_key = canonicalize_url_for_dedup(current_url)
        if dedup_key in visited_keys:
            continue
        visited_keys.add(dedup_key)

        if progress_cb:
            progress_cb(
                f"[Auto-Crawl] กำลังไต่หน้า {len(scanned_pages) + 1}/{max_pages} (Depth {depth}): {current_url}",
                (len(scanned_pages) / max(1, max_pages)) * 0.5,
            )

        check_res, html_bytes, final_url = _fetch_page_html(current_url, timeout=timeout)
        prefetched_cache[strip_tracking_params(current_url)] = check_res

        if first_page:
            first_page = False
            final_host = get_strict_hostname(final_url)
            if final_host and final_host != target_hostname:
                if _is_www_equivalent(final_host, target_hostname):
                    target_hostname = final_host
                    warnings.append(
                        f"หน้าแรกมีการ Redirect ไปยัง {final_url} ระบบจึงตั้ง Strict Hostname เป็น '{target_hostname}' (ยังคงแยก Subdomain อื่นเป็น External Link)"
                    )
                else:
                    warnings.append(
                        f"หน้าแรก Redirect ข้าม Hostname จาก '{target_hostname}' ไปยัง '{final_host}'"
                    )
            if check_res.is_broken:
                warnings.append(f"ไม่สามารถเปิดหน้าเริ่มต้นได้: {check_res.error}")

        # Mark final URL canonical key as visited too so we don't re-crawl redirected canonicals
        visited_keys.add(canonicalize_url_for_dedup(final_url))
        scanned_pages.append(current_url)

        if html_bytes is None:
            continue

        page_links = extract_links_from_html(html_bytes, final_url, target_hostname)
        all_extracted_links.extend(page_links)

        if depth < max_depth:
            for lnk in page_links:
                if lnk.link_type == "Internal":
                    next_key = canonicalize_url_for_dedup(lnk.url)
                    if next_key not in visited_keys:
                        queue.append((strip_tracking_params(lnk.url), depth + 1))

    # Phase 2: Multi-threaded HTTP status check on all extracted links
    def _phase2_progress(msg: str, frac: float) -> None:
        if progress_cb:
            progress_cb(msg, 0.5 + frac * 0.5)

    broken_links, redirected_links, total_checked, skipped_social = check_links_concurrently(
        links=all_extracted_links,
        check_external=check_external,
        ignore_social_media=ignore_social_media,
        ignored_domains=ignored_domains,
        max_workers=max_workers,
        timeout=timeout,
        prefetched_cache=prefetched_cache,
        progress_cb=_phase2_progress,
    )

    return CrawlSummary(
        scan_mode="Auto-Crawl",
        target_hostname=target_hostname,
        pages_scanned=len(scanned_pages),
        scanned_pages=scanned_pages,
        manual_input_urls=[],
        total_links_checked=total_checked,
        extracted_links=all_extracted_links,
        broken_links=broken_links,
        redirected_links=redirected_links,
        skipped_social_count=skipped_social,
        warnings=warnings,
    )


def run_sitemap_scan(
    sitemap_input: str,
    max_pages: int = 150,
    check_external: bool = True,
    ignore_social_media: bool = True,
    ignored_domains: Optional[set[str]] = None,
    max_workers: int = 12,
    timeout: int = 10,
    progress_cb: ProgressCallback = None,
) -> CrawlSummary:
    """
    Mode 2: Sitemap Mode
    Discover/parse sitemap.xml URLs, fetch each page, extract links, and run HTTP checks.
    """
    def _sm_progress(msg: str, frac: float) -> None:
        if progress_cb:
            progress_cb(msg, frac * 0.2)

    page_urls, target_hostname, warnings = discover_and_parse_sitemap(
        sitemap_input=sitemap_input,
        max_pages=max_pages,
        timeout=timeout,
        progress_cb=_sm_progress,
    )

    if not page_urls:
        return CrawlSummary(
            scan_mode="Sitemap",
            target_hostname=target_hostname,
            pages_scanned=0,
            scanned_pages=[],
            manual_input_urls=[],
            total_links_checked=0,
            extracted_links=[],
            broken_links=[],
            redirected_links=[],
            warnings=warnings,
        )

    scanned_pages: list[str] = []
    all_extracted_links: list[ExtractedLink] = []
    prefetched_cache: dict[str, LinkCheckResult] = {}

    # Seed sitemap pages themselves so broken/redirected URLs inside sitemap are reported too
    sitemap_self_links: list[ExtractedLink] = []

    total_pages = len(page_urls)
    completed = 0
    page_workers = max(1, min(max_workers, 8))

    with ThreadPoolExecutor(max_workers=page_workers) as executor:
        future_map = {
            executor.submit(_fetch_page_html, page_url, timeout): page_url
            for page_url in page_urls
        }
        for future in as_completed(future_map):
            page_url = future_map[future]
            try:
                check_res, html_bytes, final_url = future.result()
            except Exception as exc:
                check_res = LinkCheckResult(page_url, None, f"Error: {str(exc)[:120]}", True, False, "", page_url)
                html_bytes = None
                final_url = page_url

            prefetched_cache[strip_tracking_params(page_url)] = check_res
            scanned_pages.append(page_url)

            sitemap_self_links.append(
                ExtractedLink(
                    source_page="(Sitemap URL)",
                    anchor_text="(Sitemap Entry)",
                    url=page_url,
                    link_type="Internal",
                )
            )

            if html_bytes is not None:
                page_links = extract_links_from_html(html_bytes, final_url, target_hostname)
                all_extracted_links.extend(page_links)

            completed += 1
            if progress_cb:
                progress_cb(
                    f"[Sitemap] กำลังดึงหน้า {completed}/{total_pages}: {page_url}",
                    0.2 + (completed / total_pages) * 0.4,
                )

    def _phase2_progress(msg: str, frac: float) -> None:
        if progress_cb:
            progress_cb(msg, 0.6 + frac * 0.4)

    combined_for_http_check = sitemap_self_links + all_extracted_links
    broken_links, redirected_links, total_checked, skipped_social = check_links_concurrently(
        links=combined_for_http_check,
        check_external=check_external,
        ignore_social_media=ignore_social_media,
        ignored_domains=ignored_domains,
        max_workers=max_workers,
        timeout=timeout,
        prefetched_cache=prefetched_cache,
        progress_cb=_phase2_progress,
    )

    return CrawlSummary(
        scan_mode="Sitemap",
        target_hostname=target_hostname,
        pages_scanned=len(scanned_pages),
        scanned_pages=scanned_pages,
        manual_input_urls=[],
        total_links_checked=total_checked,
        extracted_links=all_extracted_links,
        broken_links=broken_links,
        redirected_links=redirected_links,
        skipped_social_count=skipped_social,
        warnings=warnings,
    )


def run_manual_list_scan(
    raw_urls_text: str,
    max_pages: int = 150,
    check_external: bool = True,
    ignore_social_media: bool = True,
    ignored_domains: Optional[set[str]] = None,
    max_workers: int = 12,
    timeout: int = 10,
    progress_cb: ProgressCallback = None,
) -> CrawlSummary:
    """
    Mode 3: Manual List Mode
    Inspect user-supplied URLs line by line: checks each URL itself + all links inside each page.
    """
    social_domains = ignored_domains if ignored_domains is not None else DEFAULT_SOCIAL_MEDIA_DOMAINS
    warnings: list[str] = []
    parsed_urls: list[str] = []
    seen_keys: set[str] = set()
    skipped_social_manual = 0

    for raw_line in raw_urls_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        url = ensure_scheme(line)
        if should_skip_url(url):
            warnings.append(f"ข้าม URL ที่ไม่ใช่หน้าเว็บ: {line}")
            continue
        if ignore_social_media and is_social_media_url(url, social_domains):
            skipped_social_manual += 1
            warnings.append(f"ข้าม URL Social Media: {line}")
            continue
        key = canonicalize_url_for_dedup(url)
        if key not in seen_keys:
            seen_keys.add(key)
            parsed_urls.append(url)
        if len(parsed_urls) >= max_pages:
            warnings.append(f"จำกัดจำนวนสูงสุดที่ {max_pages} หน้าตามการตั้งค่า Max Pages")
            break

    if not parsed_urls:
        return CrawlSummary(
            scan_mode="Manual List",
            target_hostname="",
            pages_scanned=0,
            scanned_pages=[],
            manual_input_urls=[],
            total_links_checked=0,
            extracted_links=[],
            broken_links=[],
            redirected_links=[],
            skipped_social_count=skipped_social_manual,
            warnings=warnings or ["ไม่พบ URL ที่ถูกต้องในรายการที่กรอก"],
        )

    primary_hostname = get_strict_hostname(parsed_urls[0])
    scanned_pages: list[str] = []
    all_extracted_links: list[ExtractedLink] = []
    manual_self_links: list[ExtractedLink] = []
    prefetched_cache: dict[str, LinkCheckResult] = {}

    total_pages = len(parsed_urls)
    completed = 0
    page_workers = max(1, min(max_workers, 8))

    with ThreadPoolExecutor(max_workers=page_workers) as executor:
        future_map = {
            executor.submit(_fetch_page_html, page_url, timeout): page_url
            for page_url in parsed_urls
        }
        for future in as_completed(future_map):
            page_url = future_map[future]
            page_host = get_strict_hostname(page_url)
            try:
                check_res, html_bytes, final_url = future.result()
            except Exception as exc:
                check_res = LinkCheckResult(page_url, None, f"Error: {str(exc)[:120]}", True, False, "", page_url)
                html_bytes = None
                final_url = page_url

            prefetched_cache[strip_tracking_params(page_url)] = check_res
            scanned_pages.append(page_url)

            manual_self_links.append(
                ExtractedLink(
                    source_page="(Manual List Input)",
                    anchor_text="(Direct URL)",
                    url=page_url,
                    link_type="Internal",
                )
            )

            if html_bytes is not None:
                effective_host = get_strict_hostname(final_url) or page_host
                page_links = extract_links_from_html(html_bytes, final_url, effective_host)
                all_extracted_links.extend(page_links)

            completed += 1
            if progress_cb:
                progress_cb(
                    f"[Manual List] กำลังดึงหน้า {completed}/{total_pages}: {page_url}",
                    (completed / total_pages) * 0.5,
                )

    def _phase2_progress(msg: str, frac: float) -> None:
        if progress_cb:
            progress_cb(msg, 0.5 + frac * 0.5)

    combined_for_http_check = manual_self_links + all_extracted_links
    broken_links, redirected_links, total_checked, skipped_social = check_links_concurrently(
        links=combined_for_http_check,
        check_external=check_external,
        ignore_social_media=ignore_social_media,
        ignored_domains=social_domains,
        max_workers=max_workers,
        timeout=timeout,
        prefetched_cache=prefetched_cache,
        progress_cb=_phase2_progress,
    )

    return CrawlSummary(
        scan_mode="Manual List",
        target_hostname=primary_hostname,
        pages_scanned=len(scanned_pages),
        scanned_pages=scanned_pages,
        manual_input_urls=parsed_urls,
        total_links_checked=total_checked,
        extracted_links=all_extracted_links,
        broken_links=broken_links,
        redirected_links=redirected_links,
        skipped_social_count=skipped_social_manual + skipped_social,
        warnings=warnings,
    )
