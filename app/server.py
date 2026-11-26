import base64
import calendar
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import smtplib
import sqlite3
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from notifications import NotificationChannel, SlackBotChannel

PORT = int(os.environ.get("PORT", "8080"))
APP_ORIGIN = os.environ.get("APP_ORIGIN", "").rstrip("/")
CLIENT_ID = os.environ.get("LINEAR_CLIENT_ID", "")
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(48)).encode()
ADMIN_EMAILS = {value.strip().lower() for value in os.environ.get("ADMIN_EMAILS", "").split(",") if value.strip()}
PRIMARY_APPROVER = os.environ.get("PRIMARY_APPROVER_EMAIL", "").lower()
SECONDARY_APPROVER = os.environ.get("SECONDARY_APPROVER_EMAIL", "").lower()
PRIMARY_APPROVER_NAME = os.environ.get("PRIMARY_APPROVER_NAME", "初审人")
SECONDARY_APPROVER_NAME = os.environ.get("SECONDARY_APPROVER_NAME", "终审人")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
APPROVAL_NOTIFICATION_CHANNELS: tuple[NotificationChannel, ...] = (
    (SlackBotChannel(SLACK_BOT_TOKEN),) if SLACK_BOT_TOKEN else ()
)
INDEX_HTML = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "qingjia.db"
ATTACHMENT_DIR = DATA_DIR / "attachments"
ATTACHMENT_DIR.mkdir(exist_ok=True)
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".xls", ".xlsx"}
LEAVE_TYPES = ("年假", "调休", "病假", "事假", "婚假", "产假", "陪产假", "丧假", "育儿假", "路程假", "司龄假", "Remote")


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          linear_id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'employee', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS leave_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, leave_type TEXT NOT NULL,
          start_date TEXT NOT NULL, end_date TEXT NOT NULL, days REAL NOT NULL,
          handover TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(user_id) REFERENCES users(linear_id)
        );
        CREATE TABLE IF NOT EXISTS email_outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL, subject TEXT NOT NULL,
          body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sent_at TEXT
        );
        """)
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        for column, declaration in {
            "company": "TEXT NOT NULL DEFAULT ''",
            "work_start_date": "TEXT",
            "hire_date": "TEXT",
            "annual_leave_used": "REAL",
            "notes": "TEXT NOT NULL DEFAULT ''",
            "profile_locked": "INTEGER NOT NULL DEFAULT 0",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
        leave_columns = {row[1] for row in connection.execute("PRAGMA table_info(leave_requests)")}
        for column, declaration in {
            "start_period": "TEXT NOT NULL DEFAULT '上午'",
            "end_period": "TEXT NOT NULL DEFAULT '下午'",
            "primary_decision": "TEXT",
            "primary_decided_at": "TEXT",
            "secondary_decision": "TEXT",
            "secondary_decided_at": "TEXT",
            "secondary_decided_by": "TEXT",
            "attachment_name": "TEXT",
            "attachment_path": "TEXT",
            "attachment_type": "TEXT",
            "attachment_size": "INTEGER",
        }.items():
            if column not in leave_columns:
                connection.execute(f"ALTER TABLE leave_requests ADD COLUMN {column} {declaration}")
        connection.execute("UPDATE leave_requests SET status='pending_primary' WHERE status='pending'")
        connection.execute("UPDATE users SET role='employee' WHERE role='admin'")
        for email in ADMIN_EMAILS:
            existing_admin = connection.execute("SELECT linear_id FROM users WHERE lower(email)=?", (email,)).fetchone()
            if existing_admin:
                connection.execute("UPDATE users SET role='admin',is_active=1 WHERE linear_id=?", (existing_admin["linear_id"],))
            else:
                connection.execute("INSERT INTO users(linear_id,name,email,role) VALUES(?,?,?,'admin')", (f"pending:{email}", email.split('@')[0].title(), email))


def yearfrac_30_360(start, end):
    d1 = 30 if start.day == 31 else start.day
    d2 = 30 if end.day == 31 and d1 >= 30 else end.day
    return ((end.year - start.year) * 360 + (end.month - start.month) * 30 + d2 - d1) / 360


def leave_metrics(user):
    try:
        work_start = date.fromisoformat(user.get("work_start_date") or "")
        hire = date.fromisoformat(user.get("hire_date") or "")
        service_years = yearfrac_30_360(work_start, date.today())
        standard = 0 if service_years < 1 else 5 if service_years < 10 else 10 if service_years < 20 else 15
        raw_accrued = max(0, (date.today() - hire).days / 365 * standard)
        three_year_earned = min(raw_accrued, standard * 3)
        used = float(user.get("annual_leave_used") or 0)
        return {"service_years": round(service_years, 4), "statutory_standard": standard, "statutory_accrued": round(three_year_earned, 4), "three_year_earned": round(three_year_earned, 4), "annual_leave_available": round(max(0, three_year_earned - used), 4)}
    except Exception:
        return {"service_years": None, "statutory_standard": None, "statutory_accrued": None, "three_year_earned": None, "annual_leave_available": None}


def attendance_overlap_days(request, month_start, month_end):
    """Return the approved leave slots that fall inside the selected month."""
    request_start = date.fromisoformat(request["start_date"])
    request_end = date.fromisoformat(request["end_date"])
    start_slot = request_start.toordinal() * 2 + (1 if request.get("start_period") == "下午" else 0)
    end_slot = request_end.toordinal() * 2 + (0 if request.get("end_period") == "上午" else 1)
    month_start_slot = month_start.toordinal() * 2
    month_end_slot = month_end.toordinal() * 2 + 1
    overlap = max(0, min(end_slot, month_end_slot) - max(start_slot, month_start_slot) + 1)
    return overlap / 2


def remote_days_by_week(start_date, start_period, end_date, end_period):
    """Split a half-day based Remote request into Monday-to-Sunday buckets."""
    first_slot = start_date.toordinal() * 2 + (0 if start_period == "上午" else 1)
    last_slot = end_date.toordinal() * 2 + (0 if end_period == "上午" else 1)
    totals = {}
    for slot in range(first_slot, last_slot + 1):
        current_date = date.fromordinal(slot // 2)
        week_start = current_date - timedelta(days=current_date.weekday())
        key = week_start.isoformat()
        totals[key] = totals.get(key, 0.0) + 0.5
    return totals


def remote_quota_error(connection, user_id, start_date, start_period, end_date, end_period, exclude_request_id=None):
    candidate = remote_days_by_week(start_date, start_period, end_date, end_period)
    parameters = [user_id]
    sql = "SELECT id,start_date,end_date,start_period,end_period FROM leave_requests WHERE user_id=? AND lower(leave_type)='remote' AND status IN ('pending_primary','pending_secondary','approved')"
    if exclude_request_id is not None:
        sql += " AND id<>?"
        parameters.append(exclude_request_id)
    used = {}
    for existing in connection.execute(sql, parameters).fetchall():
        weekly = remote_days_by_week(
            date.fromisoformat(existing["start_date"]), existing["start_period"],
            date.fromisoformat(existing["end_date"]), existing["end_period"],
        )
        for week_start, days in weekly.items():
            used[week_start] = used.get(week_start, 0.0) + days
    for week_start, requested_days in candidate.items():
        used_days = used.get(week_start, 0.0)
        if used_days + requested_days > 1.0 + 1e-9:
            return {"error": "remote_weekly_quota_exceeded", "week_start": week_start, "used": round(used_days, 2), "remaining": round(max(0, 1.0 - used_days), 2)}
    return None


def hire_anniversary(hire_date, year):
    day = min(hire_date.day, calendar.monthrange(year, hire_date.month)[1])
    return date(year, hire_date.month, day)


def seniority_period(hire_date, on_date):
    if on_date < hire_date:
        return {"years": 0, "quota": 0, "start": hire_date, "end": hire_anniversary(hire_date, hire_date.year + 1) - timedelta(days=1)}
    years = on_date.year - hire_date.year
    if on_date < hire_anniversary(hire_date, on_date.year):
        years -= 1
    years = max(0, years)
    period_start = hire_date if years == 0 else hire_anniversary(hire_date, hire_date.year + years)
    period_end = hire_anniversary(hire_date, hire_date.year + years + 1) - timedelta(days=1)
    return {"years": years, "quota": min(years, 5), "start": period_start, "end": period_end}


def seniority_leave_usage(connection, user_id, period_start, period_end, statuses, exclude_request_id=None):
    placeholders = ",".join("?" for _ in statuses)
    parameters = [user_id, period_end.isoformat(), period_start.isoformat(), *statuses]
    sql = f"SELECT id,start_date,end_date,start_period,end_period FROM leave_requests WHERE user_id=? AND leave_type='司龄假' AND start_date<=? AND end_date>=? AND status IN ({placeholders})"
    if exclude_request_id is not None:
        sql += " AND id<>?"
        parameters.append(exclude_request_id)
    total = 0.0
    for row in connection.execute(sql, parameters).fetchall():
        total += attendance_overlap_days(dict(row), period_start, period_end)
    return round(total, 2)


def seniority_leave_metrics(connection, user, on_date=None):
    try:
        hire_date = date.fromisoformat(user.get("hire_date") or "")
    except (TypeError, ValueError):
        return {"seniority_leave_quota": None, "seniority_leave_used": None, "seniority_leave_available": None, "seniority_leave_period_start": None, "seniority_leave_next_release": None}
    on_date = on_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    period = seniority_period(hire_date, on_date)
    used = seniority_leave_usage(connection, user["linear_id"], period["start"], period["end"], ("approved",))
    return {
        "seniority_leave_quota": period["quota"],
        "seniority_leave_used": used,
        "seniority_leave_available": round(max(0, period["quota"] - used), 2),
        "seniority_leave_period_start": period["start"].isoformat(),
        "seniority_leave_next_release": (period["end"] + timedelta(days=1)).isoformat(),
    }


def seniority_quota_error(connection, user, start_date, end_date, leave_days, exclude_request_id=None):
    try:
        hire_date = date.fromisoformat(user.get("hire_date") or "")
    except (TypeError, ValueError):
        return {"error": "seniority_hire_date_required"}
    period = seniority_period(hire_date, start_date)
    if end_date > period["end"]:
        return {"error": "seniority_leave_crosses_anniversary", "period_end": period["end"].isoformat()}
    used = seniority_leave_usage(
        connection, user["linear_id"], period["start"], period["end"],
        ("pending_primary", "pending_secondary", "approved"), exclude_request_id,
    )
    remaining = round(max(0, period["quota"] - used), 2)
    if leave_days > remaining + 1e-9:
        return {"error": "seniority_leave_quota_exceeded", "quota": period["quota"], "used": used, "remaining": remaining, "next_release": (period["end"] + timedelta(days=1)).isoformat()}
    return None


def excel_column_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def excel_date_serial(value):
    return (value - date(1899, 12, 30)).days


def xml_text(value):
    return html.escape(str(value if value is not None else ""), quote=False)


def worksheet_xml(title, period_text, headers, rows, widths, date_columns=(), numeric_columns=(), total_row=None):
    column_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )

    def cell(ref, value, style=4):
        if isinstance(value, date):
            return f'<c r="{ref}" s="6"><v>{excel_date_serial(value)}</v></c>'
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{xml_text(value)}</t></is></c>'

    last_column = excel_column_name(len(headers))
    sheet_rows = [
        f'<row r="1" ht="24" customHeight="1">{cell("A1", title, 1)}</row>',
        f'<row r="2">{cell("A2", period_text, 2)}</row>',
        '<row r="3"></row>',
        '<row r="4" ht="22" customHeight="1">' + "".join(cell(f'{excel_column_name(i)}4', value, 3) for i, value in enumerate(headers, 1)) + '</row>',
    ]
    for row_number, values in enumerate(rows, 5):
        cells = []
        for column_number, value in enumerate(values, 1):
            style = 6 if column_number in date_columns else (5 if column_number in numeric_columns else 4)
            cells.append(cell(f'{excel_column_name(column_number)}{row_number}', value, style))
        sheet_rows.append(f'<row r="{row_number}">' + "".join(cells) + '</row>')
    if total_row is not None:
        row_number = 5 + len(rows)
        cells = []
        for column_number, value in enumerate(total_row, 1):
            style = 7 if column_number == 1 else (8 if column_number in numeric_columns else 7)
            cells.append(cell(f'{excel_column_name(column_number)}{row_number}', value, style))
        sheet_rows.append(f'<row r="{row_number}">' + "".join(cells) + '</row>')
    last_row = 4 + len(rows) + (1 if total_row is not None else 0)
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView showGridLines="0" workbookViewId="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="18"/><cols>{column_xml}</cols><sheetData>{''.join(sheet_rows)}</sheetData><autoFilter ref="A4:{last_column}{max(4, last_row)}"/><mergeCells count="1"><mergeCell ref="A1:{last_column}1"/></mergeCells><pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/><pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>'''


def build_attendance_workbook(users, requests, month_start, month_end):
    leave_types = list(LEAVE_TYPES)
    by_user = {user["linear_id"]: {leave_type: 0.0 for leave_type in leave_types} for user in users}
    detail_rows = []
    for request in requests:
        overlap_days = attendance_overlap_days(request, month_start, month_end)
        if overlap_days <= 0:
            continue
        if request["user_id"] in by_user:
            by_user[request["user_id"]].setdefault(request["leave_type"], 0.0)
            by_user[request["user_id"]][request["leave_type"]] += overlap_days
        detail_rows.append([
            request["name"], request["email"], request["company"] or "", request["leave_type"],
            date.fromisoformat(request["start_date"]), request.get("start_period") or "上午",
            date.fromisoformat(request["end_date"]), request.get("end_period") or "下午",
            overlap_days, "终审通过", request.get("secondary_decided_at") or "",
        ])

    summary_rows = []
    for user in users:
        totals = by_user[user["linear_id"]]
        values = [round(totals.get(leave_type, 0), 2) for leave_type in leave_types]
        summary_rows.append([user["name"], user["email"], user["company"] or "", *values, round(sum(values), 2)])
    numeric_count = len(leave_types) + 1
    total_values = [round(sum(row[index] for row in summary_rows), 2) for index in range(3, 3 + numeric_count)] if summary_rows else [0.0] * numeric_count
    summary_total = ["合计", "", "", *total_values]
    period_text = f'统计期间：{month_start.isoformat()} 至 {month_end.isoformat()}　仅含终审通过的请假记录　导出时间：{datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")}'

    summary_xml = worksheet_xml(
        "考勤汇总", period_text,
        ["姓名", "邮箱", "公司", *leave_types, "合计请假天数"], summary_rows,
        [14, 28, 18, *([10] * len(leave_types)), 14], numeric_columns=tuple(range(4, 4 + numeric_count)), total_row=summary_total,
    )
    detail_xml = worksheet_xml(
        "请假明细", period_text,
        ["姓名", "邮箱", "公司", "请假类型", "开始日期", "开始时段", "结束日期", "结束时段", "月内天数", "审批状态", "终审时间"], detail_rows,
        [14, 28, 18, 12, 13, 10, 13, 10, 11, 12, 20], date_columns=(5, 7), numeric_columns=(9,),
    )
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/><numFmt numFmtId="165" formatCode="0.0"/></numFmts><fonts count="4"><font><sz val="10"/><name val="Arial"/></font><font><b/><sz val="14"/><color rgb="FF173F31"/><name val="Arial"/></font><font><i/><sz val="10"/><color rgb="FF65746D"/><name val="Arial"/></font><font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF173F31"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border/><border><bottom style="thin"><color rgb="FFDCE3DF"/></bottom></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="9"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="0" fontId="3" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"/><xf numFmtId="165" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>''')
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>''')
        archive.writestr("docProps/app.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>休假助手</Application></Properties>''')
        archive.writestr("docProps/core.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>考勤记录 {month_start.strftime("%Y-%m")}</dc:title><dc:creator>休假助手</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}</dcterms:created></cp:coreProperties>''')
        archive.writestr("xl/workbook.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="考勤汇总" sheetId="1" r:id="rId1"/><sheet name="请假明细" sheetId="2" r:id="rId2"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", summary_xml)
        archive.writestr("xl/worksheets/sheet2.xml", detail_xml)
    return payload.getvalue()


def ensure_user(user):
    with db() as connection:
        existing = connection.execute("SELECT * FROM users WHERE linear_id=?", (user["id"],)).fetchone()
        email = (user.get("email") or "").lower()
        if not existing and email:
            existing = connection.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
            if existing and existing["linear_id"] != user["id"]:
                old_id = existing["linear_id"]
                connection.execute("UPDATE leave_requests SET user_id=? WHERE user_id=?", (user["id"], old_id))
                connection.execute("UPDATE users SET linear_id=? WHERE linear_id=?", (user["id"], old_id))
                existing = connection.execute("SELECT * FROM users WHERE linear_id=?", (user["id"],)).fetchone()
        if not existing:
            role = "admin" if email in ADMIN_EMAILS else "employee"
            connection.execute("INSERT INTO users(linear_id,name,email,role) VALUES(?,?,?,?)", (user["id"], user.get("name") or "Linear 用户", user.get("email") or "", role))
        else:
            role = "admin" if email in ADMIN_EMAILS else ("employee" if existing["role"] == "admin" else existing["role"])
            if existing["profile_locked"]:
                connection.execute("UPDATE users SET role=? WHERE linear_id=?", (role, user["id"]))
            else:
                connection.execute("UPDATE users SET name=?,email=?,role=? WHERE linear_id=?", (user.get("name") or existing["name"], user.get("email") or existing["email"], role, user["id"]))
        current = dict(connection.execute("SELECT * FROM users WHERE linear_id=?", (user["id"],)).fetchone())
        return {**current, **leave_metrics(current), **seniority_leave_metrics(connection, current)}


def queue_email(recipient, subject, body):
    with db() as connection:
        cursor = connection.execute("INSERT INTO email_outbox(recipient,subject,body) VALUES(?,?,?)", (recipient, subject, body))
        message_id = cursor.lastrowid
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM")
    if not all((smtp_host, smtp_user, smtp_password, sender)):
        return False
    try:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
        with db() as connection:
            connection.execute("UPDATE email_outbox SET status='sent',sent_at=CURRENT_TIMESTAMP WHERE id=?", (message_id,))
        return True
    except Exception as exc:
        with db() as connection:
            connection.execute("UPDATE email_outbox SET status='failed',error=? WHERE id=?", (str(exc)[:500], message_id))
        return False


def notify_approver(request_id, status, applicant_name, start_date, end_date):
    approver = {
        "pending_primary": (PRIMARY_APPROVER, "初审"),
        "pending_secondary": (SECONDARY_APPROVER, "终审"),
    }.get(status)
    if not approver or not APPROVAL_NOTIFICATION_CHANNELS:
        return

    recipient, stage = approver
    message = f"请假申请 #{request_id} 待{stage}\n申请人：{applicant_name}\n日期：{start_date} 至 {end_date}"
    action_url = f"{APP_ORIGIN}/admin" if APP_ORIGIN else ""
    for channel in APPROVAL_NOTIFICATION_CHANNELS:
        try:
            channel.send(recipient, message, action_url)
        except Exception as error:
            print(f"审批通知发送失败（申请 #{request_id}，{stage}，{type(channel).__name__}） {error}", flush=True)


init_db()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign(value: str) -> str:
    return b64(hmac.new(SESSION_SECRET, value.encode(), hashlib.sha256).digest())


def make_session(user: dict) -> str:
    payload = b64(json.dumps({**user, "exp": __import__("time").time() + 604800}, ensure_ascii=False).encode())
    return f"{payload}.{sign(payload)}"


class Handler(BaseHTTPRequestHandler):
    def origin(self):
        return APP_ORIGIN or f"https://{self.headers.get('Host')}"

    def cookies(self):
        jar = SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        return {key: morsel.value for key, morsel in jar.items()}

    def user(self):
        try:
            payload, signature = self.cookies().get("linear_session", "").split(".", 1)
            if not hmac.compare_digest(sign(payload), signature):
                return None
            value = json.loads(unb64(payload))
            if value.get("exp", 0) <= time.time():
                return None
            current = {**value, **ensure_user(value)}
            return current if current.get("is_active", 1) else None
        except Exception:
            return None

    def send_cookie(self, name, value, max_age, path="/"):
        self.send_header("Set-Cookie", f"{name}={urllib.parse.quote(value)}; Path={path}; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax")

    def redirect(self, location, cookies=()):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for item in cookies:
            self.send_cookie(*item)
        self.end_headers()

    def json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def login_page(self, message=""):
        notice = f'<div class="msg">{html.escape(message)}</div>' if message else ""
        action = '<a class="login" href="/auth/linear"><b>L</b> 使用 Linear 登录</a>' if CLIENT_ID else '<div class="msg">Linear OAuth 尚未配置，请管理员设置 Client ID。</div>'
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录｜休假助手</title><style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6f4;color:#173f31;font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}}.card{{width:min(430px,calc(100% - 32px));background:white;border:1px solid #e1e7e3;border-radius:28px;padding:36px;box-shadow:0 24px 70px #173f3118}}.logo{{width:54px;height:54px;display:grid;place-items:center;border-radius:18px;background:#173f31;color:#fff;font-size:24px;font-weight:900}}h1{{margin:24px 0 8px;font-size:28px}}p{{color:#718079;line-height:1.7}}.login{{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;margin-top:26px;padding:13px;border-radius:13px;background:#5e6ad2;color:white;text-decoration:none;font-weight:800}}.msg{{margin-top:16px;padding:10px 12px;border-radius:10px;background:#fff2cc;color:#795b00;font-size:13px}}.hint{{font-size:12px;text-align:center;margin-top:16px}}</style></head><body><main class="card"><div class="logo">休</div><h1>登录休假助手</h1><p>使用你的 Linear 工作区账号继续。本站只读取姓名和邮箱，用于识别请假申请人。</p>{notice}{action}<p class="hint">登录状态通过安全的 HttpOnly Cookie 保存 7 天</p></main></body></html>'''

    def admin_page(self, user):
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>管理后台｜休假助手</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f4;color:#20352d;font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}}header{{height:70px;background:#173f31;color:white;display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100% - 1200px)/2))}}header a{{color:white;text-decoration:none}}main{{max-width:1200px;margin:auto;padding:34px 24px}}h1{{margin:0 0 6px}}.sub{{color:#738079;margin:0 0 28px}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:22px}}.stat,.panel{{background:white;border:1px solid #e3e8e5;border-radius:20px;padding:20px}}.stat b{{display:block;font-size:32px;margin-top:8px}}.panel{{margin-bottom:20px;overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:760px}}th,td{{padding:13px 10px;text-align:left;border-bottom:1px solid #edf0ee;font-size:13px}}th{{color:#718079}}select,button{{border:1px solid #dce3df;border-radius:9px;background:white;padding:7px 10px}}button{{cursor:pointer}}.approve{{background:#173f31;color:white}}.reject{{color:#a6382a}}.status{{font-weight:700}}.pending{{color:#946f00}}.approved{{color:#176b3b}}.rejected{{color:#a6382a}}@media(max-width:700px){{.stats{{grid-template-columns:1fr}}}}</style></head><body><header><b>休假助手 · 管理后台</b><div>{html.escape(user['name'])}　<a href="/">返回员工端</a>　<a href="/auth/logout">退出</a></div></header><main><h1>组织管理</h1><p class="sub">管理成员角色、查看所有请假申请并完成审批。</p><section class="stats"><div class="stat">成员数<b id="usersCount">—</b></div><div class="stat">待审批<b id="pendingCount">—</b></div><div class="stat">本月请假天数<b id="daysCount">—</b></div></section><section class="panel"><h2>请假审批</h2><table><thead><tr><th>申请人</th><th>类型</th><th>日期</th><th>天数</th><th>原因</th><th>状态</th><th>操作</th></tr></thead><tbody id="requests"></tbody></table></section><section class="panel"><h2>成员与权限</h2><table><thead><tr><th>姓名</th><th>邮箱</th><th>加入时间</th><th>角色</th></tr></thead><tbody id="users"></tbody></table></section></main><script>const labels={{admin:'管理员',manager:'审批人',employee:'员工'}},statusLabels={{pending:'待审批',approved:'已通过',rejected:'已驳回'}};const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));async function load(){{let r=await fetch('/api/admin/data');if(!r.ok){{location='/';return}}let d=await r.json();usersCount.textContent=d.users.length;pendingCount.textContent=d.requests.filter(x=>x.status==='pending').length;daysCount.textContent=d.requests.filter(x=>x.created_at.slice(0,7)===new Date().toISOString().slice(0,7)).reduce((s,x)=>s+x.days,0);requests.innerHTML=d.requests.map(x=>`<tr><td><b>${{esc(x.name)}}</b><br><small>${{esc(x.email)}}</small></td><td>${{esc(x.leave_type)}}</td><td>${{esc(x.start_date)}} 至 ${{esc(x.end_date)}}</td><td>${{x.days}}</td><td>${{esc(x.reason||'—')}}</td><td class="status ${{x.status}}">${{statusLabels[x.status]}}</td><td>${{x.status==='pending'?`<button class="approve" onclick="status(${{x.id}},'approved')">通过</button> <button class="reject" onclick="status(${{x.id}},'rejected')">驳回</button>`:'—'}}</td></tr>`).join('')||'<tr><td colspan="7">暂无申请</td></tr>';users.innerHTML=d.users.map(x=>`<tr><td><b>${{esc(x.name)}}</b></td><td>${{esc(x.email)}}</td><td>${{esc(x.created_at.slice(0,10))}}</td><td><select onchange="role('${{x.linear_id}}',this.value)">${{Object.entries(labels).map(([v,l])=>`<option value="${{v}}" ${{x.role===v?'selected':''}}>${{l}}</option>`).join('')}}</select></td></tr>`).join('')}}async function status(id,value){{await fetch('/api/admin/requests/'+id+'/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{status:value}})}});load()}}async function role(id,value){{await fetch('/api/admin/users/'+encodeURIComponent(id)+'/role',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{role:value}})}});load()}}load();</script></body></html>'''

    def admin_page_v2(self, user):
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>管理后台｜休假助手</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f4;color:#20352d;font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}}header{{min-height:70px;background:#173f31;color:white;display:flex;align-items:center;justify-content:space-between;padding:14px max(24px,calc((100% - 1380px)/2))}}header a{{color:white;text-decoration:none;margin-left:16px}}main{{max-width:1380px;margin:auto;padding:34px 24px}}h1{{margin:0 0 6px}}.sub{{color:#738079;margin:0 0 28px}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:22px}}.stat,.panel{{background:white;border:1px solid #e3e8e5;border-radius:20px;padding:20px}}.stat b{{display:block;font-size:32px;margin-top:8px}}.panel{{margin-bottom:20px;overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:1100px}}th,td{{padding:12px 9px;text-align:left;border-bottom:1px solid #edf0ee;font-size:12px;white-space:nowrap}}th{{color:#718079}}select,button{{border:1px solid #dce3df;border-radius:9px;background:white;padding:7px 9px}}button{{cursor:pointer}}.approve{{background:#173f31;color:white}}.reject{{color:#a6382a}}.status{{font-weight:700}}.pending{{color:#946f00}}.approved{{color:#176b3b}}.rejected{{color:#a6382a}}.num{{font-variant-numeric:tabular-nums}}@media(max-width:700px){{.stats{{grid-template-columns:1fr}}}}</style></head><body><header><b>休假助手 · 管理后台</b><div>{html.escape(user['name'])}<a href="/">员工端</a><a href="/auth/logout">退出</a></div></header><main><h1>组织管理</h1><p class="sub">员工基础信息和年假额度按导入表格公式每日重新计算。</p><section class="stats"><div class="stat">员工数<b id="usersCount">—</b></div><div class="stat">待审批<b id="pendingCount">—</b></div><div class="stat">累计可休年假<b id="availableCount">—</b></div></section><section class="panel"><h2>请假审批</h2><table><thead><tr><th>申请人</th><th>类型</th><th>日期</th><th>天数</th><th>原因</th><th>状态</th><th>操作</th></tr></thead><tbody id="requests"></tbody></table></section><section class="panel"><h2>员工年假信息</h2><table><thead><tr><th>姓名</th><th>邮箱</th><th>工作开始时间</th><th>入职时间</th><th>工龄</th><th>法定标准</th><th>最新法定年假</th><th>已休年假</th><th>可休年假</th><th>角色</th></tr></thead><tbody id="users"></tbody></table></section></main><script>const labels={{admin:'管理员',manager:'审批人',employee:'员工'}},statusLabels={{pending:'待审批',approved:'已通过',rejected:'已驳回'}},esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])),num=v=>v==null?'—':Number(v).toFixed(2);async function load(){{const r=await fetch('/api/admin/data');if(!r.ok){{location='/';return}}const d=await r.json();usersCount.textContent=d.users.length;pendingCount.textContent=d.requests.filter(x=>x.status==='pending').length;availableCount.textContent=d.users.reduce((s,x)=>s+(x.annual_leave_available||0),0).toFixed(2)+' 天';requests.innerHTML=d.requests.map(x=>`<tr><td><b>${{esc(x.name)}}</b><br><small>${{esc(x.email)}}</small></td><td>${{esc(x.leave_type)}}</td><td>${{esc(x.start_date)}} 至 ${{esc(x.end_date)}}</td><td>${{x.days}}</td><td>${{esc(x.reason||'—')}}</td><td class="status ${{x.status}}">${{statusLabels[x.status]}}</td><td>${{x.status==='pending'?`<button class="approve" onclick="setStatus(${{x.id}},'approved')">通过</button> <button class="reject" onclick="setStatus(${{x.id}},'rejected')">驳回</button>`:'—'}}</td></tr>`).join('')||'<tr><td colspan="7">暂无申请</td></tr>';users.innerHTML=d.users.map(x=>`<tr><td><b>${{esc(x.name)}}</b></td><td>${{esc(x.email)}}</td><td>${{esc(x.work_start_date||'—')}}</td><td>${{esc(x.hire_date||'—')}}</td><td class="num">${{num(x.service_years)}}</td><td class="num">${{num(x.statutory_standard)}}</td><td class="num">${{num(x.statutory_accrued)}}</td><td class="num">${{num(x.annual_leave_used)}}</td><td class="num"><b>${{num(x.annual_leave_available)}}</b></td><td><select onchange="setRole('${{x.linear_id}}',this.value)">${{Object.entries(labels).map(([v,l])=>`<option value="${{v}}" ${{x.role===v?'selected':''}}>${{l}}</option>`).join('')}}</select></td></tr>`).join('')}}async function setStatus(id,status){{await fetch('/api/admin/requests/'+id+'/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{status}})}});load()}}async function setRole(id,role){{await fetch('/api/admin/users/'+encodeURIComponent(id)+'/role',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{role}})}});load()}}load();</script></body></html>'''

    def admin_page_v3(self, user):
        page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>管理后台｜休假助手</title><style>*{box-sizing:border-box}body{margin:0;background:#f4f6f4;color:#20352d;font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}header{min-height:70px;background:#173f31;color:white;display:flex;align-items:center;justify-content:space-between;padding:14px max(24px,calc((100% - 1480px)/2))}header a{color:white;text-decoration:none;margin-left:16px}main{max-width:1480px;margin:auto;padding:34px 24px}.sub{color:#738079}.flow{background:#e5ece8;padding:14px 18px;border-radius:14px;margin:18px 0}.warn{background:#fff2cc;color:#795b00;padding:10px 14px;border-radius:10px;margin-bottom:18px}.panel{background:white;border:1px solid #e3e8e5;border-radius:20px;padding:20px;margin-bottom:20px;overflow:auto}table{width:100%;border-collapse:collapse;min-width:1240px}th,td{padding:12px 9px;text-align:left;border-bottom:1px solid #edf0ee;font-size:12px;white-space:nowrap}th{color:#718079}select,button,input{border:1px solid #dce3df;border-radius:9px;background:white;padding:8px 10px;font:inherit}button{cursor:pointer}.approve,.save{background:#173f31;color:white}.reject{color:#a6382a}.edit{color:#173f31}.pending_primary,.pending_secondary{color:#946f00;font-weight:700}.approved{color:#176b3b;font-weight:700}.rejected{color:#a6382a;font-weight:700}dialog{border:0;border-radius:20px;padding:0;box-shadow:0 30px 90px #173f3140;width:min(620px,calc(100% - 28px))}dialog::backdrop{background:#10251d80}.modal{padding:24px}.modal h2{margin-top:0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.field{display:grid;gap:6px}.field label{font-size:12px;color:#65746d}.field input{width:100%}.actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}.error{color:#a6382a;min-height:20px;margin-top:10px}@media(max-width:650px){.grid{grid-template-columns:1fr}}</style></head><body><header><b>休假助手 · 管理后台</b><div>__ADMIN_NAME__<a href="/">员工端</a><a href="/auth/logout">退出</a></div></header><main><h1>两级审批</h1><p class="sub">当前账号：__CURRENT_EMAIL__</p><div class="flow">员工提交 → __PRIMARY__ 初审 → __SECONDARY__ 终审 → 完成</div>__EMAIL_STATUS__<section class="panel"><h2>请假审批</h2><table><thead><tr><th>申请人</th><th>类型</th><th>日期与时段</th><th>天数</th><th>状态</th><th>初审</th><th>终审</th><th>操作</th></tr></thead><tbody id="requests"></tbody></table></section><section class="panel"><h2>员工年假信息</h2><table><thead><tr><th>姓名</th><th>邮箱</th><th>公司</th><th>工作开始</th><th>入职时间</th><th>工龄</th><th>法定标准</th><th>近三年累计</th><th>已休</th><th>可休</th><th>角色</th><th>操作</th></tr></thead><tbody id="users"></tbody></table></section></main><dialog id="editor"><form class="modal" id="editForm"><h2>编辑员工基本信息</h2><div class="grid"><div class="field"><label for="editName">姓名</label><input id="editName" maxlength="100" required></div><div class="field"><label for="editEmail">邮箱</label><input id="editEmail" type="email" maxlength="254" required></div><div class="field"><label for="editCompany">公司</label><input id="editCompany" maxlength="100"></div><div class="field"><label for="editWorkStart">工作开始时间</label><input id="editWorkStart" type="date"></div><div class="field"><label for="editHireDate">入职时间</label><input id="editHireDate" type="date"></div><div class="field"><label for="editUsed">已休年假（天）</label><input id="editUsed" type="number" min="0" max="1000" step="0.25" required></div></div><div id="editError" class="error"></div><div class="actions"><button type="button" onclick="editor.close()">取消</button><button class="save" type="submit">保存</button></div></form></dialog><script>const currentEmail='__CURRENT_EMAIL__',primary='__PRIMARY__',secondary='__SECONDARY__';const labels={admin:'管理员',manager:'审批人',employee:'员工'},statusLabels={pending_primary:'待初审',pending_secondary:'初审通过 · 待终审',approved:'已通过',rejected:'已驳回'},decisionLabels={approved:'通过',rejected:'驳回'},esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),num=v=>v==null?'—':Number(v).toFixed(2);let currentUsers=[],editingId='';function actions(x){const allowed=(x.status==='pending_primary'&&currentEmail===primary)||(x.status==='pending_secondary'&&currentEmail===secondary);return allowed?`<button class="approve" onclick="decide(${x.id},'approved')">通过</button> <button class="reject" onclick="decide(${x.id},'rejected')">驳回</button>`:'—'}async function load(){const r=await fetch('/api/admin/data');if(!r.ok){location='/';return}const d=await r.json();currentUsers=d.users;requests.innerHTML=d.requests.map(x=>`<tr><td><b>${esc(x.name)}</b><br><small>${esc(x.email)}</small></td><td>${esc(x.leave_type)}</td><td>${esc(x.start_date)} ${esc(x.start_period||'上午')} 至 ${esc(x.end_date)} ${esc(x.end_period||'下午')}</td><td>${x.days}</td><td class="${x.status}">${statusLabels[x.status]||x.status}</td><td>${decisionLabels[x.primary_decision]||'—'}</td><td>${decisionLabels[x.secondary_decision]||'—'}</td><td>${actions(x)}</td></tr>`).join('')||'<tr><td colspan="8">暂无申请</td></tr>';users.innerHTML=d.users.map((x,i)=>`<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.email)}</td><td>${esc(x.company||'—')}</td><td>${esc(x.work_start_date||'—')}</td><td>${esc(x.hire_date||'—')}</td><td>${num(x.service_years)}</td><td>${num(x.statutory_standard)}</td><td>${num(x.three_year_earned)}</td><td>${num(x.annual_leave_used)}</td><td><b>${num(x.annual_leave_available)}</b></td><td><select onchange="setRole('${encodeURIComponent(x.linear_id)}',this.value)">${Object.entries(labels).map(([v,l])=>`<option value="${v}" ${x.role===v?'selected':''}>${l}</option>`).join('')}</select></td><td><button class="edit" onclick="openEdit(${i})">编辑</button></td></tr>`).join('')}function openEdit(index){const x=currentUsers[index];editingId=x.linear_id;editName.value=x.name||'';editEmail.value=x.email||'';editEmail.readOnly=x.role==='admin';editCompany.value=x.company||'';editWorkStart.value=x.work_start_date||'';editHireDate.value=x.hire_date||'';editUsed.value=x.annual_leave_used??0;editError.textContent=x.role==='admin'?'管理员邮箱由系统保护，不能在此修改。':'';editor.showModal()}editForm.addEventListener('submit',async e=>{e.preventDefault();editError.textContent='';const body={name:editName.value,email:editEmail.value,company:editCompany.value,work_start_date:editWorkStart.value,hire_date:editHireDate.value,annual_leave_used:Number(editUsed.value)};const r=await fetch('/api/admin/users/'+encodeURIComponent(editingId)+'/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json().catch(()=>({}));if(!r.ok){editError.textContent=({invalid_name:'姓名不能为空',invalid_email:'邮箱格式不正确',email_exists:'该邮箱已被其他员工使用',invalid_date:'日期格式不正确',invalid_annual_leave_used:'已休年假必须是 0 至 1000 的数字',protected_admin_email:'管理员邮箱受保护'}[data.error]||'保存失败，请重试');return}editor.close();await load()});async function decide(id,status){const r=await fetch('/api/admin/requests/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});const data=await r.json().catch(()=>({}));if(!r.ok)alert(data.error==='annual_leave_exceeded'?`年假余额不足，仅剩 ${data.remaining} 天`:'当前账号不能处理此审批步骤');load()}async function setRole(encodedId,role){const r=await fetch('/api/admin/users/'+encodedId+'/role',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role})});if(!r.ok){const data=await r.json().catch(()=>({}));alert(data.error==='protected_admin'?'固定管理员不能降级':'角色修改失败')}load()}load();</script></body></html>'''
        page = page.replace("__PRIMARY__ 初审 → __SECONDARY__ 终审", f"{PRIMARY_APPROVER_NAME} 初审 → {SECONDARY_APPROVER_NAME} 终审")
        page = page.replace("pending_primary:'待初审',pending_secondary:'初审通过 · 待终审'", f"pending_primary:'待{PRIMARY_APPROVER_NAME}初审',pending_secondary:'{PRIMARY_APPROVER_NAME}初审通过 · 待{SECONDARY_APPROVER_NAME}终审'")
        configured = all(os.environ.get(key) for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"))
        email_status = "" if configured else '<div class="warn">邮件通知已进入队列，但尚未配置 SMTP，暂时不会实际发送。</div>'
        return page.replace("__ADMIN_NAME__", html.escape(user["name"])).replace("__CURRENT_EMAIL__", html.escape(user["email"].lower())).replace("__PRIMARY__", PRIMARY_APPROVER).replace("__SECONDARY__", SECONDARY_APPROVER).replace("__EMAIL_STATUS__", email_status)

    def admin_page_v4(self, user):
        page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>管理后台｜休假助手</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6f4;color:#20352d;font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}header{min-height:70px;background:#173f31;color:white;display:flex;align-items:center;justify-content:space-between;padding:14px max(24px,calc((100% - 1480px)/2))}header a{color:white;text-decoration:none;margin-left:16px}main{max-width:1480px;margin:auto;padding:34px 24px}.sub{color:#738079}.flow{background:#e5ece8;padding:14px 18px;border-radius:14px;margin:18px 0}.warn{background:#fff2cc;color:#795b00;padding:10px 14px;border-radius:10px;margin-bottom:18px}.panel{background:white;border:1px solid #e3e8e5;border-radius:20px;padding:20px;margin-bottom:20px;overflow:auto}.panel-title{display:flex;align-items:center;justify-content:space-between;gap:16px}.panel-title h2{margin:0}.formula{color:#738079;font-size:12px;margin:8px 0 18px}.export-tools{display:flex;align-items:end;gap:10px;flex-wrap:wrap}.export-tools label{display:grid;gap:6px;color:#65746d;font-size:12px}.export-tools input{min-width:170px}table{width:100%;border-collapse:collapse;min-width:1280px}th,td{padding:12px 9px;text-align:left;border-bottom:1px solid #edf0ee;font-size:12px;white-space:nowrap}th{color:#718079}select,button,input,textarea{border:1px solid #dce3df;border-radius:9px;background:white;padding:8px 10px;font:inherit}button{cursor:pointer}.approve,.save,.add{background:#173f31;color:white}.reject,.delete{color:#a6382a}.edit{color:#173f31}.pending_primary,.pending_secondary{color:#946f00;font-weight:700}.approved{color:#176b3b;font-weight:700}.rejected{color:#a6382a;font-weight:700}dialog{border:0;border-radius:20px;padding:0;box-shadow:0 30px 90px #173f3140;width:min(660px,calc(100% - 28px))}dialog::backdrop{background:#10251d80}.modal{padding:24px}.modal h2{margin-top:0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.field{display:grid;gap:6px}.field label{font-size:12px;color:#65746d}.field input,.field select,.field textarea{width:100%}.wide{grid-column:1/-1}.hint{grid-column:1/-1;background:#f4f7f5;border-radius:12px;padding:12px;color:#65746d;font-size:12px;line-height:1.7}.actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}.error{color:#a6382a;min-height:20px;margin-top:10px}@media(max-width:650px){.grid{grid-template-columns:1fr}.panel-title{align-items:flex-start;flex-direction:column}.wide{grid-column:auto}}
</style></head><body><header><b>休假助手 · 管理后台</b><div>__ADMIN_NAME__<a href="/">员工端</a><a href="/auth/logout">退出</a></div></header><main><h1>两级审批</h1><p class="sub">当前账号：__CURRENT_EMAIL_TEXT__</p><div class="flow">员工提交 → __PRIMARY_NAME__ 初审 → __SECONDARY_NAME__ 终审 → 完成</div>__EMAIL_STATUS__
<section class="panel"><div class="panel-title"><div><h2>请假审批与记录修改</h2><p class="formula">管理员可代员工申请过去、今天或未来的假期，也可代终审人完成终审；系统会保留实际操作账号，并继续检查时间重叠和假期额度。</p></div><button class="add" onclick="openLeaveCreate()">＋ 代员工申请</button></div><table><thead><tr><th>申请人</th><th>类型</th><th>日期与时段</th><th>天数</th><th>说明</th><th>状态</th><th>初审</th><th>终审</th><th>操作</th></tr></thead><tbody id="requests"></tbody></table></section>
<section class="panel"><div class="panel-title"><div><h2>考勤记录导出</h2><p class="formula">按月份导出 Excel，包含考勤汇总和请假明细，仅统计终审通过的请假记录。</p></div><div class="export-tools"><label for="attendanceMonth">月份<input id="attendanceMonth" type="month" required></label><button class="add" onclick="exportAttendance()">导出 Excel</button></div></div></section>
<section class="panel"><div class="panel-title"><h2>员工年假信息</h2><button class="add" onclick="openCreate()">＋ 新增员工</button></div><p class="formula">工龄、法定标准、近三年累计和可休年假由系统根据工作开始时间、入职时间及已休年假自动计算。</p><table><thead><tr><th>姓名</th><th>邮箱</th><th>公司</th><th>工作开始</th><th>入职时间</th><th>工龄</th><th>法定标准</th><th>近三年累计</th><th>已休</th><th>可休</th><th>角色</th><th>操作</th></tr></thead><tbody id="users"></tbody></table></section></main>
<dialog id="editor"><form class="modal" id="editForm"><h2 id="editorTitle">员工基本信息</h2><div class="grid"><div class="field"><label for="editName">姓名</label><input id="editName" maxlength="100" required></div><div class="field"><label for="editEmail">邮箱</label><input id="editEmail" type="email" maxlength="254" required></div><div class="field"><label for="editCompany">公司</label><input id="editCompany" maxlength="100"></div><div class="field"><label for="editWorkStart">工作开始时间</label><input id="editWorkStart" type="date"></div><div class="field"><label for="editHireDate">入职时间</label><input id="editHireDate" type="date"></div><div class="field"><label for="editUsed">已休年假（天）</label><input id="editUsed" type="number" min="0" max="1000" step="0.25" value="0" required></div><div class="hint">法定标准按工龄自动计算：不足 1 年为 0 天；1–10 年为 5 天；10–20 年为 10 天；20 年及以上为 15 天。年假最多累计最近三年。</div></div><div id="editError" class="error"></div><div class="actions"><button type="button" onclick="editor.close()">取消</button><button class="save" type="submit">保存</button></div></form></dialog>
<dialog id="leaveEditor"><form class="modal" id="leaveEditForm"><h2>修改请假记录</h2><div class="grid"><div class="field"><label for="leaveType">请假类型</label><select id="leaveType" required><option>年假</option><option>调休</option><option>病假</option><option>事假</option><option>婚假</option><option>产假</option><option>陪产假</option><option>丧假</option><option>育儿假</option><option>路程假</option><option>司龄假</option><option>Remote</option></select></div><div class="field"><label for="leaveStatus">审批状态</label><select id="leaveStatus" required><option value="pending_primary">待初审</option><option value="pending_secondary">待终审</option><option value="approved">已通过</option><option value="rejected">已驳回</option></select></div><div class="field"><label for="leaveStartDate">开始日期</label><input id="leaveStartDate" type="date" required></div><div class="field"><label for="leaveStartPeriod">开始时段</label><select id="leaveStartPeriod"><option>上午</option><option>下午</option></select></div><div class="field"><label for="leaveEndDate">结束日期</label><input id="leaveEndDate" type="date" required></div><div class="field"><label for="leaveEndPeriod">结束时段</label><select id="leaveEndPeriod"><option>上午</option><option>下午</option></select></div><div class="field wide"><label for="leaveHandover">工作交接</label><input id="leaveHandover" maxlength="100"></div><div class="field wide"><label for="leaveReason">请假说明</label><textarea id="leaveReason" maxlength="1000" rows="4"></textarea></div><div class="hint">保存后系统会按上午/下午重新计算天数，并检查该员工是否存在时间重叠的有效请假。Remote 每人每周最多 1 天；司龄假按入职周年自动更新额度；修改已通过的年假时，年假已休天数会同步调整。</div></div><div id="leaveEditError" class="error"></div><div class="actions"><button type="button" onclick="leaveEditor.close()">取消</button><button class="save" type="submit">保存修改</button></div></form></dialog>
<dialog id="leaveCreateEditor"><form class="modal" id="leaveCreateForm"><h2>代员工申请假期</h2><div class="grid"><div class="field wide"><label for="createLeaveEmployee">员工</label><select id="createLeaveEmployee" required></select></div><div class="field"><label for="createLeaveType">请假类型</label><select id="createLeaveType" required><option>年假</option><option>调休</option><option>病假</option><option>事假</option><option>婚假</option><option>产假</option><option>陪产假</option><option>丧假</option><option>育儿假</option><option>路程假</option><option>司龄假</option><option>Remote</option></select></div><div class="field"><label for="createLeaveStatus">审批状态</label><select id="createLeaveStatus" required><option value="pending_primary">待初审</option><option value="pending_secondary">待终审</option><option value="approved">已通过</option><option value="rejected">已驳回</option></select></div><div class="field"><label for="createLeaveStartDate">开始日期</label><input id="createLeaveStartDate" type="date" required></div><div class="field"><label for="createLeaveStartPeriod">开始时段</label><select id="createLeaveStartPeriod"><option>上午</option><option>下午</option></select></div><div class="field"><label for="createLeaveEndDate">结束日期</label><input id="createLeaveEndDate" type="date" required></div><div class="field"><label for="createLeaveEndPeriod">结束时段</label><select id="createLeaveEndPeriod"><option>上午</option><option>下午</option></select></div><div class="field wide"><label for="createLeaveHandover">工作交接</label><input id="createLeaveHandover" maxlength="100"></div><div class="field wide"><label for="createLeaveReason">请假说明</label><textarea id="createLeaveReason" maxlength="1000" rows="4"></textarea></div><div class="hint">管理员代申请不限制日期，可选择过去、今天或未来；有效请假不能与该员工已有记录重叠。选择“已通过”会直接计入考勤，年假会同步扣减余额。</div></div><div id="leaveCreateError" class="error"></div><div class="actions"><button type="button" onclick="leaveCreateEditor.close()">取消</button><button class="save" type="submit">提交申请</button></div></form></dialog>
<script>
const currentEmail=__CURRENT_EMAIL_JSON__,primary=__PRIMARY_JSON__,secondary=__SECONDARY_JSON__,protectedEmails=__PROTECTED_EMAILS__;const labels={admin:'管理员',manager:'审批人',employee:'员工'},statusLabels={pending_primary:'待__PRIMARY_NAME__初审',pending_secondary:'__PRIMARY_NAME__初审通过 · 待__SECONDARY_NAME__终审',approved:'已通过',rejected:'已驳回'},decisionLabels={approved:'通过',rejected:'驳回'},esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),num=v=>v==null?'—':Number(v).toFixed(2);let currentUsers=[],currentRequests=[],editingId=null,editingLeaveId=null;
function approvalActions(x,index){const allowed=(x.status==='pending_primary'&&currentEmail===primary)||x.status==='pending_secondary';const stage=x.status==='pending_secondary'?'secondary':'primary';const label=x.status==='pending_secondary'&&currentEmail!==secondary?'代终审':'审批';const decisions=allowed?`<button class="approve" onclick="decide(${x.id},'approved','${stage}')">${label}通过</button> <button class="reject" onclick="decide(${x.id},'rejected','${stage}')">${label}驳回</button> `:'';return decisions+`<button class="edit" onclick="openLeaveEdit(${index})">编辑</button> <button class="delete" onclick="deleteRequest(${x.id})">删除</button>`}
function secondaryDecision(x){const decision=decisionLabels[x.secondary_decision]||'—';return x.secondary_decided_by?`${decision}<br><small>${esc(x.secondary_decided_by)}</small>`:decision}
async function load(){const r=await fetch('/api/admin/data');if(!r.ok){location='/';return}const d=await r.json();currentUsers=d.users;currentRequests=d.requests;createLeaveEmployee.innerHTML=d.users.map(x=>`<option value="${esc(x.linear_id)}">${esc(x.name)}（${esc(x.email)}）</option>`).join('');requests.innerHTML=d.requests.map((x,i)=>`<tr><td><b>${esc(x.name)}</b><br><small>${esc(x.email)}</small></td><td>${esc(x.leave_type)}</td><td>${esc(x.start_date)} ${esc(x.start_period||'上午')} 至 ${esc(x.end_date)} ${esc(x.end_period||'下午')}</td><td>${x.days}</td><td title="${esc(x.reason||'')}">${esc(x.reason||'—')}</td><td class="${x.status}">${statusLabels[x.status]||x.status}</td><td>${decisionLabels[x.primary_decision]||'—'}</td><td>${secondaryDecision(x)}</td><td>${approvalActions(x,i)}</td></tr>`).join('')||'<tr><td colspan="9">暂无申请</td></tr>';users.innerHTML=d.users.map((x,i)=>{const protectedUser=protectedEmails.includes(String(x.email).toLowerCase());return `<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.email)}</td><td>${esc(x.company||'—')}</td><td>${esc(x.work_start_date||'—')}</td><td>${esc(x.hire_date||'—')}</td><td>${num(x.service_years)}</td><td>${num(x.statutory_standard)}</td><td>${num(x.three_year_earned)}</td><td>${num(x.annual_leave_used)}</td><td><b>${num(x.annual_leave_available)}</b></td><td><select onchange="setRole('${encodeURIComponent(x.linear_id)}',this.value)" ${protectedUser?'disabled':''}>${Object.entries(labels).map(([v,l])=>`<option value="${v}" ${x.role===v?'selected':''}>${l}</option>`).join('')}</select></td><td><button class="edit" onclick="openEdit(${i})">编辑</button>${protectedUser?'':` <button class="delete" onclick="deleteUser(${i})">设为离职</button>`}</td></tr>`}).join('')||'<tr><td colspan="12">暂无员工</td></tr>'}
function resetEditor(){editForm.reset();editUsed.value='0';editEmail.readOnly=false;editError.textContent=''}function openCreate(){editingId=null;resetEditor();editorTitle.textContent='新增员工';editor.showModal()}function openEdit(index){const x=currentUsers[index];editingId=x.linear_id;resetEditor();editorTitle.textContent='编辑员工基本信息';editName.value=x.name||'';editEmail.value=x.email||'';editEmail.readOnly=protectedEmails.includes(String(x.email).toLowerCase());editCompany.value=x.company||'';editWorkStart.value=x.work_start_date||'';editHireDate.value=x.hire_date||'';editUsed.value=x.annual_leave_used??0;if(editEmail.readOnly)editError.textContent='固定管理员邮箱受系统保护，不能修改。';editor.showModal()}
editForm.addEventListener('submit',async e=>{e.preventDefault();editError.textContent='';const body={name:editName.value,email:editEmail.value,company:editCompany.value,work_start_date:editWorkStart.value,hire_date:editHireDate.value,annual_leave_used:Number(editUsed.value)};const url=editingId?'/api/admin/users/'+encodeURIComponent(editingId)+'/profile':'/api/admin/users';const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json().catch(()=>({}));if(!r.ok){editError.textContent=({invalid_name:'姓名不能为空',invalid_email:'邮箱格式不正确',email_exists:'该邮箱已存在',invalid_company:'公司名称过长',invalid_date:'日期格式不正确',invalid_annual_leave_used:'已休年假必须是 0 至 1000 的数字',protected_admin_email:'管理员邮箱受保护'}[data.error]||'保存失败，请重试');return}editor.close();await load()});
function openLeaveEdit(index){const x=currentRequests[index];editingLeaveId=x.id;leaveType.value=x.leave_type;leaveStatus.value=x.status;leaveStartDate.value=x.start_date;leaveStartPeriod.value=x.start_period||'上午';leaveEndDate.value=x.end_date;leaveEndPeriod.value=x.end_period||'下午';leaveHandover.value=x.handover||'';leaveReason.value=x.reason||'';leaveEditError.textContent='';leaveEditor.showModal()}
leaveEditForm.addEventListener('submit',async e=>{e.preventDefault();leaveEditError.textContent='';const body={leave_type:leaveType.value,status:leaveStatus.value,start_date:leaveStartDate.value,start_period:leaveStartPeriod.value,end_date:leaveEndDate.value,end_period:leaveEndPeriod.value,handover:leaveHandover.value,reason:leaveReason.value};const r=await fetch('/api/admin/requests/'+editingLeaveId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json().catch(()=>({}));if(!r.ok){leaveEditError.textContent=({invalid_leave_type:'请假类型不正确',invalid_status:'审批状态不正确',invalid_leave_period:'请检查开始、结束日期和时段',overlapping_leave:'修改后的时间与该员工其他请假重叠',remote_weekly_quota_exceeded:`本周 Remote 额度不足，最多还可申请 ${data.remaining??0} 天`,seniority_hire_date_required:'请先填写员工入职日期',seniority_leave_crosses_anniversary:`司龄假不能跨越入职周年日，本司龄年度截至 ${data.period_end||'—'}`,seniority_leave_quota_exceeded:`司龄假额度不足，最多还可申请 ${data.remaining??0} 天`,annual_leave_exceeded:`年假余额不足，仅剩 ${data.remaining??0} 天`}[data.error]||'保存失败，请重试');return}leaveEditor.close();await load()});
function localDate(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())}function openLeaveCreate(){leaveCreateForm.reset();createLeaveStatus.value='pending_primary';createLeaveStartDate.value=localDate();createLeaveEndDate.value=localDate();leaveCreateError.textContent='';leaveCreateEditor.showModal()}
leaveCreateForm.addEventListener('submit',async e=>{e.preventDefault();leaveCreateError.textContent='';const body={employee_id:createLeaveEmployee.value,leave_type:createLeaveType.value,status:createLeaveStatus.value,start_date:createLeaveStartDate.value,start_period:createLeaveStartPeriod.value,end_date:createLeaveEndDate.value,end_period:createLeaveEndPeriod.value,handover:createLeaveHandover.value,reason:createLeaveReason.value};const r=await fetch('/api/admin/requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json().catch(()=>({}));if(!r.ok){leaveCreateError.textContent=({user_not_found:'员工不存在或已离职',invalid_leave_type:'请假类型不正确',invalid_status:'审批状态不正确',invalid_leave_period:'请检查开始、结束日期和时段',overlapping_leave:'申请时间与该员工其他有效请假重叠',remote_weekly_quota_exceeded:`本周 Remote 额度不足，最多还可申请 ${data.remaining??0} 天`,seniority_hire_date_required:'请先填写员工入职日期',seniority_leave_crosses_anniversary:`司龄假不能跨越入职周年日，本司龄年度截至 ${data.period_end||'—'}`,seniority_leave_quota_exceeded:`司龄假额度不足，最多还可申请 ${data.remaining??0} 天`,annual_leave_exceeded:`年假余额不足，仅剩 ${data.remaining??0} 天`}[data.error]||'提交失败，请重试');return}leaveCreateEditor.close();await load()});
async function deleteRequest(id){if(!confirm('确定删除这条请假记录吗？删除后不可恢复，相关附件也会删除。'))return;const r=await fetch('/api/admin/requests/'+id,{method:'DELETE'});if(!r.ok){alert('删除失败，请重试');return}await load()}
async function deleteUser(index){const x=currentUsers[index];if(!confirm(`确定将员工“${x.name}”设为离职吗？员工将无法继续登录，但历史请假记录和附件会全部保留。`))return;const r=await fetch('/api/admin/users/'+encodeURIComponent(x.linear_id),{method:'DELETE'});const data=await r.json().catch(()=>({}));if(!r.ok){alert(({protected_admin:'固定管理员不能设为离职',cannot_delete_self:'不能停用当前登录账号'}[data.error]||'操作失败'));return}await load()}
function localMonth(){const parts=new Intl.DateTimeFormat('en',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit'}).formatToParts(new Date());return parts.find(x=>x.type==='year').value+'-'+parts.find(x=>x.type==='month').value}function exportAttendance(){if(!attendanceMonth.value){alert('请选择月份');return}location.href='/api/admin/attendance.xlsx?month='+encodeURIComponent(attendanceMonth.value)}attendanceMonth.value=localMonth();
async function decide(id,status,stage){const r=await fetch('/api/admin/requests/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,stage})});const data=await r.json().catch(()=>({}));if(!r.ok)alert(data.error==='annual_leave_exceeded'?`年假余额不足，仅剩 ${data.remaining} 天`:data.error==='approval_stage_changed'?'审批阶段已变化，请刷新后再操作':'当前账号不能处理此审批步骤');load()}async function setRole(encodedId,role){const r=await fetch('/api/admin/users/'+encodedId+'/role',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role})});if(!r.ok){const data=await r.json().catch(()=>({}));alert(data.error==='protected_admin'?'固定管理员不能降级':'角色修改失败')}load()}load();
</script></body></html>'''
        configured = all(os.environ.get(key) for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"))
        email_status = "" if configured else '<div class="warn">邮件通知已进入队列，但尚未配置 SMTP，暂时不会实际发送。</div>'
        replacements = {
            "__ADMIN_NAME__": html.escape(user["name"]),
            "__CURRENT_EMAIL_TEXT__": html.escape(user["email"].lower()),
            "__CURRENT_EMAIL_JSON__": json.dumps(user["email"].lower()),
            "__PRIMARY_JSON__": json.dumps(PRIMARY_APPROVER),
            "__SECONDARY_JSON__": json.dumps(SECONDARY_APPROVER),
            "__PRIMARY_NAME__": PRIMARY_APPROVER_NAME,
            "__SECONDARY_NAME__": SECONDARY_APPROVER_NAME,
            "__PROTECTED_EMAILS__": json.dumps(sorted(ADMIN_EMAILS)),
            "__EMAIL_STATUS__": email_status,
        }
        for marker, value in replacements.items():
            page = page.replace(marker, value)
        return page

    def read_json(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except Exception:
            return {}

    def same_origin(self):
        return self.headers.get("Origin") in (None, self.origin())

    def do_POST(self):
        if not self.same_origin():
            return self.json(403, {"error": "invalid_origin"})
        user = self.user()
        if not user:
            return self.json(401, {"error": "unauthorized"})
        url = urllib.parse.urlparse(self.path)
        body = self.read_json()
        if url.path == "/api/leave-requests":
            required = ["leave_type", "start_date", "end_date", "start_period", "end_period"]
            if any(body.get(key) in (None, "") for key in required):
                return self.json(400, {"error": "invalid_request"})
            if str(body.get("leave_type", "")).strip() not in LEAVE_TYPES:
                return self.json(400, {"error": "invalid_leave_type"})
            attachment_bytes = None
            attachment_name = None
            attachment_type = None
            raw_attachment = body.get("attachment")
            if raw_attachment:
                try:
                    attachment_name = Path(str(raw_attachment.get("name", ""))).name[:200]
                    suffix = Path(attachment_name).suffix.lower()
                    if suffix not in ALLOWED_ATTACHMENT_EXTENSIONS:
                        return self.json(400, {"error": "invalid_attachment_type"})
                    attachment_bytes = base64.b64decode(str(raw_attachment.get("data", "")), validate=True)
                    if not attachment_bytes or len(attachment_bytes) > MAX_ATTACHMENT_SIZE:
                        return self.json(400, {"error": "invalid_attachment_size"})
                    attachment_type = str(raw_attachment.get("type") or "application/octet-stream")[:100]
                except (AttributeError, ValueError, TypeError):
                    return self.json(400, {"error": "invalid_attachment"})
            try:
                start_date = date.fromisoformat(str(body["start_date"])[:10])
                end_date = date.fromisoformat(str(body["end_date"])[:10])
                start_period = str(body["start_period"])
                end_period = str(body["end_period"])
                if start_period not in ("上午", "下午") or end_period not in ("上午", "下午"):
                    raise ValueError("invalid period")
                start_slot = 0 if start_period == "上午" else 1
                end_slot = 0 if end_period == "上午" else 1
                slot_count = (end_date - start_date).days * 2 + end_slot - start_slot + 1
                leave_days = slot_count / 2
            except (TypeError, ValueError):
                return self.json(400, {"error": "invalid_request"})
            if start_date < datetime.now(ZoneInfo("Asia/Shanghai")).date():
                return self.json(400, {"error": "past_leave_date"})
            if leave_days < 0.5:
                return self.json(400, {"error": "invalid_leave_period"})
            with db() as connection:
                requested_start = start_date.toordinal() * 2 + start_slot
                requested_end = end_date.toordinal() * 2 + end_slot
                active_requests = connection.execute(
                    "SELECT id,start_date,end_date,start_period,end_period,status FROM leave_requests WHERE user_id=? AND status IN ('pending_primary','pending_secondary','approved')",
                    (user["id"],),
                ).fetchall()
                for existing in active_requests:
                    existing_start = date.fromisoformat(existing["start_date"]).toordinal() * 2 + (0 if existing["start_period"] == "上午" else 1)
                    existing_end = date.fromisoformat(existing["end_date"]).toordinal() * 2 + (0 if existing["end_period"] == "上午" else 1)
                    if requested_start <= existing_end and requested_end >= existing_start:
                        return self.json(409, {"error": "overlapping_leave", "conflict_id": existing["id"], "conflict_status": existing["status"]})
                if str(body["leave_type"]).strip() == "Remote":
                    quota_error = remote_quota_error(connection, user["id"], start_date, start_period, end_date, end_period)
                    if quota_error:
                        return self.json(400, quota_error)
                if str(body["leave_type"]).strip() == "司龄假":
                    quota_error = seniority_quota_error(connection, user, start_date, end_date, leave_days)
                    if quota_error:
                        return self.json(400, quota_error)
                if str(body["leave_type"]) == "年假":
                    pending = connection.execute("SELECT COALESCE(SUM(days),0) FROM leave_requests WHERE user_id=? AND leave_type='年假' AND status IN ('pending_primary','pending_secondary')", (user["id"],)).fetchone()[0]
                    remaining = max(0, float(user.get("annual_leave_available") or 0) - pending)
                    if leave_days > remaining + 1e-9:
                        return self.json(400, {"error": "annual_leave_exceeded", "remaining": round(remaining, 2)})
                cursor = connection.execute("INSERT INTO leave_requests(user_id,leave_type,start_date,end_date,start_period,end_period,days,handover,reason,status) VALUES(?,?,?,?,?,?,?,?,?,'pending_primary')", (user["id"], str(body["leave_type"])[:30], start_date.isoformat(), end_date.isoformat(), start_period, end_period, leave_days, str(body.get("handover", ""))[:100], str(body.get("reason", ""))[:1000]))
                request_id = cursor.lastrowid
                if attachment_bytes is not None:
                    stored_name = f"{request_id}_{secrets.token_hex(8)}{Path(attachment_name).suffix.lower()}"
                    try:
                        (ATTACHMENT_DIR / stored_name).write_bytes(attachment_bytes)
                    except OSError:
                        connection.execute("DELETE FROM leave_requests WHERE id=?", (request_id,))
                        return self.json(500, {"error": "attachment_save_failed"})
                    connection.execute(
                        "UPDATE leave_requests SET attachment_name=?,attachment_path=?,attachment_type=?,attachment_size=? WHERE id=?",
                        (attachment_name, stored_name, attachment_type, len(attachment_bytes), request_id),
                    )
            notify_approver(request_id, "pending_primary", user["name"], start_date, end_date)
            queue_email(user["email"], "请假申请已提交", f"{user['name']}，你的{body['leave_type']}申请（{start_date.isoformat()} {start_period} 至 {end_date.isoformat()} {end_period}，共 {leave_days:g} 天）已提交，当前等待 {PRIMARY_APPROVER_NAME} 初审。")
            return self.json(201, {"id": request_id, "status": "pending_primary"})
        if user.get("role") != "admin":
            return self.json(403, {"error": "admin_required"})
        parts = url.path.strip("/").split("/")
        if url.path == "/api/admin/requests":
            employee_id = str(body.get("employee_id", "")).strip()
            leave_type = str(body.get("leave_type", "")).strip()
            status = str(body.get("status", "pending_primary")).strip()
            if leave_type not in LEAVE_TYPES:
                return self.json(400, {"error": "invalid_leave_type"})
            if status not in ("pending_primary", "pending_secondary", "approved", "rejected"):
                return self.json(400, {"error": "invalid_status"})
            try:
                start_date = date.fromisoformat(str(body.get("start_date", "")))
                end_date = date.fromisoformat(str(body.get("end_date", "")))
                start_period = str(body.get("start_period", ""))
                end_period = str(body.get("end_period", ""))
                if start_period not in ("上午", "下午") or end_period not in ("上午", "下午"):
                    raise ValueError
                start_slot = start_date.toordinal() * 2 + (0 if start_period == "上午" else 1)
                end_slot = end_date.toordinal() * 2 + (0 if end_period == "上午" else 1)
                if end_slot < start_slot:
                    raise ValueError
                leave_days = (end_slot - start_slot + 1) / 2
            except (TypeError, ValueError):
                return self.json(400, {"error": "invalid_leave_period"})
            reason = str(body.get("reason", "")).strip()[:1000]
            handover = str(body.get("handover", "")).strip()[:100]
            active_statuses = ("pending_primary", "pending_secondary", "approved")
            with db() as connection:
                target_user_row = connection.execute(
                    "SELECT * FROM users WHERE linear_id=? AND is_active=1", (employee_id,)
                ).fetchone()
                if not target_user_row:
                    return self.json(404, {"error": "user_not_found"})
                target_user = dict(target_user_row)
                if status in active_statuses:
                    conflicts = connection.execute(
                        "SELECT id,start_date,end_date,start_period,end_period FROM leave_requests WHERE user_id=? AND status IN ('pending_primary','pending_secondary','approved')",
                        (employee_id,),
                    ).fetchall()
                    for conflict in conflicts:
                        conflict_start = date.fromisoformat(conflict["start_date"]).toordinal() * 2 + (0 if conflict["start_period"] == "上午" else 1)
                        conflict_end = date.fromisoformat(conflict["end_date"]).toordinal() * 2 + (0 if conflict["end_period"] == "上午" else 1)
                        if start_slot <= conflict_end and end_slot >= conflict_start:
                            return self.json(409, {"error": "overlapping_leave", "conflict_id": conflict["id"]})
                    if leave_type == "Remote":
                        quota_error = remote_quota_error(connection, employee_id, start_date, start_period, end_date, end_period)
                        if quota_error:
                            return self.json(400, quota_error)
                    if leave_type == "司龄假":
                        quota_error = seniority_quota_error(connection, target_user, start_date, end_date, leave_days)
                        if quota_error:
                            return self.json(400, quota_error)
                    if leave_type == "年假":
                        remaining = float(leave_metrics(target_user).get("annual_leave_available") or 0)
                        if status != "approved":
                            pending = connection.execute(
                                "SELECT COALESCE(SUM(days),0) FROM leave_requests WHERE user_id=? AND leave_type='年假' AND status IN ('pending_primary','pending_secondary')",
                                (employee_id,),
                            ).fetchone()[0]
                            remaining = max(0, remaining - float(pending or 0))
                        if leave_days > remaining + 1e-9:
                            return self.json(400, {"error": "annual_leave_exceeded", "remaining": round(remaining, 2)})
                primary_decision = secondary_decision = primary_decided_at = secondary_decided_at = None
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                if status == "pending_secondary":
                    primary_decision = "approved"
                    primary_decided_at = now
                elif status == "approved":
                    primary_decision = secondary_decision = "approved"
                    primary_decided_at = secondary_decided_at = now
                elif status == "rejected":
                    primary_decision = "rejected"
                    primary_decided_at = now
                cursor = connection.execute(
                    "INSERT INTO leave_requests(user_id,leave_type,start_date,end_date,start_period,end_period,days,handover,reason,status,primary_decision,primary_decided_at,secondary_decision,secondary_decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (employee_id, leave_type, start_date.isoformat(), end_date.isoformat(), start_period, end_period, leave_days, handover, reason, status, primary_decision, primary_decided_at, secondary_decision, secondary_decided_at),
                )
                request_id = cursor.lastrowid
                if leave_type == "年假" and status == "approved":
                    connection.execute(
                        "UPDATE users SET annual_leave_used=COALESCE(annual_leave_used,0)+? WHERE linear_id=?",
                        (leave_days, employee_id),
                    )
                created = dict(connection.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone())
            notify_approver(request_id, status, target_user["name"], start_date, end_date)
            status_text = {
                "pending_primary": f"待 {PRIMARY_APPROVER_NAME} 初审",
                "pending_secondary": f"待 {SECONDARY_APPROVER_NAME} 终审",
                "approved": "终审已通过",
                "rejected": "已驳回",
            }[status]
            queue_email(
                target_user["email"],
                "管理员已代你提交请假申请",
                f"{target_user['name']}，管理员已代你提交{leave_type}申请（{start_date.isoformat()} {start_period} 至 {end_date.isoformat()} {end_period}，共 {leave_days:g} 天），当前状态：{status_text}。",
            )
            return self.json(201, {"request": created})
        if url.path == "/api/admin/users":
            name = str(body.get("name", "")).strip()
            email = str(body.get("email", "")).strip().lower()
            company = str(body.get("company", "")).strip()
            if not name or len(name) > 100:
                return self.json(400, {"error": "invalid_name"})
            if len(email) > 254 or email.count("@") != 1 or any(character.isspace() for character in email):
                return self.json(400, {"error": "invalid_email"})
            local_part, domain = email.split("@", 1)
            if not local_part or not domain or "." not in domain:
                return self.json(400, {"error": "invalid_email"})
            if len(company) > 100:
                return self.json(400, {"error": "invalid_company"})
            dates = {}
            for field in ("work_start_date", "hire_date"):
                value = str(body.get(field, "")).strip()
                if value:
                    try:
                        if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                            raise ValueError
                    except ValueError:
                        return self.json(400, {"error": "invalid_date", "field": field})
                    dates[field] = value
                else:
                    dates[field] = None
            try:
                if isinstance(body.get("annual_leave_used"), bool):
                    raise ValueError
                used = float(body.get("annual_leave_used", 0))
                if used != used or used < 0 or used > 1000:
                    raise ValueError
            except (TypeError, ValueError):
                return self.json(400, {"error": "invalid_annual_leave_used"})
            with db() as connection:
                if connection.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
                    return self.json(409, {"error": "email_exists"})
                linear_id = f"pending:{email}"
                connection.execute(
                    "INSERT INTO users(linear_id,name,email,company,work_start_date,hire_date,annual_leave_used,notes,profile_locked,role) VALUES(?,?,?,?,?,?,?,'',1,'employee')",
                    (linear_id, name, email, company, dates["work_start_date"], dates["hire_date"], used),
                )
                created = dict(connection.execute("SELECT * FROM users WHERE linear_id=?", (linear_id,)).fetchone())
            return self.json(201, {"user": {**created, **leave_metrics(created)}})
        if len(parts) == 5 and parts[:3] == ["api", "admin", "requests"] and parts[4] == "status":
            decision = body.get("status")
            if decision not in ("approved", "rejected"):
                return self.json(400, {"error": "invalid_status"})
            expected_stage = body.get("stage")
            if expected_stage not in ("primary", "secondary"):
                return self.json(400, {"error": "invalid_approval_stage"})
            notification = None
            with db() as connection:
                request_id = int(parts[3])
                # Serialize the stage read and update before sending a notice.
                connection.execute("BEGIN IMMEDIATE")
                leave = connection.execute("SELECT r.*,u.name,u.email FROM leave_requests r JOIN users u ON u.linear_id=r.user_id WHERE r.id=?", (request_id,)).fetchone()
                if not leave:
                    return self.json(404, {"error": "request_not_found"})
                actor = user.get("email", "").lower()
                if leave["status"] == "pending_primary":
                    if expected_stage != "primary":
                        return self.json(409, {"error": "approval_stage_changed"})
                    if actor != PRIMARY_APPROVER:
                        return self.json(403, {"error": "waiting_for_primary_approver"})
                    next_status = "pending_secondary" if decision == "approved" else "rejected"
                    connection.execute("UPDATE leave_requests SET status=?,primary_decision=?,primary_decided_at=CURRENT_TIMESTAMP WHERE id=?", (next_status, decision, request_id))
                    status_text = f"{PRIMARY_APPROVER_NAME} 初审已通过，正在等待 {SECONDARY_APPROVER_NAME} 终审" if decision == "approved" else f"{PRIMARY_APPROVER_NAME} 初审已驳回"
                    notification = (leave["email"], "请假申请初审状态更新", f"{leave['name']}，你的{leave['leave_type']}申请（{leave['start_date']} 至 {leave['end_date']}）{status_text}。")
                elif leave["status"] == "pending_secondary":
                    if expected_stage != "secondary":
                        return self.json(409, {"error": "approval_stage_changed"})
                    if actor != SECONDARY_APPROVER and user.get("role") != "admin":
                        return self.json(403, {"error": "waiting_for_secondary_approver"})
                    if decision == "approved" and leave["leave_type"] == "年假":
                        target_user = dict(connection.execute("SELECT * FROM users WHERE linear_id=?", (leave["user_id"],)).fetchone())
                        remaining = float(leave_metrics(target_user).get("annual_leave_available") or 0)
                        if leave["days"] > remaining + 1e-9:
                            return self.json(400, {"error": "annual_leave_exceeded", "remaining": round(remaining, 2)})
                    next_status = "approved" if decision == "approved" else "rejected"
                    connection.execute("UPDATE leave_requests SET status=?,secondary_decision=?,secondary_decided_at=CURRENT_TIMESTAMP,secondary_decided_by=? WHERE id=?", (next_status, decision, actor, request_id))
                    if decision == "approved" and leave["leave_type"] == "年假":
                        connection.execute("UPDATE users SET annual_leave_used=COALESCE(annual_leave_used,0)+? WHERE linear_id=?", (leave["days"], leave["user_id"]))
                    approver_text = SECONDARY_APPROVER_NAME if actor == SECONDARY_APPROVER else f"管理员 {user.get('name') or actor} 代终审"
                    status_text = f"{approver_text}已通过" if decision == "approved" else f"{approver_text}已驳回"
                    notification = (leave["email"], "请假申请终审状态更新", f"{leave['name']}，你的{leave['leave_type']}申请（{leave['start_date']} 至 {leave['end_date']}）{status_text}。")
                else:
                    return self.json(400, {"error": "already_processed"})
            notify_approver(request_id, next_status, leave["name"], leave["start_date"], leave["end_date"])
            if notification:
                queue_email(*notification)
            return self.json(200, {"ok": True})
        if len(parts) == 4 and parts[:3] == ["api", "admin", "requests"]:
            try:
                request_id = int(parts[3])
            except ValueError:
                return self.json(404, {"error": "request_not_found"})
            leave_type = str(body.get("leave_type", "")).strip()
            status = str(body.get("status", "")).strip()
            if leave_type not in LEAVE_TYPES:
                return self.json(400, {"error": "invalid_leave_type"})
            if status not in ("pending_primary", "pending_secondary", "approved", "rejected"):
                return self.json(400, {"error": "invalid_status"})
            try:
                start_date = date.fromisoformat(str(body.get("start_date", "")))
                end_date = date.fromisoformat(str(body.get("end_date", "")))
                start_period = str(body.get("start_period", ""))
                end_period = str(body.get("end_period", ""))
                if start_period not in ("上午", "下午") or end_period not in ("上午", "下午"):
                    raise ValueError
                start_slot = start_date.toordinal() * 2 + (0 if start_period == "上午" else 1)
                end_slot = end_date.toordinal() * 2 + (0 if end_period == "上午" else 1)
                if end_slot < start_slot:
                    raise ValueError
                leave_days = (end_slot - start_slot + 1) / 2
            except (TypeError, ValueError):
                return self.json(400, {"error": "invalid_leave_period"})
            reason = str(body.get("reason", "")).strip()[:1000]
            handover = str(body.get("handover", "")).strip()[:100]
            active_statuses = ("pending_primary", "pending_secondary", "approved")
            with db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone()
                if not existing:
                    return self.json(404, {"error": "request_not_found"})
                if status in active_statuses:
                    conflicts = connection.execute(
                        "SELECT id,start_date,end_date,start_period,end_period FROM leave_requests WHERE user_id=? AND id<>? AND status IN ('pending_primary','pending_secondary','approved')",
                        (existing["user_id"], request_id),
                    ).fetchall()
                    for conflict in conflicts:
                        conflict_start = date.fromisoformat(conflict["start_date"]).toordinal() * 2 + (0 if conflict["start_period"] == "上午" else 1)
                        conflict_end = date.fromisoformat(conflict["end_date"]).toordinal() * 2 + (0 if conflict["end_period"] == "上午" else 1)
                        if start_slot <= conflict_end and end_slot >= conflict_start:
                            return self.json(409, {"error": "overlapping_leave", "conflict_id": conflict["id"]})
                    if leave_type == "Remote":
                        quota_error = remote_quota_error(connection, existing["user_id"], start_date, start_period, end_date, end_period, request_id)
                        if quota_error:
                            return self.json(400, quota_error)
                old_annual = float(existing["days"]) if existing["leave_type"] == "年假" and existing["status"] == "approved" else 0.0
                new_annual = leave_days if leave_type == "年假" and status == "approved" else 0.0
                target_user = dict(connection.execute("SELECT * FROM users WHERE linear_id=?", (existing["user_id"],)).fetchone())
                if leave_type == "司龄假" and status in active_statuses:
                    quota_error = seniority_quota_error(connection, target_user, start_date, end_date, leave_days, request_id)
                    if quota_error:
                        return self.json(400, quota_error)
                if new_annual:
                    remaining = float(leave_metrics(target_user).get("annual_leave_available") or 0) + old_annual
                    if new_annual > remaining + 1e-9:
                        return self.json(400, {"error": "annual_leave_exceeded", "remaining": round(remaining, 2)})
                primary_decision = secondary_decision = primary_decided_at = secondary_decided_at = None
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                if status == "pending_secondary":
                    primary_decision = "approved"
                    primary_decided_at = existing["primary_decided_at"] or now
                elif status == "approved":
                    primary_decision = secondary_decision = "approved"
                    primary_decided_at = existing["primary_decided_at"] or now
                    secondary_decided_at = existing["secondary_decided_at"] or now
                elif status == "rejected":
                    if existing["status"] == "pending_secondary" or existing["secondary_decision"]:
                        primary_decision, secondary_decision = "approved", "rejected"
                        primary_decided_at = existing["primary_decided_at"] or now
                        secondary_decided_at = now
                    else:
                        primary_decision = "rejected"
                        primary_decided_at = now
                connection.execute(
                    "UPDATE leave_requests SET leave_type=?,start_date=?,end_date=?,start_period=?,end_period=?,days=?,handover=?,reason=?,status=?,primary_decision=?,primary_decided_at=?,secondary_decision=?,secondary_decided_at=? WHERE id=?",
                    (leave_type, start_date.isoformat(), end_date.isoformat(), start_period, end_period, leave_days, handover, reason, status, primary_decision, primary_decided_at, secondary_decision, secondary_decided_at, request_id),
                )
                annual_delta = new_annual - old_annual
                if abs(annual_delta) > 1e-9:
                    connection.execute("UPDATE users SET annual_leave_used=MAX(0,COALESCE(annual_leave_used,0)+?) WHERE linear_id=?", (annual_delta, existing["user_id"]))
                updated = dict(connection.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone())
            if status != existing["status"]:
                notify_approver(request_id, status, target_user["name"], start_date, end_date)
                status_text = {"pending_primary": f"待 {PRIMARY_APPROVER_NAME} 初审", "pending_secondary": f"待 {SECONDARY_APPROVER_NAME} 终审", "approved": "终审已通过", "rejected": "已驳回"}[status]
                queue_email(
                    target_user["email"],
                    "请假申请状态已由管理员更新",
                    f"{target_user['name']}，你的{leave_type}申请（{start_date.isoformat()} {start_period} 至 {end_date.isoformat()} {end_period}）状态已更新为：{status_text}。",
                )
            return self.json(200, {"request": updated})
        if len(parts) == 5 and parts[:3] == ["api", "admin", "users"] and parts[4] == "profile":
            target = urllib.parse.unquote(parts[3])
            name = str(body.get("name", "")).strip()
            email = str(body.get("email", "")).strip().lower()
            company = str(body.get("company", "")).strip()
            if not name or len(name) > 100:
                return self.json(400, {"error": "invalid_name"})
            if len(email) > 254 or email.count("@") != 1 or any(character.isspace() for character in email):
                return self.json(400, {"error": "invalid_email"})
            local_part, domain = email.split("@", 1)
            if not local_part or not domain or "." not in domain:
                return self.json(400, {"error": "invalid_email"})
            if len(company) > 100:
                return self.json(400, {"error": "invalid_company"})
            dates = {}
            for field in ("work_start_date", "hire_date"):
                value = str(body.get(field, "")).strip()
                if value:
                    try:
                        if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                            raise ValueError
                    except ValueError:
                        return self.json(400, {"error": "invalid_date", "field": field})
                    dates[field] = value
                else:
                    dates[field] = None
            try:
                if isinstance(body.get("annual_leave_used"), bool):
                    raise ValueError
                used = float(body.get("annual_leave_used"))
                if used != used or used < 0 or used > 1000:
                    raise ValueError
            except (TypeError, ValueError):
                return self.json(400, {"error": "invalid_annual_leave_used"})
            with db() as connection:
                target_user = connection.execute("SELECT * FROM users WHERE linear_id=?", (target,)).fetchone()
                if not target_user:
                    return self.json(404, {"error": "user_not_found"})
                if target_user["email"].lower() in ADMIN_EMAILS and email != target_user["email"].lower():
                    return self.json(400, {"error": "protected_admin_email"})
                duplicate = connection.execute("SELECT linear_id FROM users WHERE lower(email)=? AND linear_id<>?", (email, target)).fetchone()
                if duplicate:
                    return self.json(409, {"error": "email_exists"})
                connection.execute(
                    "UPDATE users SET name=?,email=?,company=?,work_start_date=?,hire_date=?,annual_leave_used=?,profile_locked=1 WHERE linear_id=?",
                    (name, email, company, dates["work_start_date"], dates["hire_date"], used, target),
                )
                updated = dict(connection.execute("SELECT * FROM users WHERE linear_id=?", (target,)).fetchone())
            return self.json(200, {"user": {**updated, **leave_metrics(updated)}})
        if len(parts) == 5 and parts[:3] == ["api", "admin", "users"] and parts[4] == "role":
            role = body.get("role")
            if role not in ("admin", "manager", "employee"):
                return self.json(400, {"error": "invalid_role"})
            target = urllib.parse.unquote(parts[3])
            with db() as connection:
                target_user = connection.execute("SELECT email FROM users WHERE linear_id=?", (target,)).fetchone()
                if not target_user:
                    return self.json(404, {"error": "user_not_found"})
                allowed_admin = target_user["email"].lower() in ADMIN_EMAILS
                if role == "admin" and not allowed_admin:
                    return self.json(400, {"error": "admin_email_not_allowed"})
                if allowed_admin and role != "admin":
                    return self.json(400, {"error": "protected_admin"})
                connection.execute("UPDATE users SET role=? WHERE linear_id=?", (role, target))
            return self.json(200, {"ok": True})
        return self.json(404, {"error": "not_found"})

    def do_DELETE(self):
        if not self.same_origin():
            return self.json(403, {"error": "invalid_origin"})
        user = self.user()
        if not user:
            return self.json(401, {"error": "unauthorized"})
        if user.get("role") != "admin":
            return self.json(403, {"error": "admin_required"})
        url = urllib.parse.urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if len(parts) == 4 and parts[:3] == ["api", "admin", "requests"]:
            try:
                request_id = int(parts[3])
            except ValueError:
                return self.json(404, {"error": "request_not_found"})
            with db() as connection:
                leave = connection.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone()
                if not leave:
                    return self.json(404, {"error": "request_not_found"})
                if leave["status"] == "approved" and leave["leave_type"] == "年假":
                    connection.execute("UPDATE users SET annual_leave_used=MAX(0,COALESCE(annual_leave_used,0)-?) WHERE linear_id=?", (leave["days"], leave["user_id"]))
                stored_name = leave["attachment_path"]
                connection.execute("DELETE FROM leave_requests WHERE id=?", (request_id,))
            attachment_removed = False
            if stored_name:
                attachment_path = (ATTACHMENT_DIR / stored_name).resolve()
                if attachment_path.parent == ATTACHMENT_DIR.resolve() and attachment_path.is_file():
                    try:
                        attachment_path.unlink()
                        attachment_removed = True
                    except OSError:
                        pass
            return self.json(200, {"ok": True, "attachment_removed": attachment_removed})
        if len(parts) != 4 or parts[:3] != ["api", "admin", "users"]:
            return self.json(404, {"error": "not_found"})
        target = urllib.parse.unquote(parts[3])
        if target == user.get("id"):
            return self.json(400, {"error": "cannot_delete_self"})
        with db() as connection:
            target_user = connection.execute("SELECT linear_id,email FROM users WHERE linear_id=?", (target,)).fetchone()
            if not target_user:
                return self.json(404, {"error": "user_not_found"})
            if target_user["email"].lower() in ADMIN_EMAILS:
                return self.json(400, {"error": "protected_admin"})
            retained_requests = connection.execute("SELECT COUNT(*) FROM leave_requests WHERE user_id=?", (target,)).fetchone()[0]
            connection.execute("UPDATE users SET is_active=0,role='employee' WHERE linear_id=?", (target,))
        return self.json(200, {"ok": True, "employee_deactivated": True, "retained_requests": retained_requests})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            return self.json(200, {"ok": True, "oauthConfigured": bool(CLIENT_ID)})
        if url.path == "/auth/linear":
            if not CLIENT_ID:
                return self.redirect("/?error=not_configured")
            state = secrets.token_urlsafe(24)
            verifier = secrets.token_urlsafe(48)
            challenge = b64(hashlib.sha256(verifier.encode()).digest())
            callback = f"{self.origin()}/auth/linear/callback"
            params = urllib.parse.urlencode({"client_id": CLIENT_ID, "redirect_uri": callback, "response_type": "code", "scope": "read", "actor": "user", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
            return self.redirect(f"https://linear.app/oauth/authorize?{params}", [("linear_oauth_state", state, 600, "/auth/linear"), ("linear_oauth_verifier", verifier, 600, "/auth/linear")])
        if url.path == "/auth/linear/callback":
            jar = self.cookies()
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            if not code or not state or state != jar.get("linear_oauth_state") or not jar.get("linear_oauth_verifier"):
                return self.redirect("/?error=invalid_state")
            try:
                callback = f"{self.origin()}/auth/linear/callback"
                body = urllib.parse.urlencode({"code": code, "redirect_uri": callback, "client_id": CLIENT_ID, "code_verifier": jar["linear_oauth_verifier"], "grant_type": "authorization_code"}).encode()
                request = urllib.request.Request("https://api.linear.app/oauth/token", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
                token = json.load(urllib.request.urlopen(request, timeout=15))["access_token"]
                gql = json.dumps({"query": "query Viewer { viewer { id name email } }"}).encode()
                request = urllib.request.Request("https://api.linear.app/graphql", data=gql, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
                user = json.load(urllib.request.urlopen(request, timeout=15))["data"]["viewer"]
                return self.redirect("/", [("linear_session", make_session(user), 604800, "/"), ("linear_oauth_state", "", 0, "/auth/linear"), ("linear_oauth_verifier", "", 0, "/auth/linear")])
            except Exception as exc:
                print("Linear OAuth callback failed:", repr(exc), flush=True)
                return self.redirect("/?error=oauth_failed")
        if url.path == "/auth/status":
            user = self.user()
            return self.json(200, {"configured": bool(CLIENT_ID), "connected": bool(user), "user": {key: user.get(key) for key in ("id", "name", "email", "role")} if user else None})
        if url.path == "/auth/logout":
            return self.redirect("/", [("linear_session", "", 0, "/")])
        user = self.user()
        if url.path.startswith("/api/attachments/"):
            if not user:
                return self.json(401, {"error": "unauthorized"})
            try:
                request_id = int(url.path.rsplit("/", 1)[1])
            except ValueError:
                return self.json(404, {"error": "attachment_not_found"})
            with db() as connection:
                row = connection.execute("SELECT user_id,attachment_name,attachment_path,attachment_type FROM leave_requests WHERE id=?", (request_id,)).fetchone()
            if not row or not row["attachment_path"]:
                return self.json(404, {"error": "attachment_not_found"})
            if user["id"] != row["user_id"] and user.get("role") not in ("admin", "manager"):
                return self.json(403, {"error": "forbidden"})
            attachment_path = (ATTACHMENT_DIR / row["attachment_path"]).resolve()
            if attachment_path.parent != ATTACHMENT_DIR.resolve() or not attachment_path.is_file():
                return self.json(404, {"error": "attachment_not_found"})
            payload = attachment_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", row["attachment_type"] or "application/octet-stream")
            encoded_name = urllib.parse.quote(row["attachment_name"] or "attachment")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            return self.wfile.write(payload)
        if url.path == "/api/leave-requests":
            if not user:
                return self.json(401, {"error": "unauthorized"})
            with db() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM leave_requests WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()]
            return self.json(200, {"requests": rows})
        if url.path == "/api/team-leave":
            if not user:
                return self.json(401, {"error": "unauthorized"})
            with db() as connection:
                rows = [dict(row) for row in connection.execute("SELECT r.id,r.leave_type,r.start_date,r.end_date,r.start_period,r.end_period,r.days,u.name FROM leave_requests r JOIN users u ON u.linear_id=r.user_id WHERE r.status='approved' ORDER BY r.start_date").fetchall()]
            return self.json(200, {"requests": rows})
        if url.path == "/api/leave-history":
            if not user or user.get("role") not in ("admin", "manager"):
                return self.json(403, {"error": "approver_required"})
            with db() as connection:
                rows = [dict(row) for row in connection.execute("SELECT r.id,r.leave_type,r.start_date,r.end_date,r.start_period,r.end_period,r.days,r.reason,r.status,r.primary_decision,r.primary_decided_at,r.secondary_decision,r.secondary_decided_at,r.secondary_decided_by,r.attachment_name,r.attachment_type,r.attachment_size,u.name,u.email FROM leave_requests r JOIN users u ON u.linear_id=r.user_id ORDER BY r.id DESC").fetchall()]
            return self.json(200, {"requests": rows})
        if url.path == "/api/admin/attendance.xlsx":
            if not user or user.get("role") != "admin":
                return self.json(403, {"error": "admin_required"})
            month_value = query.get("month", [datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")])[0]
            try:
                month_start = datetime.strptime(month_value, "%Y-%m").date().replace(day=1)
                if month_start.strftime("%Y-%m") != month_value:
                    raise ValueError("invalid month")
            except ValueError:
                return self.json(400, {"error": "invalid_month"})
            next_month = date(month_start.year + (1 if month_start.month == 12 else 0), 1 if month_start.month == 12 else month_start.month + 1, 1)
            month_end = next_month - timedelta(days=1)
            with db() as connection:
                users = [dict(row) for row in connection.execute("SELECT * FROM users ORDER BY name,email").fetchall()]
                requests = [dict(row) for row in connection.execute(
                    "SELECT r.*,u.name,u.email,u.company FROM leave_requests r JOIN users u ON u.linear_id=r.user_id WHERE r.status='approved' AND r.start_date<=? AND r.end_date>=? ORDER BY r.start_date,r.id",
                    (month_end.isoformat(), month_start.isoformat()),
                ).fetchall()]
            payload = build_attendance_workbook(users, requests, month_start, month_end)
            filename = urllib.parse.quote(f"考勤记录-{month_value}.xlsx")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            return self.wfile.write(payload)
        if url.path == "/api/admin/data":
            if not user or user.get("role") != "admin":
                return self.json(403, {"error": "admin_required"})
            with db() as connection:
                users = []
                for row in connection.execute("SELECT * FROM users WHERE is_active=1 ORDER BY created_at").fetchall():
                    item = dict(row)
                    users.append({**item, **leave_metrics(item), **seniority_leave_metrics(connection, item)})
                requests = [dict(row) for row in connection.execute("SELECT r.*,u.name,u.email FROM leave_requests r JOIN users u ON u.linear_id=r.user_id ORDER BY r.id DESC").fetchall()]
                actor = user.get("email", "").lower()
                for item in requests:
                    item["can_approve"] = (item["status"] == "pending_primary" and actor == PRIMARY_APPROVER) or (item["status"] == "pending_secondary" and actor == SECONDARY_APPROVER)
            return self.json(200, {"users": users, "requests": requests})
        if url.path == "/admin":
            if not user or user.get("role") != "admin":
                return self.redirect("/")
            page = self.admin_page_v4(user).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            return self.wfile.write(page)
        if url.path != "/":
            self.send_error(404)
            return
        if not user:
            errors = {"not_configured": "Linear OAuth 尚未配置。", "invalid_state": "登录请求已失效，请重试。", "oauth_failed": "Linear 登录失败，请重试或联系管理员。"}
            page = self.login_page(errors.get(query.get("error", [""])[0], "")).encode()
        else:
            name = html.escape(user.get("name") or "Linear 用户")
            admin_link = '<a class="linear" href="/admin" style="text-decoration:none;color:inherit">管理后台</a>' if user.get("role") == "admin" else ""
            available = user.get("annual_leave_available")
            used = user.get("annual_leave_used")
            standard = user.get("statutory_standard")
            seniority_available = user.get("seniority_leave_available")
            seniority_used = user.get("seniority_leave_used")
            seniority_quota = user.get("seniority_leave_quota")
            seniority_next_release = user.get("seniority_leave_next_release")
            page = INDEX_HTML.replace("{{USER_NAME}}", name).replace("{{USER_INITIAL}}", name[:1]).replace("{{ADMIN_LINK}}", admin_link).replace("{{ANNUAL_BALANCE}}", "—" if available is None else f"{available:.2f}").replace("{{LEAVE_USED}}", "—" if used is None else f"{float(used):.2f}").replace("{{ANNUAL_STANDARD}}", "—" if standard is None else str(standard)).replace("{{SENIORITY_BALANCE}}", "—" if seniority_available is None else f"{seniority_available:.2f}").replace("{{SENIORITY_USED}}", "—" if seniority_used is None else f"{seniority_used:.2f}").replace("{{SENIORITY_QUOTA}}", "—" if seniority_quota is None else str(seniority_quota)).replace("{{SENIORITY_NEXT_RELEASE}}", seniority_next_release or "—").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://linear.app")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
