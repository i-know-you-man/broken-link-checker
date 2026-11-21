# 🔍 Broken Link, Redirect & Soft 404 Checker

ระบบตรวจสอบลิงก์เสีย (Broken Links), ลิงก์ Redirect, หน้าขาว (Blank Page) และ Soft 404 สำหรับเว็บไซต์ใดก็ได้ พร้อม Basic Web UI ด้วย **Python, Streamlit และ Playwright** (ไม่ต้องเชื่อมต่อ Google Search Console)

---

## ✨ ความสามารถหลัก

### 1. โหมดการดึงหน้าเว็บ (3 Scan Modes)
- **Auto-Crawl Mode:** กรอก URL หน้าแรก แล้วไต่ตามลิงก์ภายในอัตโนมัติตาม `Max Depth` และ `Max Pages`
- **Sitemap Mode:** กรอก URL ของ `sitemap.xml` (หรือกรอกแค่โดเมนหลักเพื่อให้ระบบค้นหา `/sitemap.xml`, `/sitemap_index.xml`, และ `robots.txt` อัตโนมัติ) รองรับทั้ง `<urlset>`, `<sitemapindex>`, และ `.xml.gz`
- **Manual List Mode:** วางรายการ URL เฉพาะหน้าที่ต้องการตรวจทีละบรรทัด

### 2. การแยก Subdomain, ข้าม Social Media และป้องกันการวนลูป (Scope & Safety)
- **Strict Hostname Scope:** ไต่เฉพาะหน้าเว็บที่ตรงกับ Hostname ที่กรอกเท่านั้น เช่น กรอก `https://siamatsiam.com` จะไม่ไต่เข้าไปใน `shop.siamatsiam.com` หรือ `booking.siamatsiam.com` แต่แยกเป็น **External Link** เพื่อเช็คสถานะปลายทางอย่างเดียว
- **Ignore Social Media:** ข้ามแพลตฟอร์ม Social Media อัตโนมัติ (เช่น `facebook.com`, `instagram.com`, `tiktok.com`, `x.com`, `line.me`, `linkedin.com`, `youtube.com` ฯลฯ และสามารถปรับแก้รายชื่อโดเมนใน UI ได้) เพื่อลด False Positive จากการบล็อกบอท เช่น Facebook 400 Bad Request
- ข้ามไฟล์สื่อและลิงก์ที่ไม่ใช่หน้าเว็บอัตโนมัติ (`.jpg`, `.png`, `.gif`, `.pdf`, `.zip`, `mailto:`, `tel:`, `javascript:` ฯลฯ)
- ตัด Tracking Query Parameters (`utm_*`, `fbclid`, `gclid`, `gbraid`, `wbraid`, `msclkid`, `_ga`, `_gl`) ออกก่อนเทียบ URL ซ้ำเพื่อป้องกันการสแกนวนลูป

### 3. ระบบตรวจลิงก์ 2 ระดับ & แยกสถานะชัดเจน
1. **Standard Check (Fast HTTP Mode):**
   - ดึงลิงก์ `<a href="...">` ทั้งหมดพร้อม `Anchor Text`
   - ใช้ `concurrent.futures.ThreadPoolExecutor` เช็คสถานะ HTTP พร้อม Timeout 10 วินาที
   - **แยกหมวดสถานะชัดเจนพร้อมตัวกรองใน UI:** `404 Not Found`, `410 Gone`, `5xx Server Error`, `Timeout`, `Connection / SSL Error`, `Blocked / WAF (401/403/429)`, `Other 4xx Client Error` และแยก Redirect (`301 Permanent`, `302 Temporary`, `307/308`, `Multi-Hop Chain`)
   - **404 Highlight Screencap (เฉพาะ 404 เท่านั้น):** เปิดหน้าต้นทางด้วย Playwright แล้ววง/Highlight ปุ่มหรือลิงก์ที่มีปัญหาให้เห็นชัดเจน พร้อมแนบภาพหลักฐานในรายงาน
