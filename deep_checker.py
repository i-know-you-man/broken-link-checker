"""
deep_checker.py — Playwright headless browser engine for:
1. 404 Highlighted Screenshot Capture (Standard Check 404 links only)
2. Deep Check inspection for Soft 404, HTTP 404, Blank Pages, 5xx/4xx errors,
   Mobile/Desktop emulation, Ad Parameter click simulation, and highlighted screenshots.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import subprocess
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from crawler import (
    DEFAULT_SOCIAL_MEDIA_DOMAINS,
    BrokenLinkIssue,
    CrawlSummary,
    append_query_params,
    canonicalize_url_for_dedup,
    is_social_media_url,
)

DEFAULT_BUTTON_KEYWORDS = ["book", "booking", "reserve", "shop"]

DEFAULT_SOFT_404_KEYWORDS = [
    "does not exist",
    "page not found",
    "404",
    "unavailable",
    "something went wrong",
    "ไม่พบหน้านี้",
]

DEFAULT_AD_PARAMS = "gclid=test_gclid_123&utm_source=google&utm_medium=cpc"

DEVICE_PROFILES: dict[str, dict] = {
    "Mobile (iPhone 14)": {
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Mobile/15E148 Safari/604.1"
        ),
    },
    "Desktop": {
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
    },
}

ProgressCallback = Optional[Callable[[str, float], None]]


@dataclass
class DeepCheckTarget:
    source_page: str
    anchor_text: str
    target_url: str
    simulate_click_from_source: bool = True


@dataclass
class DeepCheckResult:
    source_page: str
    anchor_text: str
    original_url: str
    tested_url: str
    device: str
    status: str  # "Issue" or "OK"
    issue_type: str  # "Soft 404", "HTTP 404", "Blank Page", "HTTP 410", "HTTP 5xx Error", "HTTP Blocked / 4xx", "Load / Network Error", "OK"
    matched_error_text: str
    text_length: int
    http_status: Optional[int]
    ad_simulation_used: bool
    click_simulated: bool
    screenshot_path: str
    source_screenshot_path: str = ""
    notes: str = ""


# ---------------------------------------------------------------------------
# Keyword Parsing & Target Selection
# ---------------------------------------------------------------------------

def parse_keywords(raw_text: str) -> list[str]:
    """Parse comma-separated or newline-separated keywords into a cleaned list."""
    tokens = re.split(r"[,\n]+", raw_text or "")
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokens:
        cleaned = tok.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return result


def select_deep_check_targets(
    crawl_summary: CrawlSummary,
    button_keywords: list[str],
    manual_scope: str = "all_manual",
    check_external: bool = True,
    ignore_social_media: bool = True,
    ignored_domains: Optional[set[str]] = None,
    max_targets: int = 30,
) -> list[DeepCheckTarget]:
    """
    Select URLs to inspect with Playwright Deep Check.

    manual_scope (used when scan_mode == 'Manual List'):
      - 'all_manual': Check every URL in manual_input_urls directly.
      - 'keywords_only': Only links on scanned pages whose anchor text matches button_keywords.
      - 'both': All manual URLs + keyword-matched links on those pages.
    """
    social_domains = ignored_domains if ignored_domains is not None else DEFAULT_SOCIAL_MEDIA_DOMAINS
    targets: list[DeepCheckTarget] = []
    seen_targets: set[tuple[str, str]] = set()

    include_manual_direct = (
        crawl_summary.scan_mode == "Manual List"
        and manual_scope in ("all_manual", "both")
    )
    include_keyword_links = (
        crawl_summary.scan_mode != "Manual List"
        or manual_scope in ("keywords_only", "both")
    )

    if include_manual_direct:
        for url in crawl_summary.manual_input_urls:
            if len(targets) >= max_targets:
                break
            if ignore_social_media and is_social_media_url(url, social_domains):
                continue
            key = (canonicalize_url_for_dedup(url), "__manual__")
            if key not in seen_targets:
                seen_targets.add(key)
                targets.append(
                    DeepCheckTarget(
                        source_page="(Manual List Input)",
                        anchor_text="(Manual URL)",
                        target_url=url,
                        simulate_click_from_source=False,
                    )
                )

    lower_keywords = [kw.lower() for kw in button_keywords if kw.strip()]
    if include_keyword_links and lower_keywords:
        for lnk in crawl_summary.extracted_links:
            if len(targets) >= max_targets:
                break
            if not check_external and lnk.link_type != "Internal":
                continue
            if ignore_social_media and is_social_media_url(lnk.url, social_domains):
                continue

            anchor_lower = (lnk.anchor_text or "").lower()
            if any(kw in anchor_lower for kw in lower_keywords):
                # Deduplicate repeated navbar/footer buttons pointing to the same URL
                key = (canonicalize_url_for_dedup(lnk.url), anchor_lower.strip())
                if key not in seen_targets:
                    seen_targets.add(key)
                    targets.append(
                        DeepCheckTarget(
                            source_page=lnk.source_page,
                            anchor_text=lnk.anchor_text,
                            target_url=lnk.url,
                            simulate_click_from_source=True,
                        )
                    )

    return targets


# ---------------------------------------------------------------------------
# Soft 404 & Blank Page Detection
# ---------------------------------------------------------------------------

def detect_soft_404_keyword(
    visible_text: str,
    page_title: str,
    keywords: list[str],
) -> tuple[Optional[str], str]:
    """
    Scan rendered body text and page title for Soft 404 error phrases.
    Uses boundary-aware matching for short alphanumeric/numeric tokens like '404'
    to avoid false positives on phone numbers or prices (e.g. '4040').
    """
    haystack_sources = [
        ("Body", visible_text or ""),
        ("Title", page_title or ""),
    ]

    for kw in keywords:
        kw_clean = kw.strip()
        if not kw_clean:
            continue

        # Boundary guard for pure ASCII short numeric/code keywords such as "404" or "500"
        if re.fullmatch(r"[0-9a-zA-Z]{2,4}", kw_clean):
            pattern = re.compile(
                rf"(?<![0-9a-zA-Z]){re.escape(kw_clean)}(?![0-9a-zA-Z])",
                re.IGNORECASE,
            )
        else:
            pattern = re.compile(re.escape(kw_clean), re.IGNORECASE)

        for label, text in haystack_sources:
            if not text:
                continue
            match = pattern.search(text)
            if match:
                start = max(0, match.start() - 45)
                end = min(len(text), match.end() + 55)
                snippet = " ".join(text[start:end].split())
                if start > 0:
                    snippet = "..." + snippet
                if end < len(text):
                    snippet = snippet + "..."
                prefix = "[Title] " if label == "Title" else ""
                return kw_clean, f"{prefix}{snippet}"

    return None, ""


def _safe_filename(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value or "page").strip("_")
    return (cleaned[:max_len] or "page").lower()


# ---------------------------------------------------------------------------
# Playwright Navigation, Link Discovery & Visual 404 Highlight Helpers
# ---------------------------------------------------------------------------

async def _navigate_and_settle(
    page,
    url: str,
    nav_timeout_ms: int,
    extra_wait_ms: int,
) -> tuple[Optional[int], Optional[str]]:
    """
    Navigate with wait_until='networkidle' and gracefully fall back if a site keeps
    background connections open, then wait extra_wait_ms for JS spinners/hydration.
    Returns (http_status, fatal_error_message).
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    http_status: Optional[int] = None
    try:
        resp = await page.goto(url, wait_until="networkidle", timeout=nav_timeout_ms)
        if resp is not None:
            http_status = resp.status
    except PlaywrightTimeoutError:
        # Many modern sites keep analytics/chat sockets open so networkidle times out
        # even though the page has rendered. Only retry if navigation never left about:blank.
        if page.url in ("", "about:blank"):
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                if resp is not None:
                    http_status = resp.status
            except Exception as exc:
                return None, f"Navigation Timeout: {str(exc)[:140]}"
    except Exception as exc:
        return None, f"Navigation Error: {str(exc)[:140]}"

    if extra_wait_ms > 0:
        await page.wait_for_timeout(extra_wait_ms)

    return http_status, None


