"""
history_logger.py — Scan History & Timestamp Logging (Thailand UTC+7).

Supports 3 storage backends automatically (in priority order):
1. Google Sheets via Service Account (`gspread`) — when `[gcp_service_account]` + `GSHEET_URL`/`GSHEET_ID` are in Secrets
2. Google Sheets via Apps Script Webhook (`GSHEET_WEBHOOK_URL`) — easiest zero-GCP setup
3. Local CSV fallback (`scan_history.csv`) — works out of the box with zero configuration
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import pandas as pd
import requests

THAILAND_TZ = timezone(timedelta(hours=7))

DEFAULT_WORKSHEET_NAME = "Scan_History"
LOCAL_HISTORY_CSV = Path(__file__).resolve().parent / "scan_history.csv"

HISTORY_COLUMNS = [
    "Timestamp (TH)",
    "Scan Mode",
    "Target Input",
    "Target Hostname",
    "Pages Scanned",
    "Total Links Checked",
    "Broken Links",
    "Redirected Links",
    "Deep Check Issues",
    "Deep Check Enabled",
    "Status",
    "Notes",
]

_CSV_LOCK = threading.Lock()


def now_thailand() -> datetime:
    """Return current datetime in Thailand timezone (UTC+7)."""
    return datetime.now(THAILAND_TZ)


def now_thailand_str() -> str:
    """Return formatted Thailand timestamp string (YYYY-MM-DD HH:MM:SS)."""
    return now_thailand().strftime("%Y-%m-%d %H:%M:%S")


def now_thailand_slug() -> str:
    """Return filename-safe Thailand timestamp slug (YYYYMMDD_HHMMSS)."""
    return now_thailand().strftime("%Y%m%d_%H%M%S")


def format_target_input(
    scan_mode: str,
    target_url: str = "",
    manual_urls_text: str = "",
    max_length: int = 1500,
) -> str:
    """Format the user-entered website/URL input cleanly for logging."""
    if scan_mode == "Manual List Mode":
        lines = [
            line.strip()
            for line in (manual_urls_text or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            return ""
        joined = " | ".join(lines)
        if len(joined) <= max_length:
            return joined
        kept: list[str] = []
        current_len = 0
        for line in lines:
            extra = len(line) + (3 if kept else 0)
            if current_len + extra > max_length - 30:
                break
            kept.append(line)
            current_len += extra
        remaining = len(lines) - len(kept)
        prefix = " | ".join(kept) if kept else lines[0][: max_length - 30]
        return f"{prefix} ... (+{remaining} more URLs)"
    return (target_url or "").strip()


def extract_fallback_hostname(raw_input: str) -> str:
    """Extract hostname from raw user input if crawl failed before hostname detection."""
    text = (raw_input or "").strip()
    if not text:
        return ""
    first = text.split(" | ")[0].strip()
    if "://" not in first:
        first = f"https://{first}"
    try:
        return (urlparse(first).hostname or "").lower()
    except Exception:
        return ""


@dataclass
class ScanLogEntry:
    """Represents a single scan history event with Thailand timestamp."""

    timestamp: str
    scan_mode: str
    target_input: str
    target_hostname: str
    pages_scanned: int = 0
    total_links_checked: int = 0
    broken_links: int = 0
    redirected_links: int = 0
    deep_check_issues: int = 0
    deep_check_enabled: bool = False
    status: str = "Completed"
    notes: str = ""

    def to_row(self) -> list[Any]:
        return [
            self.timestamp,
            self.scan_mode,
            self.target_input,
            self.target_hostname,
            int(self.pages_scanned),
            int(self.total_links_checked),
            int(self.broken_links),
            int(self.redirected_links),
            int(self.deep_check_issues),
            "Yes" if self.deep_check_enabled else "No",
            self.status,
            self.notes,
        ]

    def to_dict(self) -> dict[str, Any]:
        return dict(zip(HISTORY_COLUMNS, self.to_row()))

    def to_webhook_payload(self, token: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": self.timestamp,
            "scan_mode": self.scan_mode,
            "target_input": self.target_input,
            "target_hostname": self.target_hostname,
            "pages_scanned": int(self.pages_scanned),
            "total_links_checked": int(self.total_links_checked),
            "broken_links": int(self.broken_links),
            "redirected_links": int(self.redirected_links),
            "deep_check_issues": int(self.deep_check_issues),
            "deep_check_enabled": bool(self.deep_check_enabled),
            "status": self.status,
            "notes": self.notes,
        }
        if token:
            payload["token"] = token
        return payload


# ---------------------------------------------------------------------------
# Configuration Discovery (Secrets / Environment Variables)
# ---------------------------------------------------------------------------

def _get_streamlit_secrets() -> Mapping[str, Any]:
    """Safely fetch st.secrets without crashing if no secrets.toml exists."""
    try:
        import streamlit as st

        # Touching st.secrets when file is absent can raise FileNotFoundError
        secrets_obj = st.secrets
        _ = len(secrets_obj)
        return secrets_obj
    except Exception:
        return {}


def _read_setting(key: str, secrets: Mapping[str, Any] | None = None) -> str:
    env_val = os.environ.get(key, "").strip()
    if env_val:
        return env_val
    sec = _get_streamlit_secrets() if secrets is None else secrets
    try:
        val = sec.get(key, "")
        return str(val).strip() if val is not None else ""
    except Exception:
        return ""


def _read_service_account_info(secrets: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    env_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if env_json:
        try:
            parsed = json.loads(env_json)
            if isinstance(parsed, dict) and parsed.get("client_email"):
                return _normalize_service_account_dict(parsed)
        except Exception:
            pass

    sec = _get_streamlit_secrets() if secrets is None else secrets
    try:
        if "gcp_service_account" in sec:
            raw = dict(sec["gcp_service_account"])
            if raw.get("client_email") and raw.get("private_key"):
                return _normalize_service_account_dict(raw)
    except Exception:
        pass

    return None


def _normalize_service_account_dict(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    pk = normalized.get("private_key")
    if isinstance(pk, str) and "\\n" in pk:
        normalized["private_key"] = pk.replace("\\n", "\n")
    return normalized


def get_storage_backend_info(secrets: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Inspect environment / Streamlit Secrets and determine which backend is active.

    Returns a dict with keys:
      - backend: 'gsheets_service_account' | 'gsheets_webhook' | 'local_csv'
      - label: Human-readable Thai status label
      - is_cloud_persistent: bool
      - details: dict of active config metadata (without exposing raw secrets)
    """
    sa_info = _read_service_account_info(secrets)
    sheet_url = _read_setting("GSHEET_URL", secrets)
    sheet_id = _read_setting("GSHEET_ID", secrets)
    worksheet_name = _read_setting("GSHEET_WORKSHEET", secrets) or DEFAULT_WORKSHEET_NAME

    if sa_info and (sheet_url or sheet_id):
        return {
            "backend": "gsheets_service_account",
            "label": "Google Sheets (Service Account — ถาวร)",
            "is_cloud_persistent": True,
            "details": {
                "sheet_url": sheet_url,
                "sheet_id": sheet_id,
                "worksheet": worksheet_name,
                "client_email": sa_info.get("client_email", ""),
            },
        }

    webhook_url = _read_setting("GSHEET_WEBHOOK_URL", secrets)
    if webhook_url:
        return {
            "backend": "gsheets_webhook",
            "label": "Google Sheets (Apps Script — ถาวร)",
            "is_cloud_persistent": True,
            "details": {
                "webhook_configured": True,
                "has_token": bool(_read_setting("GSHEET_WEBHOOK_TOKEN", secrets)),
                "sheet_url": sheet_url,
            },
        }

    return {
        "backend": "local_csv",
        "label": "ไฟล์เครื่องชั่วคราว (scan_history.csv)",
        "is_cloud_persistent": False,
        "details": {
            "csv_path": str(LOCAL_HISTORY_CSV),
        },
    }