2. **Deep Check Mode (Playwright Headless Browser):**
   - เลือกรันเฉพาะปุ่มสำคัญตาม Keyword (ค่าเริ่มต้น: `book, booking, reserve, shop`) หรือรันทุก URL ในโหมด Manual List
   - เลือก **Device Profile:** `Mobile (iPhone 14)` หรือ `Desktop`
   - รอ JavaScript โหลดจนเสร็จ (`networkidle` + รอเผื่อ Loading Spinner อีก 2.5 วินาที)
   - **แยกสถานะชัดเจน:** `Soft 404`, `HTTP 404`, `Blank Page`, `HTTP 410`, `HTTP 5xx Error`, `HTTP Blocked / 4xx`, `Load / Network Error` พร้อมตัวกรองแยกตามสถานะและ Ad Simulation
   - **ตรวจจับหน้าขาว (Blank Page):** เช็ค `document.body.innerText.trim()` ว่าว่างเปล่าหรือมีน้อยกว่า 50 ตัวอักษร
   - **ตรวจจับ Soft 404:** สแกนข้อความที่เรนเดอร์จริงบนหน้าจอตามคำคีย์เวิร์ดที่แก้ไข/เพิ่มเองได้ในหน้า UI
   - **Ad Parameter Simulation:** แนบพารามิเตอร์ทดสอบ (เช่น `?gclid=test_gclid_123&utm_source=google&utm_medium=cpc`) เข้าหน้าต้นทาง แล้วจำลองการคลิกปุ่มปลายทางจริงด้วย Playwright เพื่อตรวจจับเคสที่ระบบจอง/หน้าปลายทางพังหรือค้างขาวเมื่อมาจากโฆษณา
   - **Screenshot Capture (Highlight เฉพาะ 404):** สำหรับ `Soft 404` และ `HTTP 404` ระบบจะแคปภาพทั้งหน้าต้นทาง (วงปุ่มที่มีปัญหา) และหน้าปลายทาง (วงข้อความ Error) ส่วน `Blank Page` จะแคปภาพหน้าจอปกติเก็บไว้ในโฟลเดอร์ `screenshots/`

### 4. รายงาน, ประวัติการสแกน (Timestamp) & Export
- KPI Summary Cards แสดงยอดสรุปครบทุกหมวด
- แยกแท็บรายงาน 4 แท็บ (`Broken Links`, `Redirected Links`, `Deep Check Issues`, `Scan History`) พร้อมตัวกรองค้นหาและภาพ Screenshot
- **ระบบบันทึก Timestamp & ประวัติเว็บไซต์ที่ตรวจ (เวลาประเทศไทย UTC+7):** บันทึกอัตโนมัติทุกครั้งที่กด Start Scan รองรับทั้ง **Google Sheets** (เก็บถาวรบน Cloud) และไฟล์สำรอง `scan_history.csv` พร้อมปุ่มทดสอบการเชื่อมต่อและดาวน์โหลดประวัติ
- ปุ่มดาวน์โหลด **Excel (.xlsx)** แยก 3 ชีต (`Broken_Links`, `Redirected_Links`, `Deep_Check_Issues`) และไฟล์ **CSV** (รองรับภาษาไทยใน Excel ด้วย UTF-8-SIG)

---

## 📁 โครงสร้างไฟล์

```text
broken-link-checker/
├── app.py                   # หน้าเว็บ Streamlit UI, Dashboard, Scan History และระบบ Export Excel/CSV
├── crawler.py               # ระบบ Crawl 3 โหมด, Strict Hostname, URL Dedup และ Fast HTTP Checker
├── deep_checker.py          # ระบบ Playwright Deep Check, 404 Highlight Screencap และ auto-install Chromium
├── history_logger.py        # ระบบบันทึก Timestamp (เวลาไทย UTC+7) ลง Google Sheets / Local CSV
├── google_apps_script.gs    # โค้ด Google Apps Script พร้อมคัดลอกไปวางใน Google Sheets
├── requirements.txt         # รายการ Python packages ที่ต้องติดตั้ง
├── packages.txt             # ไลบรารี Debian Linux + ฟอนต์ภาษาไทยสำหรับรัน Chromium บน Streamlit Cloud
├── .streamlit/
│   ├── config.toml          # การตั้งค่าเซิร์ฟเวอร์ Streamlit
│   └── secrets.toml.example # ตัวอย่างการตั้งค่า Secrets สำหรับ Google Sheets และ APP_PASSWORD
├── .gitignore               # กันโฟลเดอร์ screenshots/, scan_history.csv, __pycache__/ และ secrets.toml ออกจาก Git
├── screenshots/             # โฟลเดอร์เก็บภาพ Screenshot อัตโนมัติเมื่อพบปัญหา
└── README.md                # คู่มือการติดตั้งและใช้งาน
```