async def _find_best_link_on_page(
    page,
    target_url: str,
    anchor_text: str,
    allow_anchor_only: bool = True,
) -> dict:
    """
    Locate the best matching <a> element on the loaded source page and tag it with
    data-blc-click-target='true'.
    When allow_anchor_only=False (used for 404 button highlighting), requires the
    element's URL to match target_url so sibling buttons with identical text aren't
    mistakenly circled.
    """
    target_canon = canonicalize_url_for_dedup(target_url)
    target_parsed = urlparse(target_canon)
    target_host = (target_parsed.hostname or "").lower()
    target_path = target_parsed.path or "/"
    target_query = target_parsed.query or ""
    target_anchor_lower = (anchor_text or "").strip().lower()

    js_finder = """
    ({ targetCanon, targetHost, targetPath, targetQuery, targetAnchorLower, allowAnchorOnly }) => {
        const trackingExact = new Set([
            'fbclid', 'gclid', 'gbraid', 'wbraid', 'msclkid', 'dclid',
            'ttclid', 'yclid', '_ga', '_gl', 'mc_cid', 'mc_eid'
        ]);

        function normalizeHost(h) {
            h = (h || '').toLowerCase();
            return h.startsWith('www.') ? h.slice(4) : h;
        }

        function normalizeUrl(rawHref) {
            try {
                const u = new URL(rawHref, window.location.href);
                const params = [];
                u.searchParams.forEach((v, k) => {
                    const kl = k.toLowerCase();
                    if (!trackingExact.has(kl) && !kl.startsWith('utm_')) {
                        params.push([k, v]);
                    }
                });
                params.sort((a, b) => a[0].toLowerCase().localeCompare(b[0].toLowerCase()));
                const sp = new URLSearchParams(params).toString();
                let p = u.pathname || '/';
                if (p !== '/' && p.endsWith('/')) p = p.slice(0, -1);
                return {
                    canon: `${u.protocol}//${u.host}${p}${sp ? '?' + sp : ''}`,
                    host: u.hostname.toLowerCase(),
                    bareHost: normalizeHost(u.hostname),
                    path: p,
                    query: sp,
                    liveHref: u.href
                };
            } catch {
                return null;
            }
        }

        function getVisualRect(el) {
            const r = el.getBoundingClientRect();
            const csEl = window.getComputedStyle(el);
            if (csEl && csEl.display !== 'none' && csEl.visibility !== 'hidden' && parseFloat(csEl.opacity) > 0) {
                if (r.width >= 6 && r.height >= 6) {
                    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
                }
            }
            const rects = [];
            for (const ch of el.querySelectorAll('*')) {
                const cs = window.getComputedStyle(ch);
                if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
                const cr = ch.getBoundingClientRect();
                if (cr.width >= 4 && cr.height >= 4) rects.push(cr);
            }
            if (rects.length === 0) {
                return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
            }
            let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
            for (const box of rects) {
                if (box.left < left) left = box.left;
                if (box.top < top) top = box.top;
                if (box.right > right) right = box.right;
                if (box.bottom > bottom) bottom = box.bottom;
            }
            return { left, top, right, bottom, width: right - left, height: bottom - top };
        }

        function isVisible(el) {
            const style = window.getComputedStyle(el);
            if (!style || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity) === 0) {
                return false;
            }
            const vr = getVisualRect(el);
            return vr.width >= 2 && vr.height >= 2;
        }

        document.querySelectorAll('[data-blc-click-target]').forEach(el => {
            el.removeAttribute('data-blc-click-target');
        });

        const targetBareHost = normalizeHost(targetHost);
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        let best = null;
        let bestScore = -1;

        for (const a of anchors) {
            const norm = normalizeUrl(a.href);
            if (!norm) continue;

            const text = (
                a.innerText ||
                a.getAttribute('aria-label') ||
                a.getAttribute('title') ||
                (a.querySelector('img[alt]') ? a.querySelector('img[alt]').getAttribute('alt') : '') ||
                ''
            ).replace(/\\s+/g, ' ').trim().toLowerCase();

            const urlExact = norm.canon === targetCanon;
            const hostExact = norm.host === targetHost;
            const hostBareMatch = norm.bareHost === targetBareHost;
            const hostPathQueryMatch = hostBareMatch && norm.path === targetPath && norm.query === targetQuery;
            const hostPathMatch = hostBareMatch && norm.path === targetPath;

            const cleanTargetAnchor = targetAnchorLower.replace(/^\\[img\\]\\s*/i, '');
            const anchorExact = cleanTargetAnchor && cleanTargetAnchor !== '(no text)' && text === cleanTargetAnchor;
            const anchorContains = cleanTargetAnchor && cleanTargetAnchor !== '(no text)' && text && (
                text.includes(cleanTargetAnchor) || cleanTargetAnchor.includes(text)
            );

            if (!urlExact && !hostPathQueryMatch && !hostPathMatch && !(allowAnchorOnly && anchorExact)) {
                continue;
            }

            let score = 0;
            if (urlExact) score += 140;
            else if (hostPathQueryMatch) score += 110;
            else if (hostPathMatch) {
                score += (targetQuery && norm.query !== targetQuery) ? 45 : 80;
            }
            if (hostExact) score += 10;

            if (anchorExact) score += 45;
            else if (anchorContains) score += 20;

            const vis = isVisible(a);
            if (vis) score += 60;

            if (score > bestScore) {
                bestScore = score;
                best = { el: a, visible: vis, liveHref: norm.liveHref, score };
            }
        }

        if (!best) {
            return { found: false, visible: false, liveHref: '' };
        }

        best.el.setAttribute('data-blc-click-target', 'true');
        return { found: true, visible: best.visible, liveHref: best.liveHref };
    }
    """
    return await page.evaluate(
        js_finder,
        {
            "targetCanon": target_canon,
            "targetHost": target_host,
            "targetPath": target_path,
            "targetQuery": target_query,
            "targetAnchorLower": target_anchor_lower,
            "allowAnchorOnly": allow_anchor_only,
        },
    )