# ---------------------------------------------------------------------------
# Backend 1: Google Sheets via Service Account (gspread)
# ---------------------------------------------------------------------------

def _open_gspread_worksheet(secrets: Mapping[str, Any] | None = None):
    import gspread

    sa_info = _read_service_account_info(secrets)
    if not sa_info:
        raise ValueError("ไม่พบข้อมูล [gcp_service_account] ใน Secrets")

    sheet_url = _read_setting("GSHEET_URL", secrets)
    sheet_id = _read_setting("GSHEET_ID", secrets)
    worksheet_name = _read_setting("GSHEET_WORKSHEET", secrets) or DEFAULT_WORKSHEET_NAME

    client = gspread.service_account_from_dict(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

    if sheet_url:
        spreadsheet = client.open_by_url(sheet_url)
    elif sheet_id:
        spreadsheet = client.open_by_key(sheet_id)
    else:
        raise ValueError("กรุณาระบุ GSHEET_URL หรือ GSHEET_ID ใน Secrets")

    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(HISTORY_COLUMNS))

    # Ensure headers exist in row 1
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(HISTORY_COLUMNS, value_input_option="RAW")

    return ws


def _append_via_service_account(entry: ScanLogEntry, secrets: Mapping[str, Any] | None = None) -> None:
    ws = _open_gspread_worksheet(secrets)
    ws.append_row(entry.to_row(), value_input_option="RAW")