---

## 🛠️ วิธีติดตั้ง (Installation)

ต้องมี **Python 3.10+** ติดตั้งอยู่ในเครื่อง จากนั้นเปิด Terminal / PowerShell ในโฟลเดอร์โปรเจกต์แล้วรันคำสั่งตามลำดับ:

### 1. (แนะนำ) สร้างและเปิดใช้ Virtual Environment
**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. ติดตั้ง Python Libraries
```bash
pip install -r requirements.txt
```

### 3. ติดตั้งเบราว์เซอร์ Chromium สำหรับ Playwright (จำเป็นสำหรับ Deep Check)
```bash
playwright install chromium
```

---

## 🚀 วิธีรันใช้งาน (Run Application)

```bash
streamlit run app.py
```

เมื่อรันเสร็จ เบราว์เซอร์จะเปิดหน้าเว็บอัตโนมัติที่ `http://localhost:8501`

---

## 💡 ตัวอย่างการใช้งาน Ad Parameter Simulation (ตรวจปุ่มจองจากโฆษณา)

1. เลือกโหมด **Auto-Crawl Mode** หรือ **Manual List Mode** แล้วกรอกหน้า Landing Page ของเว็บไซต์ (เช่น `https://siamatsiam.com`)
2. ติ๊ก **ตรวจสอบ External Links ด้วย** (เพื่อให้ครอบคลุมปุ่มที่ลิงก์ไป Subdomain ระบบจอง เช่น `booking.siamatsiam.com`)
3. ติ๊ก **เปิดใช้งาน Deep Check (ตรวจหน้าขาว / Soft 404)**
4. เลือก Device เป็น **Mobile (iPhone 14)**
5. กำหนด Button Keyword Filter เช่น `book, booking, reserve, shop`
6. ติ๊ก **จำลองพารามิเตอร์โฆษณา (`?gclid=...&utm_...`)**
7. กด **Start Scan** — ระบบจะเปิดหน้าต้นทางพร้อมแนบ `?gclid=test_gclid_123&utm_source=google&utm_medium=cpc` แล้วจำลองการคลิกปุ่มจองจริงด้วย Playwright หากปลายทางค้างขาวหรือขึ้น Error จะบันทึกภาพ Screenshot ไว้ในแท็บ **Deep Check Issues** ทันที

---

## ☁️ วิธีนำขึ้นออนไลน์ (GitHub + Streamlit Community Cloud)

โปรเจกต์นี้เตรียมไฟล์สำหรับ Deploy บน **Streamlit Community Cloud (ฟรี)** ไว้เรียบร้อยแล้ว (`packages.txt` สำหรับระบบ Linux + ฟอนต์ไทย, `.gitignore`, `.streamlit/config.toml` และโค้ดติดตั้ง Chromium อัตโนมัติเมื่อรันครั้งแรกบน Cloud)

1. **สร้าง Repository ใหม่บน GitHub** (ที่ `https://github.com/new` — เช่น `broken-link-checker`)
2. **Push โค้ดขึ้น GitHub** จากโฟลเดอร์โปรเจกต์:
   ```powershell
   git add .
   git commit -m "Initial commit: Broken Link, Redirect & Soft 404 Checker"
   git remote add origin https://github.com/<USERNAME>/<REPO_NAME>.git
   git push -u origin main
   ```