async def _clear_highlight_overlay(page) -> None:
    """Remove any previous visual highlight overlay injected by this tool."""
    try:
        await page.evaluate(
            """() => {
                const old = document.getElementById('__blc_highlight_overlay__');
                if (old) old.remove();
            }"""
        )
    except Exception:
        pass


async def _inject_rect_overlay(
    page,
    badge_text: str,
    banner_fallback_text: Optional[str] = None,
) -> dict:
    """
    Scroll the element tagged with [data-blc-click-target="true"] into view,
    then draw a bright red circular/rounded highlight ring + badge around its visual box.
    """
    # Step 1: scroll element into center before waiting for layout settle
    scroll_res = await page.evaluate(
        """() => {
            const el = document.querySelector('[data-blc-click-target="true"]');
            if (!el) return { exists: false };
            try {
                el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
            } catch {
                el.scrollIntoView();
            }
            return { exists: true };
        }"""
    )
    if scroll_res.get("exists"):
        await page.wait_for_timeout(250)

    # Step 2: compute post-scroll visual rect and draw overlay
    return await page.evaluate(
        """({ badgeText, bannerFallbackText }) => {
            const old = document.getElementById('__blc_highlight_overlay__');
            if (old) old.remove();

            const root = document.createElement('div');
            root.id = '__blc_highlight_overlay__';
            root.style.cssText = [
                'position: fixed',
                'inset: 0',
                'pointer-events: none',
                'z-index: 2147483647',
                'overflow: visible',
            ].join(';');

            function getVisualRect(el) {
                const r = el.getBoundingClientRect();
                const csEl = window.getComputedStyle(el);
                if (csEl && csEl.display !== 'none' && csEl.visibility !== 'hidden' && parseFloat(csEl.opacity) > 0) {
                    if (r.width >= 6 && r.height >= 6) {
                        return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
                    }
                }
                const rects = [];
                for (const ch of el.querySelectorAll('*')) {
                    const cs = window.getComputedStyle(ch);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
                    const cr = ch.getBoundingClientRect();
                    if (cr.width >= 4 && cr.height >= 4) rects.push(cr);
                }
                if (rects.length === 0) return null;
                let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
                for (const box of rects) {
                    if (box.left < left) left = box.left;
                    if (box.top < top) top = box.top;
                    if (box.right > right) right = box.right;
                    if (box.bottom > bottom) bottom = box.bottom;
                }
                return { left, top, right, bottom, width: right - left, height: bottom - top };
            }

            const el = document.querySelector('[data-blc-click-target="true"]');
            let vr = null;
            if (el) {
                const cs = window.getComputedStyle(el);
                if (cs && cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0) {
                    vr = getVisualRect(el);
                }
            }

            if (vr && vr.width >= 2 && vr.height >= 2) {
                const padX = 12;
                const padY = 8;
                const boxLeft = Math.max(4, vr.left - padX);
                const boxTop = Math.max(4, vr.top - padY);
                const boxWidth = Math.min(window.innerWidth - boxLeft - 4, vr.width + padX * 2);
                const boxHeight = Math.min(window.innerHeight - boxTop - 4, vr.height + padY * 2);
                const usePill = (vr.width <= 340 && vr.height <= 95);

                const ring = document.createElement('div');
                ring.style.cssText = [
                    'position: fixed',
                    `left: ${boxLeft}px`,
                    `top: ${boxTop}px`,
                    `width: ${Math.max(16, boxWidth)}px`,
                    `height: ${Math.max(16, boxHeight)}px`,
                    'border: 4px solid #ef4444',
                    `border-radius: ${usePill ? '999px' : '14px'}`,
                    'background: rgba(239, 68, 68, 0.11)',
                    'box-shadow: 0 0 0 6px rgba(239, 68, 68, 0.28), 0 8px 24px rgba(0, 0, 0, 0.22)',
                    'box-sizing: border-box',
                ].join(';');
                root.appendChild(ring);

                const badge = document.createElement('div');
                const placeBelow = boxTop < 42;
                const labelTop = placeBelow ? Math.min(window.innerHeight - 36, boxTop + boxHeight + 8) : Math.max(6, boxTop - 34);
                const labelLeft = Math.max(8, Math.min(boxLeft, window.innerWidth - 360));
                badge.textContent = badgeText;
                badge.style.cssText = [
                    'position: fixed',
                    `left: ${labelLeft}px`,
                    `top: ${labelTop}px`,
                    'max-width: 345px',
                    'white-space: nowrap',
                    'overflow: hidden',
                    'text-overflow: ellipsis',
                    'background: #dc2626',
                    'color: #ffffff',
                    'font-family: system-ui, -apple-system, Segoe UI, sans-serif',
                    'font-size: 13px',
                    'font-weight: 700',
                    'line-height: 1.3',
                    'padding: 5px 12px',
                    'border-radius: 999px',
                    'box-shadow: 0 4px 12px rgba(0, 0, 0, 0.30)',
                    'border: 2px solid #ffffff',
                ].join(';');
                root.appendChild(badge);
                document.documentElement.appendChild(root);
                return { highlighted: true };
            }

            // Fallback banner if element is hidden in a collapsed menu or missing from live DOM
            const banner = document.createElement('div');
            banner.textContent = bannerFallbackText || badgeText;
            banner.style.cssText = [
                'position: fixed',
                'top: 14px',
                'right: 14px',
                'max-width: min(560px, calc(100vw - 28px))',
                'background: #dc2626',
                'color: #ffffff',
                'font-family: system-ui, -apple-system, Segoe UI, sans-serif',
                'font-size: 13px',
                'font-weight: 700',
                'line-height: 1.4',
                'padding: 10px 14px',
                'border-radius: 10px',
                'box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35)',
                'border: 2px solid #ffffff',
                'word-break: break-word',
            ].join(';');
            root.appendChild(banner);
            document.documentElement.appendChild(root);
            return { highlighted: false };
        }""",
        {
            "badgeText": badge_text,
            "bannerFallbackText": banner_fallback_text,
        },
    )