def _load_via_service_account(limit: int = 200, secrets: Mapping[str, Any] | None = None) -> pd.DataFrame:
    ws = _open_gspread_worksheet(secrets)
    values = ws.get_all_values()
    if len(values) <= 1:
        return pd.DataFrame(columns=HISTORY_COLUMNS)

    headers = values[0]
    data_rows = values[1:]
    # Reverse to show newest scans first
    data_rows = list(reversed(data_rows))[:limit]

    records: list[dict[str, Any]] = []
    for r in data_rows:
        padded = r + [""] * max(0, len(headers) - len(r))
        rec = {headers[i]: padded[i] for i in range(len(headers))}
        records.append(rec)

    return _normalize_history_df(pd.DataFrame(records))


# ---------------------------------------------------------------------------
# Backend 2: Google Sheets via Apps Script Webhook
# ---------------------------------------------------------------------------

def _normalize_webhook_url(url: str) -> str:
    cleaned = (url or "").strip()
    # Fix Chrome multi-account URLs like /macros/u/1/s/.../exec -> /macros/s/.../exec
    cleaned = re.sub(r"/macros/u/\d+/s/", "/macros/s/", cleaned)
    return cleaned


def _parse_apps_script_json(resp: requests.Response) -> dict[str, Any]:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    text_preview = (resp.text or "").strip()

    if resp.status_code == 403:
        raise RuntimeError(
            "HTTP 403 (Forbidden) จาก Google Apps Script — กรุณาตรวจสอบ 2 จุด: "
            "1) ตอน Deploy Web app ต้องตั้ง 'Who has access' เป็น 'Anyone (ทุกคน)' (ไม่ใช่ Only myself หรือ Anyone with Google account) "
            "2) URL ใน Secrets ต้องเป็น Web app URL ที่ลงท้ายด้วย /exec (ไม่ใช่ /dev หรือลิงก์หน้าแก้ไข)"
        )

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} จาก Google Apps Script")

    if "text/html" in content_type or text_preview.startswith("<!DOCTYPE") or text_preview.startswith("<html"):
        raise RuntimeError(
            "Google Apps Script ส่งหน้า HTML/Login กลับมา — กรุณาตรวจสอบว่า Deploy Web app "
            "โดยตั้งค่า Who has access เป็น 'Anyone' และใช้ URL ที่ลงท้ายด้วย /exec"
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"ไม่สามารถอ่าน JSON จาก Apps Script ได้: {exc}") from exc

    if not isinstance(data, dict) or not data.get("ok"):
        err_msg = data.get("error", "Unknown error") if isinstance(data, dict) else str(data)
        raise RuntimeError(f"Apps Script แจ้งข้อผิดพลาด: {err_msg}")

    return data


def _append_via_webhook(entry: ScanLogEntry, secrets: Mapping[str, Any] | None = None) -> None:
    webhook_url = _normalize_webhook_url(_read_setting("GSHEET_WEBHOOK_URL", secrets))
    token = _read_setting("GSHEET_WEBHOOK_TOKEN", secrets)
    payload = entry.to_webhook_payload(token=token)

    resp = requests.post(
        webhook_url,
        json=payload,
        headers={"Accept": "application/json"},
        timeout=15,
        allow_redirects=True,
    )
    _parse_apps_script_json(resp)


def _load_via_webhook(limit: int = 200, secrets: Mapping[str, Any] | None = None) -> pd.DataFrame:
    webhook_url = _normalize_webhook_url(_read_setting("GSHEET_WEBHOOK_URL", secrets))
    token = _read_setting("GSHEET_WEBHOOK_TOKEN", secrets)
    params: dict[str, Any] = {"limit": limit}
    if token:
        params["token"] = token

    resp = requests.get(
        webhook_url,
        params=params,
        headers={"Accept": "application/json"},
        timeout=15,
        allow_redirects=True,
    )
    data = _parse_apps_script_json(resp)
    rows = data.get("rows") or []
    if not rows:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    return _normalize_history_df(pd.DataFrame(rows))


# ---------------------------------------------------------------------------
# Backend 3: Local CSV Fallback
# ---------------------------------------------------------------------------

def _append_via_local_csv(entry: ScanLogEntry, csv_path: Path = LOCAL_HISTORY_CSV) -> None:
    with _CSV_LOCK:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(HISTORY_COLUMNS)
            writer.writerow(entry.to_row())


def _load_via_local_csv(limit: int = 200, csv_path: Path = LOCAL_HISTORY_CSV) -> pd.DataFrame:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str).fillna("")
    except Exception:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    if df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    # Reverse to newest first
    df = df.iloc[::-1].head(limit).reset_index(drop=True)
    return _normalize_history_df(df)


# ---------------------------------------------------------------------------
# Normalization & Public API
# ---------------------------------------------------------------------------

_INT_COLUMNS = (
    "Pages Scanned",
    "Total Links Checked",
    "Broken Links",
    "Redirected Links",
    "Deep Check Issues",
)


