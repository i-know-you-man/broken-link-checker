"""
app.py — Streamlit Web UI for the Broken Link, Redirect & Soft 404 Checker.
Run with: streamlit run app.py
"""

from __future__ import annotations

import hmac
import io
import logging
import os
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

import pandas as pd
import streamlit as st
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from crawler import (
    DEFAULT_SOCIAL_MEDIA_DOMAINS,
    BrokenLinkIssue,
    CrawlSummary,
    parse_domain_list,
    run_auto_crawl,
    run_manual_list_scan,
    run_sitemap_scan,
)
from deep_checker import (
    DEFAULT_AD_PARAMS,
    DEFAULT_BUTTON_KEYWORDS,
    DEFAULT_SOFT_404_KEYWORDS,
    DEVICE_PROFILES,
    DeepCheckResult,
    capture_404_screenshots,
    parse_keywords,
    run_deep_check,
    select_deep_check_targets,
)
from history_logger import (
    ScanLogEntry,
    extract_fallback_hostname,
    format_target_input,
    get_storage_backend_info,
    load_scan_history,
    now_thailand_slug,
    now_thailand_str,
    record_scan_history,
    test_gsheets_connection,
)

SCREENSHOT_DIR = Path(__file__).resolve().parent / "screenshots"

# ---------------------------------------------------------------------------
# Page Configuration & Styling
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Broken Link, Redirect & Soft 404 Checker",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Noto+Sans+Thai:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap');

    :root {
        --ev-bg: #030712;
        --ev-sidebar: #0b1120;
        --ev-surface: #111827;
        --ev-surface-soft: rgba(17, 24, 39, 0.78);
        --ev-border: rgba(148, 163, 184, 0.16);
        --ev-border-strong: rgba(139, 92, 246, 0.42);
        --ev-violet: #8b5cf6;
        --ev-violet-light: #a78bfa;
        --ev-rose: #f43f5e;
        --ev-rose-light: #fb7185;
        --ev-amber: #f59e0b;
        --ev-emerald: #10b981;
        --ev-text: #f8fafc;
        --ev-muted: #94a3b8;
        --ev-font: 'Space Grotesk', 'Noto Sans Thai', system-ui, -apple-system, sans-serif;
        --ev-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    }

    /* Typography — scoped carefully so Streamlit Material icons remain intact */
    html, body, [data-testid="stAppViewContainer"], .stMarkdown,
    p, h1, h2, h3, h4, label, input, textarea, button {
        font-family: var(--ev-font);
    }

    code, pre {
        font-family: var(--ev-mono);
    }

    /* Eventor Obsidian Canvas + Geometric Grid + Ambient Violet/Rose Glow */
    [data-testid="stAppViewContainer"] {
        background-color: var(--ev-bg);
        background-image:
            radial-gradient(640px circle at 10% 4%, rgba(139, 92, 246, 0.22), transparent 62%),
            radial-gradient(560px circle at 94% 92%, rgba(244, 63, 94, 0.16), transparent 60%),
            linear-gradient(rgba(255, 255, 255, 0.024) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255, 255, 255, 0.024) 1px, transparent 1px);
        background-size: 100% 100%, 100% 100%, 64px 64px, 64px 64px;
        background-attachment: fixed;
        color: var(--ev-text);
    }

    [data-testid="stHeader"] {
        background: rgba(3, 7, 18, 0.55);
        backdrop-filter: blur(10px);
    }

    .block-container {
        padding-top: 1.4rem;
        padding-bottom: 2.5rem;
        max-width: 1280px;
    }

    /* Left Pro Sidebar Studio */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0b1120 0%, #060b16 100%);
        border-right: 1px solid var(--ev-border);
    }

    [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        padding-top: 1.1rem;
    }

    /* Sidebar Brand Lockup */
    .ev-brand {
        display: flex;
        align-items: center;
        gap: 0.78rem;
        padding: 0.15rem 0.1rem 0.9rem 0.1rem;
        margin-bottom: 0.35rem;
    }
    .ev-brand-icon {
        width: 44px;
        height: 44px;
        border-radius: 12px;
        background: linear-gradient(135deg, #8b5cf6 0%, #f43f5e 100%);
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 6px 20px rgba(139, 92, 246, 0.38);
        flex-shrink: 0;
    }
    .ev-brand-title {
        font-size: 1.18rem;
        font-weight: 700;
        color: #ffffff;
        line-height: 1.15;
        letter-spacing: -0.02em;
    }
    .ev-brand-sub {
        font-size: 0.76rem;
        color: var(--ev-muted);
        margin-top: 0.12rem;
    }

    .ev-card-title {
        font-size: 0.92rem;
        font-weight: 700;
        color: #f8fafc;
        margin: 0 0 0.45rem 0;
        letter-spacing: -0.01em;
    }

    /* Dark Glass Cards (Sidebar + Main bordered containers) */
    [data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(17, 24, 39, 0.72);
        border: 1px solid rgba(148, 163, 184, 0.14) !important;
        border-radius: 16px !important;
        transition: border-color 0.2s ease;
    }
    [data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: rgba(139, 92, 246, 0.36) !important;
    }

    [data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(17, 24, 39, 0.78);
        border: 1px solid rgba(148, 163, 184, 0.15) !important;
        border-radius: 16px !important;
    }

    /* Inputs & Selects */
    .stTextInput input, .stTextArea textarea {
        background-color: #030712 !important;
        border: 1px solid rgba(148, 163, 184, 0.22) !important;
        border-radius: 10px !important;
        color: #f8fafc !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .stTextInput input:focus, .stTextArea textarea:focus {
        border-color: #8b5cf6 !important;
        box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.22) !important;
    }

    /* Segmented Control (Scan Mode Pills) */
    [data-testid="stSidebar"] [data-testid="stButtonGroup"] {
        background: rgba(3, 7, 18, 0.65);
        padding: 3px;
        border-radius: 999px;
        border: 1px solid rgba(148, 163, 184, 0.16);
    }
    [data-testid="stSidebar"] [data-testid="stButtonGroup"] button {
        border-radius: 999px !important;
        font-size: 0.78rem !important;
        font-weight: 600 !important;
        border: none !important;
    }

    /* Eventor Signature Primary Gradient Pill CTA (Start Scan & Login) */
    [data-testid="stSidebar"] button[kind="primary"],
    [data-testid="stFormSubmitButton"] button[kind="primary"] {
        background: linear-gradient(90deg, #8b5cf6 0%, #f43f5e 100%) !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 9999px !important;
        font-weight: 700 !important;
        font-size: 0.96rem !important;
        padding: 0.62rem 1.25rem !important;
        box-shadow: 0 8px 24px rgba(139, 92, 246, 0.36) !important;
        transition: transform 0.18s ease, box-shadow 0.2s ease, filter 0.2s ease !important;
        cursor: pointer;
    }
    [data-testid="stSidebar"] button[kind="primary"]:hover,
    [data-testid="stFormSubmitButton"] button[kind="primary"]:hover {
        transform: translateY(-1px);
        filter: brightness(1.06);
        box-shadow: 0 10px 30px rgba(244, 63, 94, 0.42) !important;
    }

    /* Export Pill Download Buttons */
    [data-testid="stDownloadButton"] button {
        border-radius: 9999px !important;
        font-size: 0.84rem !important;
        font-weight: 600 !important;
        padding: 0.46rem 1rem !important;
        transition: all 0.2s ease !important;
        cursor: pointer;
    }
    [data-testid="stDownloadButton"] button[kind="primary"] {
        background: rgba(139, 92, 246, 0.16) !important;
        border: 1.5px solid #8b5cf6 !important;
        color: #ffffff !important;
        box-shadow: 0 4px 18px rgba(139, 92, 246, 0.25) !important;
    }
    [data-testid="stDownloadButton"] button[kind="primary"]:hover {
        background: linear-gradient(90deg, #8b5cf6 0%, #f43f5e 100%) !important;
        border-color: transparent !important;
        box-shadow: 0 6px 22px rgba(244, 63, 94, 0.32) !important;
    }
    [data-testid="stDownloadButton"] button[kind="secondary"] {
        background: rgba(17, 24, 39, 0.85) !important;
        border: 1px solid rgba(148, 163, 184, 0.24) !important;
        color: #e2e8f0 !important;
    }
    [data-testid="stDownloadButton"] button[kind="secondary"]:hover {
        border-color: #8b5cf6 !important;
        color: #ffffff !important;
        background: rgba(139, 92, 246, 0.12) !important;
    }

    /* Eventor Hero Header */
    .ev-hero {
        text-align: center;
        padding: 0.35rem 0 1.25rem 0;
    }
    .ev-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.34rem 0.95rem;
        border-radius: 9999px;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.12);
        backdrop-filter: blur(8px);
        font-size: 0.8rem;
        font-weight: 500;
        color: #cbd5e1;
        margin-bottom: 0.85rem;
    }
    .ev-dot {
        width: 8px;
        height: 8px;
        border-radius: 9999px;
        display: inline-block;
    }
    .ev-dot-rose {
        background: #fb7185;
        box-shadow: 0 0 10px #f43f5e;
    }
    .ev-dot-ok {
        background: #10b981;
        box-shadow: 0 0 10px #10b981;
    }
    .ev-hero-title {
        font-size: clamp(1.85rem, 3.2vw, 2.75rem);
        font-weight: 700;
        color: #ffffff;
        letter-spacing: -0.03em;
        line-height: 1.18;
        margin: 0;
    }
    .ev-grad-text {
        background: linear-gradient(90deg, #a78bfa 0%, #fb7185 55%, #a78bfa 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    .ev-hero-sub {
        color: var(--ev-muted);
        font-size: 0.95rem;
        max-width: 720px;
        margin: 0.6rem auto 0 auto;
        line-height: 1.55;
    }

    /* Eventor Frosted Glass KPI Cards (Option 2 Style) */
    .kpi-card {
        position: relative;
        overflow: hidden;
        border: 1px solid rgba(148, 163, 184, 0.16);
        border-radius: 16px;
        padding: 1.15rem 1.05rem 1rem 1.05rem;
        background: linear-gradient(180deg, rgba(30, 41, 59, 0.62) 0%, rgba(15, 23, 42, 0.86) 100%);
        backdrop-filter: blur(10px);
        text-align: center;
        transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
        height: 100%;
    }
    .kpi-card::before {
        content: "";
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 3px;
        background: linear-gradient(90deg, #64748b, #94a3b8);
    }
    .kpi-card:hover {
        transform: translateY(-2px);
        border-color: rgba(148, 163, 184, 0.34);
    }
    .kpi-value {
        font-size: 2.05rem;
        font-weight: 700;
        line-height: 1.1;
        color: #ffffff;
        font-variant-numeric: tabular-nums;
        letter-spacing: -0.02em;
    }
    .kpi-label {
        font-size: 0.84rem;
        color: #cbd5e1;
        margin-top: 0.38rem;
        font-weight: 600;
    }
    .kpi-sub {
        font-size: 0.74rem;
        color: var(--ev-muted);
        margin-top: 0.24rem;
        line-height: 1.35;
    }

    .kpi-info::before { background: linear-gradient(90deg, #38bdf8, #818cf8); }
    .kpi-info { border-color: rgba(56, 189, 248, 0.22); }

    .kpi-danger::before { background: linear-gradient(90deg, #f43f5e, #fb7185); }
    .kpi-danger {
        border-color: rgba(244, 63, 94, 0.38);
        background: linear-gradient(180deg, rgba(136, 19, 55, 0.36) 0%, rgba(17, 24, 39, 0.88) 100%);
    }
    .kpi-danger .kpi-value { color: #fb7185; }

    .kpi-warn::before { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
    .kpi-warn {
        border-color: rgba(245, 158, 11, 0.36);
        background: linear-gradient(180deg, rgba(120, 53, 15, 0.32) 0%, rgba(17, 24, 39, 0.88) 100%);
    }
    .kpi-warn .kpi-value { color: #fbbf24; }

    .kpi-purple::before { background: linear-gradient(90deg, #8b5cf6, #c084fc); }
    .kpi-purple {
        border-color: rgba(139, 92, 246, 0.42);
        background: linear-gradient(180deg, rgba(46, 16, 101, 0.46) 0%, rgba(17, 24, 39, 0.88) 100%);
    }
    .kpi-purple .kpi-value { color: #a78bfa; }

    .kpi-ok::before { background: linear-gradient(90deg, #10b981, #34d399); }
    .kpi-ok {
        border-color: rgba(16, 185, 129, 0.32);
        background: linear-gradient(180deg, rgba(6, 78, 59, 0.3) 0%, rgba(17, 24, 39, 0.88) 100%);
    }
    .kpi-ok .kpi-value { color: #34d399; }

    /* Welcome Feature Cards */
    .ev-feature-card {
        position: relative;
        overflow: hidden;
        background: rgba(17, 24, 39, 0.8);
        border: 1px solid rgba(148, 163, 184, 0.16);
        border-radius: 16px;
        padding: 1.25rem 1.25rem 1.15rem 1.25rem;
        height: 100%;
        transition: border-color 0.2s ease, transform 0.2s ease;
    }
    .ev-feature-card:hover {
        border-color: rgba(139, 92, 246, 0.42);
        transform: translateY(-2px);
    }
    .ev-feature-icon {
        width: 38px;
        height: 38px;
        border-radius: 10px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        margin-bottom: 0.75rem;
    }
    .ev-fi-violet { background: rgba(139, 92, 246, 0.16); border: 1px solid rgba(139, 92, 246, 0.35); color: #a78bfa; }
    .ev-fi-rose   { background: rgba(244, 63, 94, 0.16); border: 1px solid rgba(244, 63, 94, 0.35); color: #fb7185; }
    .ev-fi-indigo { background: rgba(99, 102, 241, 0.16); border: 1px solid rgba(129, 140, 248, 0.35); color: #c4b5fd; }
    .ev-feature-title {
        font-size: 1.02rem;
        font-weight: 700;
        color: #ffffff;
        margin-bottom: 0.55rem;
    }
    .ev-feature-list {
        margin: 0;
        padding-left: 1.05rem;
        color: #cbd5e1;
        font-size: 0.86rem;
        line-height: 1.6;
    }
    .ev-feature-list li {
        margin-bottom: 0.32rem;
    }

    .ev-cta-hint {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.6rem;
        padding: 0.85rem 1.2rem;
        border-radius: 14px;
        background: rgba(139, 92, 246, 0.1);
        border: 1px solid rgba(139, 92, 246, 0.3);
        color: #e2e8f0;
        font-size: 0.9rem;
        margin-bottom: 1.2rem;
    }

    .ev-toolbar-label {
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--ev-muted);
        margin-bottom: 0.45rem;
    }

    /* Eventor Segmented Tabs */
    .stTabs [data-baseweb="tab-list"] {
        background: rgba(17, 24, 39, 0.88);
        border: 1px solid rgba(148, 163, 184, 0.16);
        border-radius: 14px;
        padding: 5px;
        gap: 6px;
        margin-bottom: 0.75rem;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 10px !important;
        padding: 0.48rem 1rem !important;
        color: #94a3b8 !important;
        font-weight: 600 !important;
        font-size: 0.88rem !important;
        background: transparent !important;
        border: none !important;
        transition: all 0.18s ease !important;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: #f8fafc !important;
        background: rgba(255, 255, 255, 0.05) !important;
    }
    .stTabs [aria-selected="true"] {
        background: #8b5cf6 !important;
        color: #ffffff !important;
        box-shadow: 0 4px 16px rgba(139, 92, 246, 0.35) !important;
    }
    .stTabs [data-baseweb="tab-highlight"],
    .stTabs [data-baseweb="tab-border"] {
        display: none !important;
    }

    /* Issue Badges & Status Chips */
    .issue-badge {
        display: inline-block;
        padding: 0.22rem 0.68rem;
        border-radius: 9999px;
        font-size: 0.78rem;
        font-weight: 600;
        margin-right: 0.4rem;
        margin-bottom: 0.2rem;
    }
    .badge-soft404 {
        background: rgba(244, 63, 94, 0.18);
        color: #fb7185;
        border: 1px solid rgba(244, 63, 94, 0.42);
    }
    .badge-blank {
        background: rgba(139, 92, 246, 0.18);
        color: #a78bfa;
        border: 1px solid rgba(139, 92, 246, 0.42);
    }
    .badge-ad {
        background: rgba(245, 158, 11, 0.18);
        color: #fbbf24;
        border: 1px solid rgba(245, 158, 11, 0.42);
    }
    .badge-device {
        background: rgba(56, 189, 248, 0.15);
        color: #38bdf8;
        border: 1px solid rgba(56, 189, 248, 0.35);
    }
    .badge-neutral {
        background: rgba(148, 163, 184, 0.16);
        color: #cbd5e1;
        border: 1px solid rgba(148, 163, 184, 0.32);
    }
    .status-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 0.48rem;
        margin-bottom: 0.85rem;
    }
    .status-chip {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        padding: 0.26rem 0.78rem;
        border-radius: 9999px;
        font-size: 0.80rem;
        font-weight: 600;
        color: #e2e8f0;
        border: 1px solid rgba(148, 163, 184, 0.24);
        background: rgba(17, 24, 39, 0.85);
    }

    /* DataFrame rounded container */
    [data-testid="stDataFrame"] {
        border: 1px solid rgba(148, 163, 184, 0.16);
        border-radius: 14px;
        overflow: hidden;
    }

    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            transition: none !important;
            animation: none !important;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# DataFrame & Export Builders
# ---------------------------------------------------------------------------

BROKEN_COLUMNS = [
    "Source Page",
    "Anchor Text",
    "Broken URL",
    "Link Type",
    "Status Category",
    "Status Code / Error",
    "Screenshot File",
]

REDIRECT_COLUMNS = [
    "Source Page",
    "Anchor Text",
    "Original URL",
    "Link Type",
    "Redirect Type",
    "Status Code",
    "Final Destination URL",
]

DEEP_ISSUE_COLUMNS = [
    "Source Page",
    "Anchor Text",
    "Original URL",
    "Tested URL",
    "Device",
    "Issue Type",
    "HTTP Status",
    "Matched Error Text",
    "Ad Simulation",
    "Source Screenshot",
    "Destination Screenshot",
]


def build_broken_df(summary: CrawlSummary) -> pd.DataFrame:
    rows = [
        {
            "Source Page": item.source_page,
            "Anchor Text": item.anchor_text,
            "Broken URL": item.broken_url,
            "Link Type": item.link_type,
            "Status Category": item.status_category,
            "Status Code / Error": item.status_or_error,
            "Screenshot File": Path(item.screenshot_path).name if item.screenshot_path else "",
        }
        for item in summary.broken_links
    ]
    return pd.DataFrame(rows, columns=BROKEN_COLUMNS)


def build_redirect_df(summary: CrawlSummary) -> pd.DataFrame:
    rows = [
        {
            "Source Page": item.source_page,
            "Anchor Text": item.anchor_text,
            "Original URL": item.original_url,
            "Link Type": item.link_type,
            "Redirect Type": item.redirect_category,
            "Status Code": item.status_code,
            "Final Destination URL": item.final_destination_url,
        }
        for item in summary.redirected_links
    ]
    return pd.DataFrame(rows, columns=REDIRECT_COLUMNS)


def build_deep_issues_df(deep_results: list[DeepCheckResult]) -> pd.DataFrame:
    issues = [r for r in deep_results if r.status == "Issue"]
    rows = [
        {
            "Source Page": item.source_page,
            "Anchor Text": item.anchor_text,
            "Original URL": item.original_url,
            "Tested URL": item.tested_url,
            "Device": item.device,
            "Issue Type": item.issue_type,
            "HTTP Status": str(item.http_status) if item.http_status is not None else "-",
            "Matched Error Text": item.matched_error_text,
            "Ad Simulation": (
                "Click Simulated"
                if item.click_simulated
                else ("Ad Params Direct" if item.ad_simulation_used else "No")
            ),
            "Source Screenshot": Path(item.source_screenshot_path).name if item.source_screenshot_path else "",
            "Destination Screenshot": Path(item.screenshot_path).name if item.screenshot_path else "",
        }
        for item in issues
    ]
    return pd.DataFrame(rows, columns=DEEP_ISSUE_COLUMNS)


def build_deep_all_df(deep_results: list[DeepCheckResult]) -> pd.DataFrame:
    rows = [
        {
            "Status": item.status,
            "Issue Type": item.issue_type,
            "Source Page": item.source_page,
            "Anchor Text": item.anchor_text,
            "Original URL": item.original_url,
            "Tested URL": item.tested_url,
            "Device": item.device,
            "Visible Chars": item.text_length,
            "HTTP Status": item.http_status if item.http_status is not None else "-",
            "Ad Simulation": (
                "Click Simulated"
                if item.click_simulated
                else ("Ad Params Direct" if item.ad_simulation_used else "No")
            ),
            "Matched Error Text": item.matched_error_text,
            "Notes": item.notes,
        }
        for item in deep_results
    ]
    return pd.DataFrame(rows)


def build_excel_bytes(
    broken_df: pd.DataFrame,
    redirect_df: pd.DataFrame,
    deep_issues_df: pd.DataFrame,
) -> bytes:
    """Create a formatted 3-sheet Excel workbook in memory."""
    output = io.BytesIO()
    sheets = {
        "Broken_Links": broken_df,
        "Redirected_Links": redirect_df,
        "Deep_Check_Issues": deep_issues_df,
    }

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            ws = writer.sheets[sheet_name]
            ws.freeze_panes = "A2"

            for col_idx, col_name in enumerate(df.columns, start=1):
                cell = ws.cell(row=1, column=col_idx)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="left", vertical="center")

                max_len = len(str(col_name))
                if not df.empty:
                    col_max = df[col_name].astype(str).map(len).max()
                    max_len = max(max_len, int(col_max))
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 4, 60)

            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)

    return output.getvalue()


def build_combined_csv_bytes(
    broken_df: pd.DataFrame,
    redirect_df: pd.DataFrame,
    deep_issues_df: pd.DataFrame,
) -> bytes:
    """Create a unified UTF-8-SIG CSV combining all 3 issue categories."""
    frames: list[pd.DataFrame] = []

    if not broken_df.empty:
        b = broken_df.copy()
        b.insert(0, "Category", "Broken Link")
        b = b.rename(
            columns={
                "Broken URL": "URL",
                "Status Category": "Sub-Status",
                "Status Code / Error": "Detail / Status",
            }
        )
        frames.append(b)

    if not redirect_df.empty:
        r = redirect_df.copy()
        r.insert(0, "Category", "Redirected Link")
        r = r.rename(
            columns={
                "Original URL": "URL",
                "Redirect Type": "Sub-Status",
                "Status Code": "Detail / Status",
            }
        )
        frames.append(r)

    if not deep_issues_df.empty:
        d = deep_issues_df.copy()
        d.insert(0, "Category", "Deep Check Issue")
        d = d.rename(
            columns={
                "Tested URL": "URL",
                "Issue Type": "Sub-Status",
                "Matched Error Text": "Detail / Status",
            }
        )
        frames.append(d)

    if frames:
        combined = pd.concat(frames, ignore_index=True, sort=False)
    else:
        combined = pd.DataFrame(
            columns=["Category", "Sub-Status", "Source Page", "Anchor Text", "URL", "Detail / Status"]
        )

    return combined.to_csv(index=False).encode("utf-8-sig")


# ---------------------------------------------------------------------------
# UI Rendering Helpers
# ---------------------------------------------------------------------------

def render_kpi_card(label: str, value: int, subtitle: str = "", tone: str = "info") -> None:
    tone_cls = f"kpi-{tone}" if tone else "kpi-info"
    st.markdown(
        f"""
        <div class="kpi-card {tone_cls}">
            <div class="kpi-value">{value:,}</div>
            <div class="kpi-label">{label}</div>
            <div class="kpi-sub">{subtitle}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_status_chips(counts: Counter) -> None:
    if not counts:
        return
    chips = "".join(
        f'<span class="status-chip">{label}: <strong>{cnt}</strong></span>'
        for label, cnt in counts.most_common()
    )
    st.markdown(f'<div class="status-strip">{chips}</div>', unsafe_allow_html=True)


def filter_df_by_search(df: pd.DataFrame, query: str) -> pd.DataFrame:
    q = (query or "").strip().lower()
    if not q or df.empty:
        return df
    mask = df.apply(lambda row: any(q in str(val).lower() for val in row), axis=1)
    return df[mask]


def _broken_issue_matches_filters(
    item: BrokenLinkIssue,
    selected_statuses: list[str],
    link_type: str,
    search_text: str,
) -> bool:
    if selected_statuses and item.status_category not in selected_statuses:
        return False
    if link_type != "ทั้งหมด" and item.link_type != link_type:
        return False
    q = (search_text or "").strip().lower()
    if q:
        haystack = " ".join(
            [
                item.source_page,
                item.anchor_text,
                item.broken_url,
                item.link_type,
                item.status_category,
                item.status_or_error,
            ]
        ).lower()
        if q not in haystack:
            return False
    return True


def _deep_issue_matches_filters(
    item: DeepCheckResult,
    selected_types: list[str],
    ad_filter: str,
    search_text: str,
) -> bool:
    if selected_types and item.issue_type not in selected_types:
        return False
    if ad_filter == "Ad Simulation Only" and not item.ad_simulation_used:
        return False
    if ad_filter == "Non-Ad Only" and item.ad_simulation_used:
        return False
    q = (search_text or "").strip().lower()
    if q:
        haystack = " ".join(
            [
                item.source_page,
                item.anchor_text,
                item.original_url,
                item.tested_url,
                item.issue_type,
                item.matched_error_text,
                str(item.http_status or ""),
                item.device,
            ]
        ).lower()
        if q not in haystack:
            return False
    return True


def _get_cached_scan_history(
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, dict, str | None]:
    """Load scan history once per session unless refreshed or invalidated by a new scan."""
    if force_refresh or "scan_history_cache" not in st.session_state:
        st.session_state["scan_history_cache"] = load_scan_history(limit=300)
    return st.session_state["scan_history_cache"]


def render_scan_history_panel(key_prefix: str = "hist") -> None:
    """Render the Timestamp & Scan History panel (Google Sheets or Local CSV)."""
    st.subheader("🕒 ประวัติการตรวจสอบเว็บไซต์ (Scan History & Timestamp)")

    history_df, backend_info, load_err = _get_cached_scan_history()

    sheet_url = (backend_info.get("details") or {}).get("sheet_url", "")
    if backend_info.get("is_cloud_persistent"):
        st.success(f"☁️ กำลังบันทึกและดึงประวัติถาวรจาก **{backend_info['label']}** (เวลาประเทศไทย UTC+7)")
    else:
        st.info(
            f"💾 กำลังใช้งาน **{backend_info['label']}** (เวลาประเทศไทย UTC+7) — "
            "หากรันบน Streamlit Cloud แนะนำเชื่อมต่อ **Google Sheets** ใน Secrets เพื่อไม่ให้ประวัติหายเมื่อแอป Sleep"
        )

    btn_cols = st.columns([1.0, 1.3, 1.2, 1.0]) if sheet_url else st.columns([1.0, 1.35, 1.25])
    with btn_cols[0]:
        if st.button("🔄 รีเฟรชประวัติ", key=f"{key_prefix}_refresh", width="stretch"):
            history_df, backend_info, load_err = _get_cached_scan_history(force_refresh=True)
            st.rerun()
    with btn_cols[1]:
        if st.button("🔌 ทดสอบการเชื่อมต่อ Google Sheets", key=f"{key_prefix}_test", width="stretch"):
            ok, msg = test_gsheets_connection()
            if ok:
                st.success(f"✅ {msg}")
            else:
                st.warning(f"⚠️ {msg}")
    with btn_cols[2]:
        st.download_button(
            label="⬇️ ดาวน์โหลดประวัติ (.csv)",
            data=history_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"scan_history_{now_thailand_slug()}.csv",
            mime="text/csv",
            key=f"{key_prefix}_download",
            width="stretch",
        )
    if sheet_url:
        with btn_cols[3]:
            st.link_button("📄 เปิด Google Sheet", sheet_url, width="stretch")

    if load_err:
        st.warning(f"⚠️ {load_err}")

    if history_df.empty:
        st.caption(
            "ยังไม่มีประวัติการสแกน — เมื่อกด **Start Scan** ระบบจะบันทึกวัน-เวลา (UTC+7) และเว็บไซต์ที่ตรวจสอบให้อัตโนมัติ"
        )
    else:
        unique_targets = (
            history_df["Target Hostname"]
            .where(history_df["Target Hostname"].str.strip() != "", history_df["Target Input"])
            .replace("", pd.NA)
            .dropna()
            .nunique()
        )
        latest_ts = str(history_df["Timestamp (TH)"].iloc[0])

        m1, m2, m3 = st.columns(3)
        m1.metric("จำนวนครั้งที่สแกน", f"{len(history_df):,}")
        m2.metric("เว็บไซต์ที่ตรวจแล้ว (ไม่ซ้ำ)", f"{int(unique_targets):,}")
        m3.metric("สแกนล่าสุดเมื่อ (เวลาไทย)", latest_ts)

        hf1, hf2 = st.columns([2.0, 1.0])
        with hf1:
            hist_search = st.text_input(
                "ค้นหาเว็บไซต์ / URL / วันที่",
                placeholder="เช่น example.com, 2026-11-21...",
                key=f"{key_prefix}_search",
            )
        with hf2:
            mode_options = ["ทั้งหมด"] + sorted(
                m for m in history_df["Scan Mode"].dropna().unique().tolist() if str(m).strip()
            )
            hist_mode = st.selectbox(
                "กรองตามโหมดสแกน",
                options=mode_options,
                key=f"{key_prefix}_mode",
            )

        filtered_hist = history_df.copy()
        if hist_mode != "ทั้งหมด":
            filtered_hist = filtered_hist[filtered_hist["Scan Mode"] == hist_mode]
        filtered_hist = filter_df_by_search(filtered_hist, hist_search)

        st.caption(f"กำลังแสดง **{len(filtered_hist)}** จาก **{len(history_df)}** รายการล่าสุด")
        st.dataframe(
            filtered_hist,
            width="stretch",
            hide_index=True,
        )

    if not backend_info.get("is_cloud_persistent"):
        with st.expander("☁️ วิธีตั้งค่าบันทึกประวัติลง Google Sheets ถาวร (ง่ายสุด 3 นาที)"):
            st.markdown(
                """
                1. สร้างไฟล์ **Google Sheets** ใหม่ใน Google Drive ของคุณ
                2. คลิกเมนู **ส่วนขยาย (Extensions) -> Apps Script**
                3. ลบโค้ดเดิมออก แล้วคัดลอกโค้ดจากไฟล์ `google_apps_script.gs` ในโปรเจกต์ไปวาง -> กด **Save**
                4. กด **Deploy -> New deployment -> เลือกประเภท Web app**:
                   - **Execute as:** `Me`
                   - **Who has access:** `Anyone`
                   - กด **Deploy** -> อนุญาตสิทธิ์ -> คัดลอก **Web app URL** (ลงท้ายด้วย `/exec`)
                5. เข้าหน้า **Streamlit Cloud -> Settings -> Secrets** แล้ววาง:
                   ```toml
                   GSHEET_WEBHOOK_URL = "https://script.google.com/macros/s/xxxxx/exec"
                   GSHEET_URL = "https://docs.google.com/spreadsheets/d/xxxxx/edit"
                   ```
                6. กด **Save** แล้วกดปุ่ม **🔌 ทดสอบการเชื่อมต่อ Google Sheets** ด้านบนได้เลย!
                """
            )


def _is_history_ui_enabled() -> bool:
    """
    Control whether Scan History UI is visible in the web app.
    Defaults to False so regular users never see scan logs; admins view logs
    directly in their private Google Sheet. Set SHOW_HISTORY_UI = true in
    Streamlit Secrets only if you explicitly want to display it in the UI.
    """
    raw: object = os.environ.get("SHOW_HISTORY_UI", "")
    if not str(raw).strip():
        try:
            raw = st.secrets.get("SHOW_HISTORY_UI", False)
        except Exception:
            raw = False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _get_configured_password() -> str:
    """Read optional APP_PASSWORD from environment or Streamlit Cloud secrets."""
    env_pw = os.environ.get("APP_PASSWORD", "").strip()
    if env_pw:
        return env_pw
    try:
        return str(st.secrets.get("APP_PASSWORD", "")).strip()
    except Exception:
        return ""


def _enforce_optional_password() -> None:
    """Show a login gate only when APP_PASSWORD is configured (recommended for public Cloud hosting)."""
    expected_pw = _get_configured_password()
    if not expected_pw:
        return
    if st.session_state.get("auth_verified"):
        return

    st.title("🔒 เข้าสู่ระบบ Site Link Checker")
    st.caption("แอปนี้ตั้งค่ารหัสผ่านไว้ใน Secrets เพื่อป้องกันการใช้งานจากบุคคลภายนอก")
    with st.form("app_login_form", clear_on_submit=False):
        entered = st.text_input("รหัสผ่าน (APP_PASSWORD)", type="password")
        submitted = st.form_submit_button("เข้าสู่ระบบ", type="primary")

    if submitted:
        if hmac.compare_digest(entered, expected_pw):
            st.session_state["auth_verified"] = True
            st.rerun()
        else:
            st.error("รหัสผ่านไม่ถูกต้อง")

    st.stop()


_enforce_optional_password()
SHOW_HISTORY_UI = _is_history_ui_enabled()


# ---------------------------------------------------------------------------
# Sidebar — Controls & Scan Configuration
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(
        """
        <div class="ev-brand">
            <div class="ev-brand-icon">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
                    <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
                </svg>
            </div>
            <div>
                <div class="ev-brand-title">Site Link Checker</div>
                <div class="ev-brand-sub">Audit Workspace</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.markdown('<div class="ev-card-title">1. Scan Mode</div>', unsafe_allow_html=True)
        mode_pill = st.segmented_control(
            "เลือกโหมดสแกน",
            options=["Auto-Crawl", "Sitemap", "Manual List"],
            default="Auto-Crawl",
            label_visibility="collapsed",
            help=(
                "- Auto-Crawl: ไต่ตามลิงก์ภายในจากหน้าแรก\n"
                "- Sitemap: ดึงรายชื่อหน้าจาก sitemap.xml\n"
                "- Manual List: วาง URL เฉพาะหน้าที่ต้องการตรวจทีละบรรทัด"
            ),
        ) or "Auto-Crawl"
        _mode_to_label = {
            "Auto-Crawl": "Auto-Crawl Mode",
            "Sitemap": "Sitemap Mode",
            "Manual List": "Manual List Mode",
        }
        scan_mode_label = _mode_to_label[mode_pill]

        target_url = ""
        manual_urls_text = ""

        if scan_mode_label == "Auto-Crawl Mode":
            target_url = st.text_input(
                "URL หน้าแรก (Start URL)",
                placeholder="https://example.com",
                help="ไต่เฉพาะ Hostname ที่ตรงกันเป๊ะ (Subdomain อื่นจะถูกแยกเป็น External Link)",
            )
        elif scan_mode_label == "Sitemap Mode":
            target_url = st.text_input(
                "URL ของ Sitemap หรือเว็บไซต์",
                placeholder="https://example.com/sitemap.xml หรือ https://example.com",
                help="หากกรอกเฉพาะชื่อเว็บ ระบบจะค้นหา /sitemap.xml และ robots.txt ให้อัตโนมัติ",
            )
        else:
            manual_urls_text = st.text_area(
                "รายการ URL (1 บรรทัดต่อ 1 URL)",
                height=130,
                placeholder="https://example.com/\nhttps://example.com/rooms\nhttps://example.com/promo",
            )

    with st.container(border=True):
        st.markdown('<div class="ev-card-title">2. Scope &amp; Safety</div>', unsafe_allow_html=True)

        max_depth = st.slider(
            "Max Depth (ระดับความลึกในการไต่)",
            min_value=0,
            max_value=10,
            value=3,
            disabled=(scan_mode_label != "Auto-Crawl Mode"),
            help="ใช้ในโหมด Auto-Crawl (0 = ตรวจเฉพาะหน้าแรก, 3 = ไต่ลึก 3 ชั้น)",
        )

        max_pages = st.slider(
            "Max Pages (จำนวนหน้าสูงสุด)",
            min_value=1,
            max_value=1000,
            value=150,
            step=5,
            help="จำกัดจำนวนหน้าสูงสุดที่สแกนเพื่อป้องกันการรันนานเกินไป",
        )

        check_external = st.toggle(
            "ตรวจสอบ External Links",
            value=True,
            help="ตรวจสอบสถานะปลายทางของลิงก์นอกโดเมนและ Subdomain อื่น (เช่น shop.*, booking.*) โดยไม่ไต่ต่อเข้าไป",
        )

        ignore_social_media = st.toggle(
            "ข้ามลิงก์ Social Media",
            value=True,
            help=(
                "ข้ามแพลตฟอร์ม Social Media เช่น Facebook, Instagram, TikTok, X/Twitter, LINE, "
                "LinkedIn, YouTube, WhatsApp, Telegram เพื่อลด False Positive จากระบบกัน Bot (เช่น 400 / 403)"
            ),
        )

        ignored_domains_text = ", ".join(sorted(DEFAULT_SOCIAL_MEDIA_DOMAINS))
        if ignore_social_media:
            with st.expander("รายชื่อโดเมน Social Media ที่ข้าม"):
                ignored_domains_text = st.text_area(
                    "โดเมนที่ข้าม (คั่นด้วยจุลภาคหรือขึ้นบรรทัดใหม่)",
                    value=ignored_domains_text,
                    height=110,
                    help="ระบบจะข้ามทั้งโดเมนหลักและ Subdomain ของโดเมนเหล่านี้โดยอัตโนมัติ",
                )

        capture_404_highlights = st.toggle(
            "แคปภาพ Highlight ปุ่มเสีย (404)",
            value=True,
            help=(
                "เมื่อพบลิงก์ 404 Not Found ระบบจะเปิดหน้าต้นทางเพื่อวง/Highlight "
                "ปุ่มหรือลิงก์ที่มีปัญหาพร้อมแคป Screenshot (ทำงานเฉพาะเคส 404 เท่านั้น)"
            ),
        )

        screencap_404_device = "Desktop"
        max_404_screenshots = 30
        if capture_404_highlights:
            with st.expander("ตั้งค่าการแคปภาพ 404 Highlight"):
                screencap_404_device = st.selectbox(
                    "ขนาดหน้าจอสำหรับแคปปุ่ม 404",
                    options=["Desktop", "Mobile (iPhone 14)"],
                    index=0,
                    help="แนะนำ Desktop เพื่อให้เห็นปุ่มบนแถบเมนูหลักได้ครบโดยไม่ถูกซ่อนใน Mobile Menu",
                )
                max_404_screenshots = st.slider(
                    "จำนวนภาพ 404 สูงสุดต่อการสแกน",
                    min_value=5,
                    max_value=100,
                    value=30,
                    step=5,
                )

    with st.container(border=True):
        st.markdown('<div class="ev-card-title">3. Deep Check</div>', unsafe_allow_html=True)

        enable_deep_check = st.toggle(
            "เปิดใช้งาน Deep Check (หน้าขาว / Soft 404)",
            value=False,
            help="เปิดหน้าเว็บจริงด้วย Headless Browser เพื่อรอ JavaScript ทำงานและจับหน้าขาว/Soft 404 พร้อมแคปภาพหน้าจอ",
        )

        device_profile = "Mobile (iPhone 14)"
        button_keywords_raw = ", ".join(DEFAULT_BUTTON_KEYWORDS)
        manual_deep_scope = "all_manual"
        soft_404_keywords_raw = "\n".join(DEFAULT_SOFT_404_KEYWORDS)
        simulate_ad_params = False
        ad_params_input = DEFAULT_AD_PARAMS
        max_deep_targets = 30
        extra_wait_sec = 2.5

        if enable_deep_check:
            device_profile = st.selectbox(
                "Device Profile",
                options=list(DEVICE_PROFILES.keys()),
                index=0,
                help="เลือกจำลองหน้าจอและ User-Agent แบบ Mobile (iPhone 14) หรือ Desktop",
            )

            if scan_mode_label == "Manual List Mode":
                manual_scope_label = st.radio(
                    "ขอบเขต Deep Check (Manual List)",
                    options=[
                        "ตรวจทุก URL ใน Manual List",
                        "เฉพาะปุ่ม/ลิงก์ที่ตรงกับ Button Keyword",
                        "ทั้งสองอย่าง (ทุก URL + ปุ่มที่ตรง Keyword)",
                    ],
                    index=0,
                )
                manual_scope_map = {
                    "ตรวจทุก URL ใน Manual List": "all_manual",
                    "เฉพาะปุ่ม/ลิงก์ที่ตรงกับ Button Keyword": "keywords_only",
                    "ทั้งสองอย่าง (ทุก URL + ปุ่มที่ตรง Keyword)": "both",
                }
                manual_deep_scope = manual_scope_map[manual_scope_label]

            button_keywords_raw = st.text_input(
                "Button Keyword Filter (คั่นด้วยจุลภาค)",
                value=", ".join(DEFAULT_BUTTON_KEYWORDS),
                help="Deep Check จะเลือกรันเฉพาะลิงก์ที่ Anchor Text มีคำเหล่านี้ (ไม่สนตัวพิมพ์เล็ก-ใหญ่)",
                disabled=(scan_mode_label == "Manual List Mode" and manual_deep_scope == "all_manual"),
            )

            simulate_ad_params = st.toggle(
                "จำลองพารามิเตอร์โฆษณา (?gclid=...&utm_...)",
                value=False,
                help="แนบพารามิเตอร์โฆษณาเข้าหน้าต้นทาง แล้วจำลองการคลิกปุ่มปลายทางจริงเพื่อตรวจว่าระบบปลายทางค้างขาวหรือขึ้น Error หรือไม่",
            )

            if simulate_ad_params:
                ad_params_input = st.text_input(
                    "พารามิเตอร์โฆษณาทดสอบ",
                    value=DEFAULT_AD_PARAMS,
                    help="Query string ที่จะแนบเข้า URL ทดสอบ (ไม่ต้องใส่เครื่องหมาย ? นำหน้าก็ได้)",
                )

            with st.expander("ตั้งค่าคำคีย์เวิร์ด Soft 404 & ขั้นสูง"):
                soft_404_keywords_raw = st.text_area(
                    "คำคีย์เวิร์ด Soft 404 (1 คำต่อ 1 บรรทัด)",
                    value="\n".join(DEFAULT_SOFT_404_KEYWORDS),
                    height=140,
                    help="สามารถเพิ่มหรือแก้ไขข้อความ Error ที่ต้องการให้ระบบตรวจจับได้อิสระ",
                )
                max_deep_targets = st.slider(
                    "จำนวน URL สูงสุดสำหรับ Deep Check",
                    min_value=1,
                    max_value=200,
                    value=30,
                    help="จำกัดจำนวนหน้าที่จะเปิดด้วย Headless Browser เพื่อคุมเวลาการทำงาน",
                )
                extra_wait_sec = st.slider(
                    "รอเพิ่มหลังหน้าโหลดเสร็จ (วินาที)",
                    min_value=1.0,
                    max_value=6.0,
                    value=2.5,
                    step=0.5,
                    help="เผื่อเวลาสำหรับ Loading Spinner หรือ SPA JavaScript เรนเดอร์ให้เสร็จ",
                )

    start_scan = st.button("Start Scan  →", type="primary", width="stretch")
    if SHOW_HISTORY_UI:
        _sidebar_storage = get_storage_backend_info()
        if _sidebar_storage["is_cloud_persistent"]:
            st.caption(f"☁️ Log ประวัติ: **{_sidebar_storage['label']}**")
        else:
            st.caption("💾 Log ประวัติ: **Local CSV** (ตั้งค่า Google Sheets ใน Secrets เพื่อเก็บถาวร)")


# ---------------------------------------------------------------------------
# Main Header & Scan Execution
# ---------------------------------------------------------------------------

_existing_report = st.session_state.get("scan_report")
if _existing_report and not start_scan:
    _rep_summary: CrawlSummary = _existing_report["summary"]
    _host_pill = f" · Strict Hostname: {_rep_summary.target_hostname}" if _rep_summary.target_hostname else ""
    st.markdown(
        f"""
        <div class="ev-hero">
            <div class="ev-status-pill">
                <span class="ev-dot ev-dot-ok"></span>
                <span>Scan Completed · {_existing_report['timestamp']} (UTC+7){_host_pill}</span>
            </div>
            <h1 class="ev-hero-title">Site Audit <span class="ev-grad-text">Overview</span></h1>
            <p class="ev-hero-sub">
                สรุปผลตรวจสอบลิงก์เสีย Redirect และ Deep Check แยกหมวดหมู่สถานะพร้อมหลักฐานภาพหน้าจอ 404 Highlight
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        """
        <div class="ev-hero">
            <div class="ev-status-pill">
                <span class="ev-dot ev-dot-rose"></span>
                <span>Fast Broken Link, Redirect &amp; Soft 404 Inspector</span>
            </div>
            <h1 class="ev-hero-title">Broken Link, Redirect &amp; <span class="ev-grad-text">Soft 404 Checker</span></h1>
            <p class="ev-hero-sub">
                ตรวจสอบลิงก์เสีย แยกสถานะชัดเจนพร้อมตัวกรอง ข้าม Social Media อัตโนมัติ
                แคปภาพ Highlight ปุ่มที่มีปัญหาเฉพาะ 404 และตรวจจับหน้าขาว / Soft 404 ในคลิกเดียว
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

if start_scan:
    if scan_mode_label in ("Auto-Crawl Mode", "Sitemap Mode") and not target_url.strip():
        st.error("กรุณากรอก URL เป้าหมายก่อนกด Start Scan")
    elif scan_mode_label == "Manual List Mode" and not manual_urls_text.strip():
        st.error("กรุณาวางรายการ URL อย่างน้อย 1 บรรทัดก่อนกด Start Scan")
    else:
        scan_timestamp = now_thailand_str()
        formatted_target = format_target_input(scan_mode_label, target_url, manual_urls_text)
        progress_bar = st.progress(0.0)
        status_box = st.empty()
        scan_warnings: list[str] = []

        parsed_ignored_domains = (
            parse_domain_list(ignored_domains_text) if ignore_social_media else set()
        )

        # Progress bar allocation across Standard Crawl, 404 Screencap, and Deep Check
        if enable_deep_check and capture_404_highlights:
            std_start, std_span = 0.0, 0.55
            cap_start, cap_span = 0.55, 0.15
            deep_start, deep_span = 0.70, 0.30
        elif enable_deep_check:
            std_start, std_span = 0.0, 0.65
            cap_start, cap_span = 0.65, 0.0
            deep_start, deep_span = 0.65, 0.35
        elif capture_404_highlights:
            std_start, std_span = 0.0, 0.80
            cap_start, cap_span = 0.80, 0.20
            deep_start, deep_span = 1.0, 0.0
        else:
            std_start, std_span = 0.0, 1.0
            cap_start, cap_span = 1.0, 0.0
            deep_start, deep_span = 1.0, 0.0

        def _update_std_progress(msg: str, frac: float) -> None:
            clamped = std_start + max(0.0, min(1.0, frac)) * std_span
            progress_bar.progress(clamped)
            status_box.info(f"⏳ {msg}")

        try:
            if scan_mode_label == "Auto-Crawl Mode":
                crawl_summary = run_auto_crawl(
                    start_url=target_url.strip(),
                    max_depth=max_depth,
                    max_pages=max_pages,
                    check_external=check_external,
                    ignore_social_media=ignore_social_media,
                    ignored_domains=parsed_ignored_domains,
                    progress_cb=_update_std_progress,
                )
            elif scan_mode_label == "Sitemap Mode":
                crawl_summary = run_sitemap_scan(
                    sitemap_input=target_url.strip(),
                    max_pages=max_pages,
                    check_external=check_external,
                    ignore_social_media=ignore_social_media,
                    ignored_domains=parsed_ignored_domains,
                    progress_cb=_update_std_progress,
                )
            else:
                crawl_summary = run_manual_list_scan(
                    raw_urls_text=manual_urls_text,
                    max_pages=max_pages,
                    check_external=check_external,
                    ignore_social_media=ignore_social_media,
                    ignored_domains=parsed_ignored_domains,
                    progress_cb=_update_std_progress,
                )

            scan_warnings.extend(crawl_summary.warnings)

            # Capture highlighted screenshots ONLY for HTTP 404 links
            has_404 = any(item.status_code == 404 for item in crawl_summary.broken_links)
            if capture_404_highlights and has_404:
                def _update_404_progress(msg: str, frac: float) -> None:
                    clamped = cap_start + max(0.0, min(1.0, frac)) * cap_span
                    progress_bar.progress(min(1.0, clamped))
                    status_box.info(f"📸 {msg}")

                try:
                    capture_404_screenshots(
                        broken_links=crawl_summary.broken_links,
                        device_name=screencap_404_device,
                        screenshot_dir=SCREENSHOT_DIR,
                        max_screenshots=max_404_screenshots,
                        progress_cb=_update_404_progress,
                    )
                except Exception as sc_err:
                    scan_warnings.append(f"ไม่สามารถแคปภาพ Highlight 404 ได้: {sc_err}")

            deep_results: list[DeepCheckResult] = []
            if enable_deep_check:
                btn_keywords = parse_keywords(button_keywords_raw)
                s404_keywords = parse_keywords(soft_404_keywords_raw)

                deep_targets = select_deep_check_targets(
                    crawl_summary=crawl_summary,
                    button_keywords=btn_keywords,
                    manual_scope=manual_deep_scope,
                    check_external=check_external,
                    ignore_social_media=ignore_social_media,
                    ignored_domains=parsed_ignored_domains,
                    max_targets=max_deep_targets,
                )

                if not deep_targets:
                    scan_warnings.append(
                        "Deep Check เปิดใช้งานอยู่ แต่ไม่พบปุ่ม/ลิงก์ที่ตรงกับ Button Keyword Filter "
                        f"({', '.join(btn_keywords) or 'ว่าง'})"
                    )
                else:
                    def _update_deep_progress(msg: str, frac: float) -> None:
                        clamped = deep_start + max(0.0, min(1.0, frac)) * deep_span
                        progress_bar.progress(min(1.0, clamped))
                        status_box.info(f"🕵️ {msg}")

                    deep_results = run_deep_check(
                        targets=deep_targets,
                        device_name=device_profile,
                        soft_404_keywords=s404_keywords,
                        simulate_ad_params=simulate_ad_params,
                        ad_params_str=ad_params_input,
                        screenshot_dir=SCREENSHOT_DIR,
                        wait_extra_ms=int(extra_wait_sec * 1000),
                        progress_cb=_update_deep_progress,
                    )

            progress_bar.progress(1.0)
            status_box.success("✅ การตรวจสอบเสร็จสมบูรณ์!")

            deep_issue_count = sum(1 for r in deep_results if r.status == "Issue")
            log_entry = ScanLogEntry(
                timestamp=scan_timestamp,
                scan_mode=scan_mode_label,
                target_input=formatted_target,
                target_hostname=crawl_summary.target_hostname or extract_fallback_hostname(formatted_target),
                pages_scanned=crawl_summary.pages_scanned,
                total_links_checked=crawl_summary.total_links_checked,
                broken_links=len(crawl_summary.broken_links),
                redirected_links=len(crawl_summary.redirected_links),
                deep_check_issues=deep_issue_count,
                deep_check_enabled=enable_deep_check,
                status="Completed",
                notes=f"Warnings: {len(scan_warnings)}" if scan_warnings else "",
            )
            log_ok, log_backend, log_msg = record_scan_history(log_entry)
            if not log_ok:
                logger.warning("Scan history log failed (%s): %s", log_backend, log_msg)
            st.session_state.pop("scan_history_cache", None)

            st.session_state["scan_report"] = {
                "timestamp": scan_timestamp,
                "summary": crawl_summary,
                "deep_results": deep_results,
                "deep_enabled": enable_deep_check,
                "warnings": scan_warnings,
                "log_status": (log_ok, log_backend, log_msg),
            }
            _scan_succeeded = True
        except Exception as exc:
            _scan_succeeded = False
            progress_bar.empty()
            status_box.error(f"เกิดข้อผิดพลาดระหว่างการสแกน: {exc}")
            err_entry = ScanLogEntry(
                timestamp=scan_timestamp,
                scan_mode=scan_mode_label,
                target_input=formatted_target,
                target_hostname=extract_fallback_hostname(formatted_target),
                deep_check_enabled=enable_deep_check,
                status="Error",
                notes=str(exc)[:300],
            )
            err_ok, err_backend, err_msg = record_scan_history(err_entry)
            if not err_ok:
                logger.warning("Error scan history log failed (%s): %s", err_backend, err_msg)
            st.session_state.pop("scan_history_cache", None)
        if _scan_succeeded:
            st.rerun()


# ---------------------------------------------------------------------------
# Report Dashboard
# ---------------------------------------------------------------------------

report = st.session_state.get("scan_report")

if not report:
    st.markdown(
        """
        <div class="ev-cta-hint">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
            </svg>
            <span>ตั้งค่าโหมดการสแกนและกรอก URL ทางแถบควบคุมด้านซ้าย จากนั้นกด <strong>Start Scan →</strong> เพื่อเริ่มตรวจสอบเว็บไซต์</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown(
            """
            <div class="ev-feature-card">
                <div class="ev-feature-icon ev-fi-violet">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>
                        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
                    </svg>
                </div>
                <div class="ev-feature-title">1. Multi-Mode &amp; Clean Scope</div>
                <ul class="ev-feature-list">
                    <li><strong>Auto-Crawl / Sitemap / Manual List</strong> รองรับครบ 3 โหมด</li>
                    <li><strong>Strict Hostname:</strong> แยก Subdomain เป็น External Link อัตโนมัติ</li>
                    <li><strong>Ignore Social Media:</strong> ข้าม Facebook, IG, TikTok, X, LINE ลด False Positive</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_b:
        st.markdown(
            """
            <div class="ev-feature-card">
                <div class="ev-feature-icon ev-fi-rose">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                        <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
                    </svg>
                </div>
                <div class="ev-feature-title">2. Status Separation &amp; 404 Highlight</div>
                <ul class="ev-feature-list">
                    <li>แยกกลุ่มสถานะชัดเจน (<code>404</code>, <code>5xx</code>, <code>Timeout</code>, <code>403/429</code>, <code>301/302</code>) พร้อมตัวกรอง</li>
                    <li>แคปภาพพร้อม <strong>วง/Highlight ปุ่มที่เสีย</strong> บนหน้าต้นทาง <strong>เฉพาะ 404 เท่านั้น</strong></li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_c:
        st.markdown(
            """
            <div class="ev-feature-card">
                <div class="ev-feature-icon ev-fi-indigo">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="5" y="2" width="14" height="20" rx="2" ry="2"/>
                        <line x1="12" y1="18" x2="12.01" y2="18"/>
                    </svg>
                </div>
                <div class="ev-feature-title">3. Deep Check &amp; Ad Simulation</div>
                <ul class="ev-feature-list">
                    <li>จำลอง <strong>Mobile (iPhone 14)</strong> หรือ <strong>Desktop</strong> พร้อมรอ JS เรนเดอร์เสร็จ</li>
                    <li>จับ <strong>Soft 404</strong> (Highlight ต้นทาง + จุด Error ปลายทาง) และ <strong>Blank Page</strong></li>
                    <li>จำลองคลิกปุ่มจริงพร้อม <code>?gclid=...&amp;utm_...</code> และ Export Excel / CSV</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if SHOW_HISTORY_UI:
        st.divider()
        render_scan_history_panel(key_prefix="home_hist")
else:
    summary: CrawlSummary = report["summary"]
    deep_results: list[DeepCheckResult] = report["deep_results"]
    deep_enabled: bool = report["deep_enabled"]
    warnings: list[str] = report["warnings"]

    broken_df = build_broken_df(summary)
    redirect_df = build_redirect_df(summary)
    deep_issues_df = build_deep_issues_df(deep_results)
    deep_issues_list = [r for r in deep_results if r.status == "Issue"]

    if SHOW_HISTORY_UI:
        log_status = report.get("log_status")
        if log_status:
            log_ok, _log_backend, log_msg = log_status
            if log_ok:
                st.caption(f"📝 {log_msg}")
            else:
                st.warning(f"⚠️ {log_msg}")

    if summary.skipped_social_count > 0:
        st.info(
            f"🛡️ ข้ามลิงก์ Social Media จำนวน **{summary.skipped_social_count:,}** ลิงก์อัตโนมัติ "
            "(เพื่อลด False Positive จากระบบป้องกัน Bot เช่น Facebook 400 / 403)"
        )

    for warn in warnings:
        st.warning(f"⚠️ {warn}")

    # Build concise status breakdown subtitles for KPI Cards
    broken_counts = Counter(item.status_category for item in summary.broken_links)
    broken_sub = (
        " | ".join(f"{k.split(' ', 1)[0]}: {v}" for k, v in broken_counts.most_common(4))
        if broken_counts
        else "ไม่พบลิงก์เสีย"
    )

    redirect_counts = Counter(item.redirect_category for item in summary.redirected_links)
    redirect_sub = (
        " | ".join(f"{k.split(' ', 1)[0]}: {v}" for k, v in redirect_counts.most_common(4))
        if redirect_counts
        else "ไม่พบ Redirect"
    )

    deep_counts = Counter(item.issue_type for item in deep_issues_list)
    if not deep_enabled:
        deep_sub = "Deep Check ปิดอยู่"
    elif deep_counts:
        deep_sub = " | ".join(f"{k}: {v}" for k, v in deep_counts.most_common(4))
    else:
        deep_sub = f"ผ่านทั้งหมด ({len(deep_results)} URL)"

    # KPI Summary Cards
    k1, k2, k3, k4, k5 = st.columns(5)
    with k1:
        render_kpi_card("Pages Scanned", summary.pages_scanned, "หน้าที่สแกนทั้งหมด", tone="info")
    with k2:
        total_sub = (
            f"ข้าม Social: {summary.skipped_social_count:,}"
            if summary.skipped_social_count > 0
            else "ลิงก์ที่ตรวจทั้งหมด"
        )
        render_kpi_card("Total Links", summary.total_links_checked, total_sub, tone="info")
    with k3:
        render_kpi_card(
            "Broken / Error",
            len(broken_df),
            broken_sub,
            tone="danger" if len(broken_df) > 0 else "ok",
        )
    with k4:
        render_kpi_card(
            "Redirected",
            len(redirect_df),
            redirect_sub,
            tone="warn" if len(redirect_df) > 0 else "ok",
        )
    with k5:
        render_kpi_card(
            "Deep Check Issues",
            len(deep_issues_df),
            deep_sub,
            tone="purple" if len(deep_issues_df) > 0 else "ok",
        )

    st.markdown("")

    # Export Toolbar
    ts_slug = now_thailand_slug()
    excel_bytes = build_excel_bytes(broken_df, redirect_df, deep_issues_df)
    combined_csv_bytes = build_combined_csv_bytes(broken_df, redirect_df, deep_issues_df)

    st.markdown('<div class="ev-toolbar-label">Export Toolbar</div>', unsafe_allow_html=True)
    exp_col1, exp_col2, exp_col3, exp_col4, exp_col5 = st.columns([1.45, 1.3, 1.05, 1.1, 1.1])
    with exp_col1:
        st.download_button(
            label="Export Excel (.xlsx — 3 Sheets)",
            data=excel_bytes,
            file_name=f"site_audit_report_{ts_slug}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch",
        )
    with exp_col2:
        st.download_button(
            label="Export All Issues (.csv)",
            data=combined_csv_bytes,
            file_name=f"site_audit_all_issues_{ts_slug}.csv",
            mime="text/csv",
            width="stretch",
        )
    with exp_col3:
        st.download_button(
            label="Broken CSV",
            data=broken_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"broken_links_{ts_slug}.csv",
            mime="text/csv",
            width="stretch",
        )
    with exp_col4:
        st.download_button(
            label="Redirects CSV",
            data=redirect_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"redirected_links_{ts_slug}.csv",
            mime="text/csv",
            width="stretch",
        )
    with exp_col5:
        st.download_button(
            label="Deep Check CSV",
            data=deep_issues_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"deep_check_issues_{ts_slug}.csv",
            mime="text/csv",
            width="stretch",
        )

    st.markdown("")

    # Report Tabs (Scan History tab is hidden from regular users by default)
    report_tab_labels = [
        f"Broken / Error Links ({len(broken_df)})",
        f"Redirected Links ({len(redirect_df)})",
        f"Deep Check Issues ({len(deep_issues_df)})",
    ]
    if SHOW_HISTORY_UI:
        report_tab_labels.append("Scan History")
        tab1, tab2, tab3, tab4 = st.tabs(report_tab_labels)
    else:
        tab1, tab2, tab3 = st.tabs(report_tab_labels)
        tab4 = None

    # --- Tab 1: Broken Links ---
    with tab1:
        if broken_df.empty:
            st.success("🎉 ไม่พบ Broken Links ในการสแกนครั้งนี้")
        else:
            render_status_chips(broken_counts)

            available_broken_statuses = list(broken_counts.keys())
            f1, f2, f3 = st.columns([1.8, 1.0, 1.5])
            with f1:
                selected_broken_statuses = st.multiselect(
                    "กรองตามสถานะ (Status Category)",
                    options=available_broken_statuses,
                    default=available_broken_statuses,
                    key="broken_status_filter",
                )
            with f2:
                link_type_filter = st.selectbox(
                    "กรองตามประเภทลิงก์",
                    options=["ทั้งหมด", "Internal", "External"],
                    key="broken_type_filter",
                )
            with f3:
                broken_search = st.text_input(
                    "ค้นหา URL / Anchor / Error",
                    placeholder="เช่น 404, booking, timeout...",
                    key="broken_search",
                )

            filtered_broken = broken_df.copy()
            if selected_broken_statuses:
                filtered_broken = filtered_broken[
                    filtered_broken["Status Category"].isin(selected_broken_statuses)
                ]
            else:
                filtered_broken = filtered_broken.iloc[0:0]

            if link_type_filter != "ทั้งหมด":
                filtered_broken = filtered_broken[filtered_broken["Link Type"] == link_type_filter]

            filtered_broken = filter_df_by_search(filtered_broken, broken_search)

            st.caption(f"กำลังแสดง **{len(filtered_broken)}** จาก **{len(broken_df)}** รายการ")
            st.dataframe(
                filtered_broken,
                width="stretch",
                hide_index=True,
                column_config={
                    "Source Page": st.column_config.LinkColumn("Source Page"),
                    "Broken URL": st.column_config.LinkColumn("Broken URL"),
                },
            )

            # 404 Highlighted Screenshots Gallery (Strictly 404 only)
            matching_404_with_shots = [
                item
                for item in summary.broken_links
                if item.status_code == 404
                and item.screenshot_path
                and Path(item.screenshot_path).exists()
                and _broken_issue_matches_filters(
                    item,
                    selected_broken_statuses,
                    link_type_filter,
                    broken_search,
                )
            ]

            if matching_404_with_shots:
                st.subheader(
                    f"📸 ภาพหน้าจอ Highlight ปุ่ม/จุดที่มีปัญหา (เฉพาะ 404 เท่านั้น — {len(matching_404_with_shots)} รายการ)"
                )
                for idx, item in enumerate(matching_404_with_shots, start=1):
                    with st.container(border=True):
                        c_info, c_img = st.columns([1.15, 1.0])
                        with c_info:
                            badges_html = (
                                '<span class="issue-badge badge-soft404">404 Not Found</span>'
                                f'<span class="issue-badge badge-device">{item.link_type}</span>'
                            )
                            st.markdown(f"**#{idx}** &nbsp; {badges_html}", unsafe_allow_html=True)
                            st.markdown(f"- **Source Page:** {item.source_page}")
                            st.markdown(f"- **Anchor Text:** `{item.anchor_text}`")
                            st.markdown(f"- **Broken URL (404):** [{item.broken_url}]({item.broken_url})")
                            st.markdown(f"- **Status:** `{item.status_or_error}`")
                        with c_img:
                            st.image(
                                item.screenshot_path,
                                caption=Path(item.screenshot_path).name,
                                width="stretch",
                            )

    # --- Tab 2: Redirected Links ---
    with tab2:
        if redirect_df.empty:
            st.success("🎉 ไม่พบ Redirected Links (301/302/307/308) ในการสแกนครั้งนี้")
        else:
            render_status_chips(redirect_counts)

            available_redirect_types = list(redirect_counts.keys())
            r1, r2, r3 = st.columns([1.8, 1.0, 1.5])
            with r1:
                selected_redirect_types = st.multiselect(
                    "กรองตามประเภท Redirect",
                    options=available_redirect_types,
                    default=available_redirect_types,
                    key="redirect_type_filter",
                )
            with r2:
                redirect_link_filter = st.selectbox(
                    "กรองตามประเภทลิงก์",
                    options=["ทั้งหมด", "Internal", "External"],
                    key="redirect_link_filter",
                )
            with r3:
                redirect_search = st.text_input(
                    "ค้นหา Redirect URL / Status Code",
                    placeholder="เช่น 301, 302, /old-page...",
                    key="redirect_search",
                )

            filtered_redirect = redirect_df.copy()
            if selected_redirect_types:
                filtered_redirect = filtered_redirect[
                    filtered_redirect["Redirect Type"].isin(selected_redirect_types)
                ]
            else:
                filtered_redirect = filtered_redirect.iloc[0:0]

            if redirect_link_filter != "ทั้งหมด":
                filtered_redirect = filtered_redirect[
                    filtered_redirect["Link Type"] == redirect_link_filter
                ]

            filtered_redirect = filter_df_by_search(filtered_redirect, redirect_search)

            st.caption(f"กำลังแสดง **{len(filtered_redirect)}** จาก **{len(redirect_df)}** รายการ")
            st.dataframe(
                filtered_redirect,
                width="stretch",
                hide_index=True,
                column_config={
                    "Source Page": st.column_config.LinkColumn("Source Page"),
                    "Original URL": st.column_config.LinkColumn("Original URL"),
                    "Final Destination URL": st.column_config.LinkColumn("Final Destination URL"),
                },
            )

    # --- Tab 3: Deep Check Issues (Soft 404 / HTTP 404 / Blank Page / etc.) ---
    with tab3:
        if not deep_enabled:
            st.info(
                "ℹ️ ยังไม่ได้เปิดใช้งาน **Deep Check** ในการสแกนรอบนี้ "
                "— สามารถเปิดสวิตช์ **Deep Check** ที่แถบด้านซ้ายเพื่อตรวจหาหน้าขาว (Blank Page), Soft 404 และจำลองคลิกโฆษณา"
            )
        elif not deep_results:
            st.warning(
                "ไม่มี URL ที่ถูกส่งเข้าตรวจใน Deep Check (ลองปรับ Button Keyword Filter หรือเลือกตรวจทุก URL ใน Manual List)"
            )
        else:
            if deep_issues_df.empty:
                st.success(
                    f"🎉 ผ่านการตรวจ Deep Check ทั้งหมด {len(deep_results)} URL ไม่พบหน้าขาวหรือ Soft 404"
                )
            else:
                render_status_chips(deep_counts)

                available_deep_types = list(deep_counts.keys())
                d1, d2, d3 = st.columns([1.8, 1.0, 1.5])
                with d1:
                    selected_deep_types = st.multiselect(
                        "กรองตามสถานะปัญหา (Issue Type)",
                        options=available_deep_types,
                        default=available_deep_types,
                        key="deep_issue_type_filter",
                    )
                with d2:
                    ad_sim_filter = st.selectbox(
                        "กรองการจำลองโฆษณา",
                        options=["ทั้งหมด", "Ad Simulation Only", "Non-Ad Only"],
                        key="deep_ad_filter",
                    )
                with d3:
                    deep_search = st.text_input(
                        "ค้นหา URL / Anchor / Error",
                        placeholder="เช่น nopage, book, does not exist...",
                        key="deep_search",
                    )

                filtered_deep_df = deep_issues_df.copy()
                if selected_deep_types:
                    filtered_deep_df = filtered_deep_df[
                        filtered_deep_df["Issue Type"].isin(selected_deep_types)
                    ]
                else:
                    filtered_deep_df = filtered_deep_df.iloc[0:0]

                if ad_sim_filter == "Ad Simulation Only":
                    filtered_deep_df = filtered_deep_df[
                        filtered_deep_df["Ad Simulation"] != "No"
                    ]
                elif ad_sim_filter == "Non-Ad Only":
                    filtered_deep_df = filtered_deep_df[
                        filtered_deep_df["Ad Simulation"] == "No"
                    ]

                filtered_deep_df = filter_df_by_search(filtered_deep_df, deep_search)

                st.caption(f"กำลังแสดง **{len(filtered_deep_df)}** จาก **{len(deep_issues_df)}** ปัญหา")
                st.dataframe(
                    filtered_deep_df,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Source Page": st.column_config.LinkColumn("Source Page"),
                        "Original URL": st.column_config.LinkColumn("Original URL"),
                        "Tested URL": st.column_config.LinkColumn("Tested URL"),
                    },
                )

                filtered_deep_items = [
                    item
                    for item in deep_issues_list
                    if _deep_issue_matches_filters(
                        item,
                        selected_deep_types,
                        ad_sim_filter,
                        deep_search,
                    )
                ]

                if filtered_deep_items:
                    st.subheader("📸 ภาพหน้าจอหลักฐาน (Captured Screenshots)")
                    for idx, issue in enumerate(filtered_deep_items, start=1):
                        with st.container(border=True):
                            if issue.issue_type in ("Soft 404", "HTTP 404"):
                                badge_cls = "badge-soft404"
                            elif issue.issue_type == "Blank Page":
                                badge_cls = "badge-blank"
                            else:
                                badge_cls = "badge-neutral"

                            badges_html = (
                                f'<span class="issue-badge {badge_cls}">{issue.issue_type}</span>'
                                f'<span class="issue-badge badge-device">{issue.device}</span>'
                            )
                            if issue.ad_simulation_used:
                                sim_label = "Ad Click Simulated" if issue.click_simulated else "Ad Params Appended"
                                badges_html += f'<span class="issue-badge badge-ad">🎯 {sim_label}</span>'

                            st.markdown(f"**#{idx}** &nbsp; {badges_html}", unsafe_allow_html=True)
                            st.markdown(f"- **Source Page:** {issue.source_page}")
                            st.markdown(f"- **Anchor Text:** `{issue.anchor_text}`")
                            if issue.original_url != issue.tested_url:
                                st.markdown(f"- **Original URL (บนหน้าต้นทาง):** [{issue.original_url}]({issue.original_url})")
                            st.markdown(f"- **Tested URL (ปลายทางสุดท้าย):** [{issue.tested_url}]({issue.tested_url})")
                            st.markdown(f"- **Matched Error Text:** `{issue.matched_error_text}`")
                            st.markdown(
                                f"- **Visible Characters:** `{issue.text_length}` chars"
                                + (f" | **HTTP Status:** `{issue.http_status}`" if issue.http_status else "")
                            )
                            if issue.notes:
                                st.caption(f"📝 หมายเหตุ: {issue.notes}")

                            has_src_shot = bool(
                                issue.source_screenshot_path and Path(issue.source_screenshot_path).exists()
                            )
                            has_dest_shot = bool(
                                issue.screenshot_path and Path(issue.screenshot_path).exists()
                            )

                            if has_src_shot and has_dest_shot:
                                img_c1, img_c2 = st.columns(2)
                                with img_c1:
                                    st.image(
                                        issue.source_screenshot_path,
                                        caption=f"📍 หน้าต้นทาง (Highlight ปุ่มที่มีปัญหา) — {Path(issue.source_screenshot_path).name}",
                                        width="stretch",
                                    )
                                with img_c2:
                                    st.image(
                                        issue.screenshot_path,
                                        caption=f"🎯 หน้าปลายทาง (Highlight จุด 404) — {Path(issue.screenshot_path).name}",
                                        width="stretch",
                                    )
                            elif has_dest_shot:
                                st.image(
                                    issue.screenshot_path,
                                    caption=Path(issue.screenshot_path).name,
                                    width="stretch",
                                )
                            elif has_src_shot:
                                st.image(
                                    issue.source_screenshot_path,
                                    caption=Path(issue.source_screenshot_path).name,
                                    width="stretch",
                                )

            with st.expander(f"📋 ดูรายการที่ตรวจสอบด้วย Deep Check ทั้งหมด ({len(deep_results)} URL)"):
                all_deep_df = build_deep_all_df(deep_results)
                a1, a2 = st.columns([1, 2])
                with a1:
                    all_status_filter = st.selectbox(
                        "สถานะผลตรวจ",
                        options=["ทั้งหมด", "Issue", "OK"],
                        key="deep_all_status_filter",
                    )
                with a2:
                    all_type_options = sorted(all_deep_df["Issue Type"].unique().tolist())
                    all_type_filter = st.multiselect(
                        "ประเภทสถานะ (Issue Type)",
                        options=all_type_options,
                        default=all_type_options,
                        key="deep_all_type_filter",
                    )

                filtered_all_deep = all_deep_df.copy()
                if all_status_filter != "ทั้งหมด":
                    filtered_all_deep = filtered_all_deep[
                        filtered_all_deep["Status"] == all_status_filter
                    ]
                if all_type_filter:
                    filtered_all_deep = filtered_all_deep[
                        filtered_all_deep["Issue Type"].isin(all_type_filter)
                    ]
                else:
                    filtered_all_deep = filtered_all_deep.iloc[0:0]

                st.dataframe(
                    filtered_all_deep,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Source Page": st.column_config.LinkColumn("Source Page"),
                        "Original URL": st.column_config.LinkColumn("Original URL"),
                        "Tested URL": st.column_config.LinkColumn("Tested URL"),
                    },
                )

    # --- Tab 4: Scan History & Timestamp (only when SHOW_HISTORY_UI = true) ---
    if tab4 is not None:
        with tab4:
            render_scan_history_panel(key_prefix="report_hist")
