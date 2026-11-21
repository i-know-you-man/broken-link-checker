/**
 * Google Apps Script สำหรับบันทึกและอ่านประวัติการสแกน (Scan History + Timestamp)
 * จากแอป Broken Link, Redirect & Soft 404 Checker (Streamlit)
 *
 * วิธีใช้งาน:
 * 1. เปิด Google Sheets ที่ต้องการเก็บประวัติ
 * 2. ไปที่เมนู ส่วนขยาย (Extensions) -> Apps Script
 * 3. ลบโค้ดเดิมออกทั้งหมด แล้ววางโค้ดไฟล์นี้ลงไป -> กด Save (Ctrl+S)
 * 4. กดปุ่ม Deploy (ทำให้ใช้งานได้) -> New deployment (การทำให้ใช้งานได้รายการใหม่)
 * 5. เลือกประเภท: Web app (เว็บแอป)
 *    - Execute as: Me (ฉัน)
 *    - Who has access: Anyone (ทุกคน)
 * 6. กด Deploy -> อนุญาตสิทธิ์ (Authorize access) -> คัดลอก Web app URL (ที่ลงท้ายด้วย /exec)
 * 7. นำ Web app URL ไปวางใน Streamlit Cloud -> Settings -> Secrets:
 *      GSHEET_WEBHOOK_URL = "https://script.google.com/macros/s/xxxxx/exec"
 */

// (ไม่บังคับ) หากต้องการความปลอดภัยเพิ่ม สามารถตั้งรหัสลับตรงนี้ให้ตรงกับ
// GSHEET_WEBHOOK_TOKEN ใน Streamlit Secrets ได้ (ถ้าไม่ใช้ ให้ปล่อยเป็น "" ไว้)
const SECRET_TOKEN = "";

// ชื่อแท็บชีตที่จะสร้างให้อัตโนมัติใน Google Sheets
const SHEET_NAME = "Scan_History";

const HEADERS = [
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
  "Notes"
];

function getOrCreateSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    const firstSheet = ss.getSheets()[0];
    if (firstSheet && firstSheet.getLastRow() === 0) {
      firstSheet.setName(SHEET_NAME);
      sheet = firstSheet;
    } else {
      sheet = ss.insertSheet(SHEET_NAME);
    }
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
    const headerRange = sheet.getRange(1, 1, 1, HEADERS.length);
    headerRange.setFontWeight("bold");
    headerRange.setBackground("#1E293B");
    headerRange.setFontColor("#FFFFFF");
    sheet.setColumnWidth(1, 175); // Timestamp (TH)
    sheet.setColumnWidth(2, 150); // Scan Mode
    sheet.setColumnWidth(3, 340); // Target Input
    sheet.setColumnWidth(4, 200); // Target Hostname
    sheet.setColumnWidth(12, 260); // Notes
  }
  return sheet;
}

function checkToken_(providedToken) {
  if (!SECRET_TOKEN) {
    return true;
  }
  return String(providedToken || "") === String(SECRET_TOKEN);
}

function jsonResponse_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(10000);
    const raw = (e && e.postData && e.postData.contents) ? e.postData.contents : "{}";
    const data = JSON.parse(raw);

    if (!checkToken_(data.token)) {
      return jsonResponse_({ ok: false, error: "Invalid token" });
    }

    const sheet = getOrCreateSheet_();
    const row = [
      String(data.timestamp || ""),
      String(data.scan_mode || ""),
      String(data.target_input || ""),
      String(data.target_hostname || ""),
      Number(data.pages_scanned || 0),
      Number(data.total_links_checked || 0),
      Number(data.broken_links || 0),
      Number(data.redirected_links || 0),
      Number(data.deep_check_issues || 0),
      data.deep_check_enabled ? "Yes" : "No",
      String(data.status || "Completed"),
      String(data.notes || "")
    ];

    const nextRow = sheet.getLastRow() + 1;
    // บังคับคอลัมน์เวลาเป็น Plain Text เพื่อไม่ให้ Timezone ของชีตเลื่อนเวลาประเทศไทย (UTC+7)
    sheet.getRange(nextRow, 1).setNumberFormat("@");
    sheet.getRange(nextRow, 1, 1, row.length).setValues([row]);

    return jsonResponse_({ ok: true, row_number: nextRow });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err) });
  } finally {
    try {
      lock.releaseLock();
    } catch (_) {}
  }
}

function doGet(e) {
  try {
    const params = (e && e.parameter) ? e.parameter : {};
    if (!checkToken_(params.token)) {
      return jsonResponse_({ ok: false, error: "Invalid token" });
    }

    const sheet = getOrCreateSheet_();
    const values = sheet.getDataRange().getValues();
    if (values.length <= 1) {
      return jsonResponse_({ ok: true, rows: [] });
    }

    const headers = values[0];
    const limit = Math.min(Math.max(Number(params.limit || 200), 1), 1000);
    const dataRows = values.slice(1).slice(-limit).reverse();

    const rows = dataRows.map(function (r) {
      const obj = {};
      for (let i = 0; i < headers.length; i++) {
        let val = r[i];
        if (val instanceof Date) {
          val = Utilities.formatDate(val, "Asia/Bangkok", "yyyy-MM-dd HH:mm:ss");
        }
        obj[headers[i]] = val;
      }
      return obj;
    });

    return jsonResponse_({ ok: true, rows: rows });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err) });
  }
}