async def _highlight_link_on_page(
    page,
    target_url: str,
    anchor_text: str,
    badge_text: str,
) -> dict:
    """Find a specific link/button on the current page and draw a red 404 highlight/circle around it."""
    await _clear_highlight_overlay(page)
    found_info = await _find_best_link_on_page(
        page,
        target_url=target_url,
        anchor_text=anchor_text,
        allow_anchor_only=False,
    )

    if not found_info.get("found"):
        fallback = f"{badge_text} (ไม่พบปุ่มนี้ใน DOM ปัจจุบัน: {target_url})"
    elif not found_info.get("visible"):
        fallback = f"{badge_text} (ปุ่มซ่อนอยู่ในเมนู/Dropdown: {target_url})"
    else:
        fallback = badge_text

    res = await _inject_rect_overlay(page, badge_text=badge_text, banner_fallback_text=fallback)
    return {**found_info, **res}


async def _highlight_404_spot_on_destination(
    page,
    matched_kw: Optional[str],
    badge_text: str,
) -> dict:
    """
    Highlight/circle the error text element (for Soft 404 or HTTP 404) on the destination page.
    Uses Range.getBoundingClientRect() on matching text nodes so centered/full-width block
    paragraphs get a tight highlight right around the actual error text.
    """
    await _clear_highlight_overlay(page)

    # Step 1: find matching text node or fallback element and scroll it into view
    found_kind = await page.evaluate(
        """({ matchedKw }) => {
            document.querySelectorAll('[data-blc-click-target]').forEach(el => {
                el.removeAttribute('data-blc-click-target');
            });
            window.__blcMatchedKw = matchedKw || '';

            function isVisible(el) {
                if (!el) return false;
                const cs = window.getComputedStyle(el);
                if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
                const r = el.getBoundingClientRect();
                return r.width >= 4 && r.height >= 4;
            }

            let targetEl = null;

            if (matchedKw && document.body) {
                const isShortAscii = /^[0-9a-zA-Z]{2,4}$/.test(matchedKw.trim());
                const escaped = matchedKw.trim().replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                const rx = isShortAscii
                    ? new RegExp(`(?<![0-9a-zA-Z])${escaped}(?![0-9a-zA-Z])`, 'i')
                    : new RegExp(escaped, 'i');

                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const val = (node.nodeValue || '').trim();
                    if (!val) continue;
                    const parent = node.parentElement;
                    if (!parent || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(parent.tagName)) continue;
                    if (rx.test(val) && isVisible(parent)) {
                        const range = document.createRange();
                        range.selectNodeContents(node);
                        const tr = range.getBoundingClientRect();
                        if (tr.width >= 4 && tr.height >= 4 && tr.height <= window.innerHeight * 0.85) {
                            targetEl = parent;
                            break;
                        }
                    }
                }
            }

            if (!targetEl && document.body) {
                const selectors = [
                    'h1', 'h2', '[class*="404"]', '[id*="404"]',
                    '[class*="not-found"]', '[class*="notfound"]',
                    '[class*="error"]', 'main p', 'p', 'main'
                ];
                for (const sel of selectors) {
                    for (const cand of document.querySelectorAll(sel)) {
                        if (!isVisible(cand)) continue;
                        const txt = (cand.innerText || '').trim();
                        if (!txt) continue;
                        const r = cand.getBoundingClientRect();
                        if (r.height <= window.innerHeight * 0.8) {
                            targetEl = cand;
                            break;
                        }
                    }
                    if (targetEl) break;
                }
            }

            if (targetEl) {
                targetEl.setAttribute('data-blc-click-target', 'true');
                try {
                    targetEl.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                } catch {
                    targetEl.scrollIntoView();
                }
                return true;
            }
            return false;
        }""",
        {"matchedKw": matched_kw or ""},
    )

    if found_kind:
        await page.wait_for_timeout(250)

    # Step 2: compute tight post-scroll rect (using text Range if available) and draw overlay
    overlay_res = await page.evaluate(
        """({ badgeText, matchedKw }) => {
            const old = document.getElementById('__blc_highlight_overlay__');
            if (old) old.remove();

            const root = document.createElement('div');
            root.id = '__blc_highlight_overlay__';
            root.style.cssText = [
                'position: fixed',
                'inset: 0',
                'pointer-events: none',
                'z-index: 2147483647',
                'overflow: visible',
            ].join(';');

            function getTightTextRect(el, kw) {
                if (!el) return null;
                const rx = kw ? (() => {
                    const isShortAscii = /^[0-9a-zA-Z]{2,4}$/.test(kw.trim());
                    const escaped = kw.trim().replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                    return isShortAscii
                        ? new RegExp(`(?<![0-9a-zA-Z])${escaped}(?![0-9a-zA-Z])`, 'i')
                        : new RegExp(escaped, 'i');
                })() : null;

                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                const rects = [];
                let node;
                while ((node = walker.nextNode())) {
                    const val = (node.nodeValue || '').trim();
                    if (!val) continue;
                    if (rx && !rx.test(val)) continue;
                    const r = document.createRange();
                    r.selectNodeContents(node);
                    const br = r.getBoundingClientRect();
                    if (br.width >= 4 && br.height >= 4) {
                        rects.push(br);
                    }
                }
                if (rects.length === 0) {
                    const r = document.createRange();
                    r.selectNodeContents(el);
                    const br = r.getBoundingClientRect();
                    if (br.width >= 4 && br.height >= 4) return br;
                    return el.getBoundingClientRect();
                }
                let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
                for (const b of rects) {
                    if (b.left < left) left = b.left;
                    if (b.top < top) top = b.top;
                    if (b.right > right) right = b.right;
                    if (b.bottom > bottom) bottom = b.bottom;
                }
                return { left, top, right, bottom, width: right - left, height: bottom - top };
            }

            const el = document.querySelector('[data-blc-click-target="true"]');
            const vr = getTightTextRect(el, matchedKw);

            if (vr && vr.width >= 4 && vr.height >= 4) {
                const padX = 16;
                const padY = 10;
                const boxLeft = Math.max(4, vr.left - padX);
                const boxTop = Math.max(4, vr.top - padY);
                const boxWidth = Math.min(window.innerWidth - boxLeft - 4, vr.width + padX * 2);
                const boxHeight = Math.min(window.innerHeight - boxTop - 4, vr.height + padY * 2);
                const usePill = (vr.width <= 460 && vr.height <= 95);

                const ring = document.createElement('div');
                ring.style.cssText = [
                    'position: fixed',
                    `left: ${boxLeft}px`,
                    `top: ${boxTop}px`,
                    `width: ${Math.max(20, boxWidth)}px`,
                    `height: ${Math.max(20, boxHeight)}px`,
                    'border: 4px solid #ef4444',
                    `border-radius: ${usePill ? '999px' : '14px'}`,
                    'background: rgba(239, 68, 68, 0.11)',
                    'box-shadow: 0 0 0 6px rgba(239, 68, 68, 0.28), 0 8px 24px rgba(0, 0, 0, 0.22)',
                    'box-sizing: border-box',
                ].join(';');
                root.appendChild(ring);

                const badge = document.createElement('div');
                const placeBelow = boxTop < 42;
                const labelTop = placeBelow ? Math.min(window.innerHeight - 36, boxTop + boxHeight + 8) : Math.max(6, boxTop - 34);
                const labelLeft = Math.max(8, Math.min(boxLeft, window.innerWidth - 360));
                badge.textContent = badgeText;
                badge.style.cssText = [
                    'position: fixed',
                    `left: ${labelLeft}px`,
                    `top: ${labelTop}px`,
                    'max-width: 345px',
                    'white-space: nowrap',
                    'overflow: hidden',
                    'text-overflow: ellipsis',
                    'background: #dc2626',
                    'color: #ffffff',
                    'font-family: system-ui, -apple-system, Segoe UI, sans-serif',
                    'font-size: 13px',
                    'font-weight: 700',
                    'line-height: 1.3',
                    'padding: 5px 12px',
                    'border-radius: 999px',
                    'box-shadow: 0 4px 12px rgba(0, 0, 0, 0.30)',
                    'border: 2px solid #ffffff',
                ].join(';');
                root.appendChild(badge);
                document.documentElement.appendChild(root);
                return { highlighted: true };
            }

            const banner = document.createElement('div');
            banner.textContent = badgeText;
            banner.style.cssText = [
                'position: fixed',
                'top: 14px',
                'right: 14px',
                'max-width: min(560px, calc(100vw - 28px))',
                'background: #dc2626',
                'color: #ffffff',
                'font-family: system-ui, -apple-system, Segoe UI, sans-serif',
                'font-size: 13px',
                'font-weight: 700',
                'line-height: 1.4',
                'padding: 10px 14px',
                'border-radius: 10px',
                'box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35)',
                'border: 2px solid #ffffff',
            ].join(';');
            root.appendChild(banner);
            document.documentElement.appendChild(root);
            return { highlighted: false };
        }""",
        {"badgeText": badge_text, "matchedKw": matched_kw or ""},
    )
    return {"found": found_kind, **overlay_res}