3. **Deploy บน Streamlit Community Cloud:**
   - เข้าไปที่ **`https://share.streamlit.io`** แล้วล็อกอินด้วย GitHub
   - กด **Create app** -> เลือก Repository และ Branch `main` -> ตั้ง **Main file path** เป็น `app.py`
   - *(แนะนำ)* กด **Advanced settings -> Secrets** แล้วเพิ่มรหัสผ่านกันคนนอกเข้าใช้:
     ```toml
     APP_PASSWORD = "ตั้งรหัสผ่านของคุณที่นี่"
     ```
   - กด **Deploy!** เมื่อสร้างเสร็จจะได้ลิงก์ `https://<ชื่อแอปของคุณ>.streamlit.app` สำหรับเปิดใช้งานผ่านเน็ตทันที

---

## 📊 วิธีเชื่อมต่อ Google Sheets เพื่อเก็บ Timestamp & ประวัติการสแกนถาวร

### วิธีที่ 1 (แนะนำ — ง่ายสุด ใช้เวลา 3 นาที ไม่ต้องเปิด Google Cloud)
1. สร้างไฟล์ **Google Sheets** ใหม่ใน Google Drive ของคุณ (เช่นตั้งชื่อว่า `Broken Link Checker History`)
2. ไปที่เมนู **ส่วนขยาย (Extensions) -> Apps Script**
3. ลบโค้ดเดิมออกทั้งหมด แล้วคัดลอกโค้ดทั้งหมดจากไฟล์ `google_apps_script.gs` ไปวาง -> กด **Save** (`Ctrl+S`)
4. กดปุ่ม **Deploy (ทำให้ใช้งานได้) -> New deployment (การทำให้ใช้งานได้รายการใหม่)**
   - คลิกไอคอนฟันเฟือง เลือกประเภท **Web app (เว็บแอป)**
   - **Execute as (ดำเนินการในฐานะ):** `Me (ฉัน)`
   - **Who has access (ผู้ที่มีสิทธิ์เข้าถึง):** `Anyone (ทุกคน)`
   - กด **Deploy** -> กดอนุญาตสิทธิ์ (**Authorize access**) -> คัดลอก **Web app URL** (ที่ลงท้ายด้วย `/exec`)
5. เปิดหน้าแอปของคุณบน **Streamlit Cloud -> ⋮ (มุมขวาล่าง/ขวาบน) -> Settings -> Secrets** แล้ววาง:
   ```toml
   GSHEET_WEBHOOK_URL = "https://script.google.com/macros/s/xxxxx/exec"
   GSHEET_URL = "https://docs.google.com/spreadsheets/d/xxxxx/edit"
   ```
   *(ดูตัวอย่างเพิ่มเติมรวมถึงการตั้งรหัสผ่านและวิธี Service Account ได้ใน `.streamlit/secrets.toml.example`)*
6. กด **Save** แล้วเปิดแอป กดปุ่ม **🔌 ทดสอบการเชื่อมต่อ Google Sheets** ในส่วนประวัติการสแกน

### วิธีที่ 2 (ทางเลือก — Google Cloud Service Account)
หากต้องการใช้ Google Service Account (`gspread`) ให้เปิดใช้งาน **Google Sheets API** ใน Google Cloud Console สร้าง Service Account Key (JSON), แชร์ Google Sheet ให้กับ `client_email` เป็นสิทธิ์ **Editor**, แล้ววาง `[gcp_service_account]` และ `GSHEET_URL` ใน Streamlit Secrets ตามตัวอย่างใน `.streamlit/secrets.toml.example`

---

## 🔄 วิธีอัปเดตโค้ดขึ้น GitHub (และอัปเดตเว็บออนไลน์อัตโนมัติ)

ทุกครั้งที่มีการแก้ไขหรือเพิ่มฟีเจอร์ใหม่ ให้เปิด **Command Prompt / PowerShell** แล้วรันคำสั่งต่อไปนี้:

```powershell
cd C:\Users\airo_\.gemini\antigravity\scratch\broken-link-checker
git status
git add .
git commit -m "Add scan history timestamp and Google Sheets sync"
git push origin main
```

เมื่อ `git push` สำเร็จ **Streamlit Cloud จะดึงโค้ดใหม่ไปอัปเดตหน้าเว็บออนไลน์ให้อัตโนมัติ** (หากมีการเพิ่มไลบรารีใหม่ใน `requirements.txt` จะใช้เวลาติดตั้งประมาณ 1–2 นาที)