def _normalize_history_df(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure consistent column ordering and clean types for UI rendering."""
    out = df.copy()
    for col in HISTORY_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out = out[HISTORY_COLUMNS].copy()
    for col in _INT_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)
    for col in HISTORY_COLUMNS:
        if col not in _INT_COLUMNS:
            out[col] = out[col].fillna("").astype(str)
    return out.reset_index(drop=True)


def record_scan_history(
    entry: ScanLogEntry,
    secrets: Mapping[str, Any] | None = None,
    csv_path: Path = LOCAL_HISTORY_CSV,
) -> tuple[bool, str, str]:
    """
    Record a scan history entry to the configured backend.

    Returns:
        (success, backend_used, user_message)
    """
    info = get_storage_backend_info(secrets)
    backend = info["backend"]

    if backend == "gsheets_service_account":
        try:
            _append_via_service_account(entry, secrets)
            return True, backend, f"บันทึกประวัติลง Google Sheets แล้ว ({entry.timestamp} น.)"
        except Exception as exc:
            try:
                _append_via_local_csv(entry, csv_path)
            except Exception:
                pass
            return (
                False,
                "local_csv_fallback",
                f"ไม่สามารถบันทึกลง Google Sheets (Service Account) ได้: {exc} — สำรองไว้ในไฟล์เครื่องชั่วคราวแล้ว",
            )

    if backend == "gsheets_webhook":
        try:
            _append_via_webhook(entry, secrets)
            return True, backend, f"บันทึกประวัติลง Google Sheets แล้ว ({entry.timestamp} น.)"
        except Exception as exc:
            try:
                _append_via_local_csv(entry, csv_path)
            except Exception:
                pass
            return (
                False,
                "local_csv_fallback",
                f"ไม่สามารถบันทึกลง Google Sheets (Apps Script) ได้: {exc} — สำรองไว้ในไฟล์เครื่องชั่วคราวแล้ว",
            )

    try:
        _append_via_local_csv(entry, csv_path)
        return (
            True,
            "local_csv",
            f"บันทึกประวัติลงไฟล์เครื่องแล้ว ({entry.timestamp} น.) — ตั้งค่า Google Sheets ใน Secrets เพื่อเก็บถาวรบน Cloud",
        )
    except Exception as exc:
        return False, "error", f"ไม่สามารถบันทึกประวัติได้: {exc}"


def load_scan_history(
    limit: int = 200,
    secrets: Mapping[str, Any] | None = None,
    csv_path: Path = LOCAL_HISTORY_CSV,
) -> tuple[pd.DataFrame, dict[str, Any], str | None]:
    """
    Load recent scan history rows (newest first).

    Returns:
        (dataframe, backend_info, error_message_or_none)
    """
    info = get_storage_backend_info(secrets)
    backend = info["backend"]

    if backend == "gsheets_service_account":
        try:
            df = _load_via_service_account(limit=limit, secrets=secrets)
            return df, info, None
        except Exception as exc:
            fallback_df = _load_via_local_csv(limit=limit, csv_path=csv_path)
            return fallback_df, info, f"โหลดข้อมูลจาก Google Sheets ไม่สำเร็จ: {exc}"

    if backend == "gsheets_webhook":
        try:
            df = _load_via_webhook(limit=limit, secrets=secrets)
            return df, info, None
        except Exception as exc:
            fallback_df = _load_via_local_csv(limit=limit, csv_path=csv_path)
            return fallback_df, info, f"โหลดข้อมูลจาก Google Sheets (Apps Script) ไม่สำเร็จ: {exc}"

    return _load_via_local_csv(limit=limit, csv_path=csv_path), info, None


def test_gsheets_connection(secrets: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """Verify Google Sheets connectivity for the currently configured Secrets."""
    info = get_storage_backend_info(secrets)
    backend = info["backend"]

    if backend == "gsheets_service_account":
        try:
            ws = _open_gspread_worksheet(secrets)
            return True, f"เชื่อมต่อ Google Sheets สำเร็จ! (Worksheet: '{ws.title}')"
        except Exception as exc:
            return False, f"เชื่อมต่อ Google Sheets (Service Account) ไม่สำเร็จ: {exc}"

    if backend == "gsheets_webhook":
        try:
            _load_via_webhook(limit=1, secrets=secrets)
            return True, "เชื่อมต่อ Google Sheets ผ่าน Apps Script สำเร็จ!"
        except Exception as exc:
            return False, f"เชื่อมต่อ Google Sheets (Apps Script) ไม่สำเร็จ: {exc}"

    return False, "ยังไม่ได้ตั้งค่า Google Sheets ใน Secrets (กำลังใช้งานโหมดไฟล์เครื่องชั่วคราว scan_history.csv)"