# ---------------------------------------------------------------------------
# Single Deep Check Execution
# ---------------------------------------------------------------------------

async def _capture_source_button_screenshot(
    context,
    source_page: str,
    target_url: str,
    anchor_text: str,
    issue_type: str,
    index: int,
    screenshot_dir: Path,
    simulate_ad_params: bool,
    ad_params_str: str,
    nav_timeout_ms: int,
) -> str:
    """
    Open the Source Page in a temporary page on the same browser context,
    highlight/circle the problematic button, and save a screenshot.
    Called ONLY for 404 issues (Soft 404 & HTTP 404).
    """
    if not source_page.startswith(("http://", "https://")):
        return ""

    src_page = await context.new_page()
    try:
        src_url = append_query_params(source_page, ad_params_str) if simulate_ad_params else source_page
        _, src_err = await _navigate_and_settle(
            src_page,
            src_url,
            nav_timeout_ms=nav_timeout_ms,
            extra_wait_ms=1000,
        )
        if src_err:
            return ""

        clean_anchor = (anchor_text or "").strip()[:36] or "Link"
        badge = f"{issue_type} Button • {clean_anchor}"
        await _highlight_link_on_page(src_page, target_url, anchor_text, badge)

        screenshot_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        host_slug = _safe_filename(urlparse(source_page).hostname or "source", max_len=24)
        fname = f"{ts}_{index:03d}_source_btn_404_{host_slug}.png"
        full_path = screenshot_dir / fname
        await src_page.screenshot(path=str(full_path), full_page=False)
        return str(full_path)
    except Exception:
        return ""
    finally:
        try:
            await src_page.close()
        except Exception:
            pass


def _classify_deep_issue(
    http_status: Optional[int],
    matched_kw: Optional[str],
    snippet: str,
    visible_text: str,
    text_len: int,
) -> tuple[str, str, str]:
    """
    Return (status, issue_type, matched_error_text) with clean separation between:
    HTTP 404, HTTP 410, HTTP 5xx Error, HTTP Blocked / 4xx, Soft 404, Blank Page, OK.
    """
    if http_status == 404:
        detail = f"HTTP 404 Not Found — [{matched_kw}] {snippet}" if matched_kw else "HTTP 404 Not Found"
        return "Issue", "HTTP 404", detail

    if http_status == 410:
        detail = f"HTTP 410 Gone — [{matched_kw}] {snippet}" if matched_kw else "HTTP 410 Gone"
        return "Issue", "HTTP 410", detail

    if http_status is not None and http_status >= 500:
        return "Issue", "HTTP 5xx Error", f"HTTP Status {http_status}"

    if http_status is not None and http_status >= 400:
        return "Issue", "HTTP Blocked / 4xx", f"HTTP Status {http_status}"

    if matched_kw:
        return "Issue", "Soft 404", f"[{matched_kw}] {snippet}"

    if text_len < 50:
        if text_len == 0:
            return "Issue", "Blank Page", "หน้าเว็บว่างเปล่า (0 ตัวอักษร)"
        clean_preview = " ".join(visible_text.split())
        return "Issue", "Blank Page", f'เนื้อหาน้อยกว่า 50 ตัวอักษร ({text_len} chars): "{clean_preview}"'

    return "OK", "OK", ""


async def _run_single_deep_check(
    browser,
    target: DeepCheckTarget,
    index: int,
    device_name: str,
    device_config: dict,
    soft_404_keywords: list[str],
    simulate_ad_params: bool,
    ad_params_str: str,
    screenshot_dir: Path,
    wait_extra_ms: int,
    nav_timeout_ms: int,
) -> DeepCheckResult:
    """Execute one isolated Playwright inspection inside its own fresh browser context."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    context = await browser.new_context(
        viewport=device_config["viewport"],
        device_scale_factor=device_config["device_scale_factor"],
        is_mobile=device_config["is_mobile"],
        has_touch=device_config["has_touch"],
        user_agent=device_config["user_agent"],
        ignore_https_errors=False,
    )

    page = await context.new_page()
    active_page = page
    http_status: Optional[int] = None
    fatal_error: Optional[str] = None
    click_simulated = False
    notes_parts: list[str] = []

    try:
        use_click_sim = (
            simulate_ad_params
            and target.simulate_click_from_source
            and target.source_page.startswith(("http://", "https://"))
        )

        if use_click_sim:
            source_with_ads = append_query_params(target.source_page, ad_params_str)
            _, src_err = await _navigate_and_settle(
                page,
                source_with_ads,
                nav_timeout_ms=nav_timeout_ms,
                extra_wait_ms=1500,
            )

            if src_err:
                notes_parts.append(f"เปิดหน้าต้นทางไม่สำเร็จ ({src_err}) — ทดสอบเปิดปลายทางพร้อม Ad Params โดยตรง")
                direct_url = append_query_params(target.target_url, ad_params_str)
                http_status, fatal_error = await _navigate_and_settle(
                    page, direct_url, nav_timeout_ms=nav_timeout_ms, extra_wait_ms=wait_extra_ms
                )
            else:
                found_info = await _find_best_link_on_page(
                    page,
                    target.target_url,
                    target.anchor_text,
                    allow_anchor_only=True,
                )

                if found_info.get("found") and found_info.get("visible"):
                    new_pages: list = []
                    latest_nav_status: list[int] = []

                    def _on_new_page(p):
                        new_pages.append(p)
                        p.on(
                            "response",
                            lambda r: latest_nav_status.append(r.status)
                            if r.request.is_navigation_request() and r.frame == p.main_frame
                            else None,
                        )

                    context.on("page", _on_new_page)
                    page.on(
                        "response",
                        lambda r: latest_nav_status.append(r.status)
                        if r.request.is_navigation_request() and r.frame == page.main_frame
                        else None,
                    )

                    before_url = page.url
                    locator = page.locator('[data-blc-click-target="true"]').first
                    clicked_ok = False
                    try:
                        await locator.scroll_into_view_if_needed(timeout=4000)
                        await locator.click(timeout=7000)
                        clicked_ok = True
                    except Exception:
                        try:
                            await locator.click(force=True, timeout=5000)
                            clicked_ok = True
                            notes_parts.append("ใช้ force-click เนื่องจากมีเลเยอร์ทับปุ่ม (เช่น Cookie Banner)")
                        except Exception as click_exc:
                            notes_parts.append(f"คลิกปุ่มไม่สำเร็จ ({str(click_exc)[:80]}) — เปิด live href โดยตรง")

                    if clicked_ok:
                        click_simulated = True
                        await page.wait_for_timeout(1500)
                        if new_pages:
                            active_page = new_pages[-1]
                            try:
                                await active_page.wait_for_load_state("networkidle", timeout=nav_timeout_ms)
                            except PlaywrightTimeoutError:
                                pass
                            await active_page.wait_for_timeout(wait_extra_ms)
                            if latest_nav_status:
                                http_status = latest_nav_status[-1]
                        else:
                            try:
                                await page.wait_for_load_state("networkidle", timeout=nav_timeout_ms)
                            except PlaywrightTimeoutError:
                                pass
                            await page.wait_for_timeout(wait_extra_ms)

                            if page.url == before_url and canonicalize_url_for_dedup(before_url) != canonicalize_url_for_dedup(target.target_url):
                                live_href = found_info.get("liveHref") or target.target_url
                                notes_parts.append("การคลิกไม่เปลี่ยนหน้า — เปิดปลายทางจาก live href โดยตรง")
                                http_status, fatal_error = await _navigate_and_settle(
                                    page, live_href, nav_timeout_ms=nav_timeout_ms, extra_wait_ms=wait_extra_ms
                                )
                            elif latest_nav_status:
                                http_status = latest_nav_status[-1]
                    else:
                        live_href = found_info.get("liveHref") or append_query_params(target.target_url, ad_params_str)
                        http_status, fatal_error = await _navigate_and_settle(
                            page, live_href, nav_timeout_ms=nav_timeout_ms, extra_wait_ms=wait_extra_ms
                        )
                elif found_info.get("found"):
                    live_href = found_info.get("liveHref") or target.target_url
                    if "gclid=" not in live_href.lower() and "utm_" not in live_href.lower():
                        live_href = append_query_params(live_href, ad_params_str)
                    notes_parts.append("ปุ่มถูกซ่อนอยู่ในเมนู (เช่น Mobile Menu) — ทดสอบเปิด live href โดยตรง")
                    http_status, fatal_error = await _navigate_and_settle(
                        page, live_href, nav_timeout_ms=nav_timeout_ms, extra_wait_ms=wait_extra_ms
                    )
                else:
                    notes_parts.append("ไม่พบปุ่มที่ตรงกันบนหน้าต้นทาง — ทดสอบเปิดปลายทางพร้อม Ad Params โดยตรง")
                    direct_url = append_query_params(target.target_url, ad_params_str)
                    http_status, fatal_error = await _navigate_and_settle(
                        page, direct_url, nav_timeout_ms=nav_timeout_ms, extra_wait_ms=wait_extra_ms
                    )
        else:
            dest_url = (
                append_query_params(target.target_url, ad_params_str)
                if simulate_ad_params
                else target.target_url
            )
            http_status, fatal_error = await _navigate_and_settle(
                page, dest_url, nav_timeout_ms=nav_timeout_ms, extra_wait_ms=wait_extra_ms
            )

        tested_url = active_page.url if active_page.url and active_page.url != "about:blank" else target.target_url

        if fatal_error:
            return DeepCheckResult(
                source_page=target.source_page,
                anchor_text=target.anchor_text,
                original_url=target.target_url,
                tested_url=tested_url,
                device=device_name,
                status="Issue",
                issue_type="Load / Network Error",
                matched_error_text=fatal_error,
                text_length=0,
                http_status=http_status,
                ad_simulation_used=simulate_ad_params,
                click_simulated=click_simulated,
                screenshot_path="",
                source_screenshot_path="",
                notes=" | ".join(notes_parts),
            )

        # Evaluate actual rendered visible text & title
        try:
            visible_text = await active_page.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText.trim() : ''"
            )
        except Exception:
            visible_text = ""

        try:
            page_title = await active_page.title()
        except Exception:
            page_title = ""

        text_len = len(visible_text)
        matched_kw, snippet = detect_soft_404_keyword(visible_text, page_title, soft_404_keywords)

        status, issue_type, matched_error_text = _classify_deep_issue(
            http_status=http_status,
            matched_kw=matched_kw,
            snippet=snippet,
            visible_text=visible_text,
            text_len=text_len,
        )

        screenshot_path = ""
        source_screenshot_path = ""
        is_404_issue = issue_type in ("Soft 404", "HTTP 404")

        # Highlight & screencap:
        # - For 404 (Soft 404 / HTTP 404): highlight error spot on destination AND highlight problematic button on source page.
        # - For Blank Page: capture plain destination screenshot (no highlight circle).
        if is_404_issue or issue_type == "Blank Page":
            try:
                if is_404_issue:
                    badge = (
                        f"Soft 404 • {matched_kw}"
                        if (issue_type == "Soft 404" and matched_kw)
                        else f"{issue_type} Detected"
                    )
                    await _highlight_404_spot_on_destination(active_page, matched_kw, badge)

                screenshot_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                issue_slug = _safe_filename(issue_type, max_len=16)
                host_slug = _safe_filename(urlparse(tested_url).hostname or "page", max_len=28)
                fname = f"{ts}_{index:03d}_{issue_slug}_{host_slug}.png"
                full_path = screenshot_dir / fname
                await active_page.screenshot(path=str(full_path), full_page=False)
                screenshot_path = str(full_path)
            except Exception as sc_exc:
                notes_parts.append(f"แคปภาพหน้าจอไม่สำเร็จ: {str(sc_exc)[:80]}")

            if is_404_issue and target.source_page.startswith(("http://", "https://")):
                source_screenshot_path = await _capture_source_button_screenshot(
                    context=context,
                    source_page=target.source_page,
                    target_url=target.target_url,
                    anchor_text=target.anchor_text,
                    issue_type=issue_type,
                    index=index,
                    screenshot_dir=screenshot_dir,
                    simulate_ad_params=simulate_ad_params,
                    ad_params_str=ad_params_str,
                    nav_timeout_ms=nav_timeout_ms,
                )

        return DeepCheckResult(
            source_page=target.source_page,
            anchor_text=target.anchor_text,
            original_url=target.target_url,
            tested_url=tested_url,
            device=device_name,
            status=status,
            issue_type=issue_type,
            matched_error_text=matched_error_text,
            text_length=text_len,
            http_status=http_status,
            ad_simulation_used=simulate_ad_params,
            click_simulated=click_simulated,
            screenshot_path=screenshot_path,
            source_screenshot_path=source_screenshot_path,
            notes=" | ".join(notes_parts),
        )
    finally:
        try:
            await context.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Chromium Auto-Install & Low-Memory Cloud Launch Helper
# ---------------------------------------------------------------------------

_CHROMIUM_INSTALL_LOCK = threading.Lock()


def _install_chromium_sync() -> None:
    """Install Playwright Chromium binary once if missing (e.g. on Streamlit Cloud)."""
    with _CHROMIUM_INSTALL_LOCK:
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err_tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(
                "ติดตั้ง Playwright Chromium อัตโนมัติไม่สำเร็จ — กรุณารันคำสั่ง "
                f"`playwright install chromium`\nรายละเอียด: {err_tail}"
            )


async def _launch_chromium_browser(playwright_instance, progress_cb: ProgressCallback = None):
    """
    Launch headless Chromium with container-safe low-memory flags on Linux
    and automatic one-time browser binary installation when running on Cloud.
    """
    launch_args: list[str] = []
    if sys.platform.startswith("linux"):
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
        ]

    exe_path = playwright_instance.chromium.executable_path
    if not exe_path or not os.path.exists(exe_path):
        if progress_cb:
            progress_cb(
                "กำลังดาวน์โหลดและติดตั้ง Playwright Chromium ครั้งแรก (อาจใช้เวลา ~30–60 วินาที)...",
                0.0,
            )
        await asyncio.to_thread(_install_chromium_sync)

    try:
        return await playwright_instance.chromium.launch(headless=True, args=launch_args)
    except Exception as exc:
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            if progress_cb:
                progress_cb(
                    "กำลังดาวน์โหลดและติดตั้ง Playwright Chromium ครั้งแรก (อาจใช้เวลา ~30–60 วินาที)...",
                    0.0,
                )
            await asyncio.to_thread(_install_chromium_sync)
            try:
                return await playwright_instance.chromium.launch(headless=True, args=launch_args)
            except Exception as retry_exc:
                raise RuntimeError(
                    "ไม่สามารถเปิด Chromium ของ Playwright ได้ — กรุณาตรวจสอบ packages.txt หรือรัน "
                    f"`playwright install chromium`\nรายละเอียด: {retry_exc}"
                ) from retry_exc
        raise RuntimeError(
            "ไม่สามารถเปิด Chromium ของ Playwright ได้ — กรุณารันคำสั่ง "
            f"`playwright install chromium`\nรายละเอียด: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Standard Check 404 Highlight Screencap Engine (404 Only)
# ---------------------------------------------------------------------------

async def _capture_404_screenshots_async(
    broken_404_items: list[BrokenLinkIssue],
    device_name: str,
    screenshot_dir: Path,
    wait_extra_ms: int,
    nav_timeout_ms: int,
    progress_cb: ProgressCallback,
) -> None:
    from playwright.async_api import async_playwright

    device_config = DEVICE_PROFILES.get(device_name, DEVICE_PROFILES["Desktop"])

    # Group by source_page so each page is loaded once even if it has multiple 404 links
    grouped: dict[str, list[BrokenLinkIssue]] = defaultdict(list)
    for item in broken_404_items:
        grouped[item.source_page].append(item)

    total = len(broken_404_items)
    done_count = 0

    async with async_playwright() as p:
        browser = await _launch_chromium_browser(p, progress_cb=progress_cb)

        try:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            for source_page, items in grouped.items():
                context = await browser.new_context(
                    viewport=device_config["viewport"],
                    device_scale_factor=device_config["device_scale_factor"],
                    is_mobile=device_config["is_mobile"],
                    has_touch=device_config["has_touch"],
                    user_agent=device_config["user_agent"],
                    ignore_https_errors=False,
                )
                page = await context.new_page()
                try:
                    if source_page.startswith(("http://", "https://")):
                        _, src_err = await _navigate_and_settle(
                            page,
                            source_page,
                            nav_timeout_ms=nav_timeout_ms,
                            extra_wait_ms=wait_extra_ms,
                        )
                        if src_err:
                            done_count += len(items)
                            continue

                        for item in items:
                            done_count += 1
                            if progress_cb:
                                progress_cb(
                                    f"[404 Screencap] กำลัง Highlight ปุ่มเสีย ({done_count}/{total}): {item.anchor_text}",
                                    done_count / max(1, total),
                                )
                            clean_anchor = (item.anchor_text or "").strip()[:36] or "404 Link"
                            badge = f"404 Broken • {clean_anchor}"
                            await _highlight_link_on_page(
                                page,
                                target_url=item.broken_url,
                                anchor_text=item.anchor_text,
                                badge_text=badge,
                            )
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            host_slug = _safe_filename(urlparse(source_page).hostname or "page", max_len=24)
                            fname = f"{ts}_{done_count:03d}_404_src_{host_slug}.png"
                            full_path = screenshot_dir / fname
                            await page.screenshot(path=str(full_path), full_page=False)
                            item.screenshot_path = str(full_path)
                    else:
                        # Direct Sitemap / Manual List entry that itself returned 404
                        for item in items:
                            done_count += 1
                            if progress_cb:
                                progress_cb(
                                    f"[404 Screencap] กำลังแคปหน้า 404 ({done_count}/{total}): {item.broken_url}",
                                    done_count / max(1, total),
                                )
                            _, err = await _navigate_and_settle(
                                page,
                                item.broken_url,
                                nav_timeout_ms=nav_timeout_ms,
                                extra_wait_ms=wait_extra_ms,
                            )
                            if err:
                                continue
                            await _highlight_404_spot_on_destination(
                                page,
                                matched_kw="404",
                                badge_text=f"HTTP 404 Not Found • {urlparse(item.broken_url).path or '/'}",
                            )
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            host_slug = _safe_filename(urlparse(item.broken_url).hostname or "page", max_len=24)
                            fname = f"{ts}_{done_count:03d}_404_page_{host_slug}.png"
                            full_path = screenshot_dir / fname
                            await page.screenshot(path=str(full_path), full_page=False)
                            item.screenshot_path = str(full_path)
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Thread-safe Async Runner & Public Synchronous Entrypoints
# ---------------------------------------------------------------------------

def _run_coro_in_proactor_thread(coro_factory: Callable[[ProgressCallback], object], progress_cb: ProgressCallback):
    """
    Execute an async Playwright coroutine on a dedicated thread with ProactorEventLoop
    on Windows, pumping progress callbacks safely back on the caller thread.
    """
    result_box: dict = {"value": None, "error": None}
    progress_queue: queue.Queue[tuple[str, float]] = queue.Queue()

    def _thread_progress(msg: str, frac: float) -> None:
        progress_queue.put((msg, frac))

    def _worker() -> None:
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            cb = _thread_progress if progress_cb else None
            result_box["value"] = loop.run_until_complete(coro_factory(cb))
        except Exception as exc:
            result_box["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    while thread.is_alive() or not progress_queue.empty():
        try:
            msg, frac = progress_queue.get(timeout=0.1)
            if progress_cb:
                progress_cb(msg, frac)
        except queue.Empty:
            continue

    thread.join()

    if result_box["error"] is not None:
        raise result_box["error"]

    return result_box["value"]


async def _run_deep_check_async(
    targets: list[DeepCheckTarget],
    device_name: str,
    soft_404_keywords: list[str],
    simulate_ad_params: bool,
    ad_params_str: str,
    screenshot_dir: Path,
    wait_extra_ms: int,
    nav_timeout_ms: int,
    progress_cb: ProgressCallback,
) -> list[DeepCheckResult]:
    from playwright.async_api import async_playwright

    device_config = DEVICE_PROFILES.get(device_name, DEVICE_PROFILES["Mobile (iPhone 14)"])
    results: list[DeepCheckResult] = []
    total = len(targets)

    async with async_playwright() as p:
        browser = await _launch_chromium_browser(p, progress_cb=progress_cb)

        try:
            for idx, target in enumerate(targets, start=1):
                if progress_cb:
                    mode_tag = " + Ad Click Sim" if simulate_ad_params else ""
                    progress_cb(
                        f"[Deep Check — {device_name}{mode_tag}] กำลังตรวจ ({idx}/{total}): {target.target_url}",
                        (idx - 1) / max(1, total),
                    )
                res = await _run_single_deep_check(
                    browser=browser,
                    target=target,
                    index=idx,
                    device_name=device_name,
                    device_config=device_config,
                    soft_404_keywords=soft_404_keywords,
                    simulate_ad_params=simulate_ad_params,
                    ad_params_str=ad_params_str,
                    screenshot_dir=screenshot_dir,
                    wait_extra_ms=wait_extra_ms,
                    nav_timeout_ms=nav_timeout_ms,
                )
                results.append(res)
                if progress_cb:
                    progress_cb(
                        f"[Deep Check — {device_name}] ตรวจเสร็จแล้ว ({idx}/{total})",
                        idx / max(1, total),
                    )
        finally:
            await browser.close()

    return results


def run_deep_check(
    targets: list[DeepCheckTarget],
    device_name: str = "Mobile (iPhone 14)",
    soft_404_keywords: Optional[list[str]] = None,
    simulate_ad_params: bool = False,
    ad_params_str: str = DEFAULT_AD_PARAMS,
    screenshot_dir: str | Path = "screenshots",
    wait_extra_ms: int = 2500,
    nav_timeout_ms: int = 20000,
    progress_cb: ProgressCallback = None,
) -> list[DeepCheckResult]:
    """
    Synchronous entrypoint safe to call from Streamlit (including on Windows).
    """
    if not targets:
        return []

    keywords = soft_404_keywords if soft_404_keywords is not None else list(DEFAULT_SOFT_404_KEYWORDS)
    shot_dir = Path(screenshot_dir)

    return _run_coro_in_proactor_thread(
        lambda cb: _run_deep_check_async(
            targets=targets,
            device_name=device_name,
            soft_404_keywords=keywords,
            simulate_ad_params=simulate_ad_params,
            ad_params_str=ad_params_str,
            screenshot_dir=shot_dir,
            wait_extra_ms=wait_extra_ms,
            nav_timeout_ms=nav_timeout_ms,
            progress_cb=cb,
        ),
        progress_cb=progress_cb,
    )


def capture_404_screenshots(
    broken_links: list[BrokenLinkIssue],
    device_name: str = "Desktop",
    screenshot_dir: str | Path = "screenshots",
    max_screenshots: int = 30,
    wait_extra_ms: int = 1000,
    nav_timeout_ms: int = 20000,
    progress_cb: ProgressCallback = None,
) -> int:
    """
    Capture highlighted screenshots strictly for HTTP 404 BrokenLinkIssue entries,
    updating each item's `screenshot_path` in place.
    Returns the number of 404 screenshots captured.
    """
    only_404 = [item for item in broken_links if item.status_code == 404]
    if not only_404:
        return 0

    # Deduplicate by (source_page, broken_url, anchor_text) up to max_screenshots
    unique_items: list[BrokenLinkIssue] = []
    dup_groups: dict[tuple[str, str, str], list[BrokenLinkIssue]] = defaultdict(list)

    for item in only_404:
        sig = (item.source_page, item.broken_url, item.anchor_text)
        if sig not in dup_groups:
            if len(unique_items) >= max_screenshots:
                continue
            unique_items.append(item)
        dup_groups[sig].append(item)

    if not unique_items:
        return 0

    shot_dir = Path(screenshot_dir)
    _run_coro_in_proactor_thread(
        lambda cb: _capture_404_screenshots_async(
            broken_404_items=unique_items,
            device_name=device_name,
            screenshot_dir=shot_dir,
            wait_extra_ms=wait_extra_ms,
            nav_timeout_ms=nav_timeout_ms,
            progress_cb=cb,
        ),
        progress_cb=progress_cb,
    )

    captured = 0
    for sig, group in dup_groups.items():
        first_path = group[0].screenshot_path
        if first_path:
            captured += 1
            for dup in group[1:]:
                dup.screenshot_path = first_path

    return captured
