import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import server


BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8080")
TEST_EMAIL = "employee-test@example.com"


def request(path, method="GET", body=None, cookie=""):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Cookie": f"linear_session={urllib.parse.quote(cookie)}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    response = urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, method=method, headers=headers), timeout=10)
    payload = response.read()
    return response.status, payload


with server.db() as connection:
    admin = connection.execute(
        "SELECT linear_id,name,email FROM users WHERE lower(email)=?",
        (server.PRIMARY_APPROVER,),
    ).fetchone()
if not admin:
    raise RuntimeError("Primary administrator is missing")

session = server.make_session({"id": admin["linear_id"], "name": admin["name"], "email": admin["email"]})
status, page = request("/admin", cookie=session)
if status != 200 or "新增员工".encode() not in page or "代员工申请".encode() not in page or "考勤记录导出".encode() not in page or "修改请假记录".encode() not in page or b"Remote" not in page or "育儿假".encode() not in page or "路程假".encode() not in page or "司龄假".encode() not in page:
    sys.exit(11)

with server.db() as connection:
    existing = connection.execute("SELECT linear_id FROM users WHERE lower(email)=?", (TEST_EMAIL,)).fetchone()
    if existing:
        connection.execute("DELETE FROM leave_requests WHERE user_id=?", (existing["linear_id"],))
        connection.execute("DELETE FROM users WHERE linear_id=?", (existing["linear_id"],))

payload = {
    "name": "功能测试员工",
    "email": TEST_EMAIL,
    "company": "Example",
    "work_start_date": "2010-01-01",
    "hire_date": "2025-01-01",
    "annual_leave_used": 1.5,
}
try:
    status, body = request("/api/admin/users", method="POST", body=payload, cookie=session)
except urllib.error.HTTPError:
    sys.exit(12)
created = json.loads(body)
if status != 201 or created["user"]["statutory_standard"] != 10:
    sys.exit(13)
target = urllib.parse.quote(created["user"]["linear_id"], safe="")
employee_session = server.make_session({"id": created["user"]["linear_id"], "name": payload["name"], "email": TEST_EMAIL})
admin_leave_payload = {
    "employee_id": created["user"]["linear_id"],
    "leave_type": "事假",
    "status": "approved",
    "start_date": "2020-01-02",
    "end_date": "2020-01-02",
    "start_period": "下午",
    "end_period": "下午",
    "handover": "",
    "reason": "管理员代申请过去日期验证",
}
try:
    status, body = request("/api/admin/requests", method="POST", body=admin_leave_payload, cookie=session)
except urllib.error.HTTPError:
    sys.exit(36)
admin_leave = json.loads(body)
if status != 201 or admin_leave["request"]["status"] != "approved" or admin_leave["request"]["days"] != 0.5:
    sys.exit(37)
admin_leave_id = admin_leave["request"]["id"]
try:
    status, _ = request(f"/api/admin/requests/{admin_leave_id}", method="DELETE", cookie=session)
except urllib.error.HTTPError:
    sys.exit(38)
if status != 200:
    sys.exit(39)

proxy_leave_payload = {
    **admin_leave_payload,
    "status": "pending_secondary",
    "start_date": "2020-01-03",
    "end_date": "2020-01-03",
    "reason": "管理员代终审验证",
}
status, body = request("/api/admin/requests", method="POST", body=proxy_leave_payload, cookie=session)
proxy_leave = json.loads(body)
if status != 201:
    sys.exit(40)
proxy_leave_id = proxy_leave["request"]["id"]
status, _ = request(f"/api/admin/requests/{proxy_leave_id}/status", method="POST", body={"status": "approved", "stage": "secondary"}, cookie=session)
if status != 200:
    sys.exit(41)
status, body = request("/api/admin/data", cookie=session)
approved = next((item for item in json.loads(body)["requests"] if item["id"] == proxy_leave_id), None)
if status != 200 or not approved or approved["status"] != "approved" or not approved.get("secondary_decided_by"):
    sys.exit(42)
status, _ = request(f"/api/admin/requests/{proxy_leave_id}", method="DELETE", cookie=session)
if status != 200:
    sys.exit(43)

split_stage_payload = {
    **admin_leave_payload,
    "status": "pending_primary",
    "start_date": "2020-01-04",
    "end_date": "2020-01-04",
    "reason": "两级审批防重复验证",
}
status, body = request("/api/admin/requests", method="POST", body=split_stage_payload, cookie=session)
split_stage_id = json.loads(body)["request"]["id"]
if status != 201:
    sys.exit(44)
status, _ = request(f"/api/admin/requests/{split_stage_id}/status", method="POST", body={"status": "approved", "stage": "primary"}, cookie=session)
if status != 200:
    sys.exit(45)
try:
    status, body = request(f"/api/admin/requests/{split_stage_id}/status", method="POST", body={"status": "approved", "stage": "primary"}, cookie=session)
except urllib.error.HTTPError as error:
    status, body = error.code, error.read()
if status != 409 or json.loads(body).get("error") != "approval_stage_changed":
    sys.exit(46)
status, body = request("/api/admin/data", cookie=session)
split_stage = next((item for item in json.loads(body)["requests"] if item["id"] == split_stage_id), None)
if status != 200 or not split_stage or split_stage["status"] != "pending_secondary" or split_stage.get("secondary_decision"):
    sys.exit(47)
status, _ = request(f"/api/admin/requests/{split_stage_id}", method="DELETE", cookie=session)
if status != 200:
    sys.exit(48)

status, employee_page = request("/", cookie=employee_session)
if status != 200 or "可休司龄假".encode() not in employee_page or b"{{SENIORITY_BALANCE}}" in employee_page or b"loadTeam(),loadLeaveHistory()" not in employee_page or b"if(id==='leave-history')loadLeaveHistory()" not in employee_page:
    sys.exit(28)
today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
remote_day = today + timedelta(days=(7 - today.weekday()) % 7 + 7)
remote_payload = {
    "leave_type": "Remote",
    "start_date": remote_day.isoformat(),
    "end_date": remote_day.isoformat(),
    "start_period": "上午",
    "end_period": "下午",
    "handover": "",
    "reason": "Remote quota verification",
}
try:
    status, _ = request("/api/leave-requests", method="POST", body=remote_payload, cookie=employee_session)
except urllib.error.HTTPError:
    sys.exit(24)
if status != 201:
    sys.exit(25)
second_remote = {**remote_payload, "start_date": (remote_day + timedelta(days=1)).isoformat(), "end_date": (remote_day + timedelta(days=1)).isoformat(), "end_period": "上午"}
try:
    request("/api/leave-requests", method="POST", body=second_remote, cookie=employee_session)
    sys.exit(26)
except urllib.error.HTTPError as error:
    quota_response = json.loads(error.read())
    if error.code != 400 or quota_response.get("error") != "remote_weekly_quota_exceeded" or quota_response.get("remaining") != 0:
        sys.exit(27)
seniority_day = remote_day + timedelta(days=2)
seniority_payload = {
    "leave_type": "司龄假",
    "start_date": seniority_day.isoformat(),
    "end_date": seniority_day.isoformat(),
    "start_period": "上午",
    "end_period": "下午",
    "handover": "",
    "reason": "Seniority quota verification",
}
try:
    status, _ = request("/api/leave-requests", method="POST", body=seniority_payload, cookie=employee_session)
except urllib.error.HTTPError:
    sys.exit(29)
if status != 201:
    sys.exit(30)
second_seniority = {**seniority_payload, "start_date": (seniority_day + timedelta(days=1)).isoformat(), "end_date": (seniority_day + timedelta(days=1)).isoformat(), "end_period": "上午"}
try:
    request("/api/leave-requests", method="POST", body=second_seniority, cookie=employee_session)
    sys.exit(31)
except urllib.error.HTTPError as error:
    quota_response = json.loads(error.read())
    if error.code != 400 or quota_response.get("error") != "seniority_leave_quota_exceeded" or quota_response.get("remaining") != 0:
        sys.exit(32)
with server.db() as connection:
    cursor = connection.execute(
        "INSERT INTO leave_requests(user_id,leave_type,start_date,end_date,start_period,end_period,days,status,primary_decision,secondary_decision,secondary_decided_at) VALUES(?,?,?,?,?,?,?,'approved','approved','approved',CURRENT_TIMESTAMP)",
        (created["user"]["linear_id"], "病假", "2026-09-22", "2026-09-22", "上午", "上午", 0.5),
    )
    leave_id = cursor.lastrowid
edit_payload = {
    "leave_type": "病假",
    "status": "approved",
    "start_date": "2026-09-22",
    "start_period": "上午",
    "end_date": "2026-09-22",
    "end_period": "下午",
    "handover": "测试交接",
    "reason": "测试修改请假记录",
}
try:
    status, body = request(f"/api/admin/requests/{leave_id}", method="POST", body=edit_payload, cookie=session)
except urllib.error.HTTPError:
    sys.exit(22)
edited = json.loads(body)
if status != 200 or edited["request"]["days"] != 1.0 or edited["request"]["reason"] != edit_payload["reason"]:
    sys.exit(23)
try:
    status, workbook = request("/api/admin/attendance.xlsx?month=2026-09", cookie=session)
except urllib.error.HTTPError:
    sys.exit(17)
if status != 200 or not workbook.startswith(b"PK"):
    sys.exit(18)
with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
    names = set(archive.namelist())
    if not {"xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"}.issubset(names):
        sys.exit(19)
    workbook_xml = archive.read("xl/workbook.xml")
    summary_xml = archive.read("xl/worksheets/sheet1.xml")
    detail_xml = archive.read("xl/worksheets/sheet2.xml")
    if "考勤汇总".encode() not in workbook_xml or "请假明细".encode() not in workbook_xml:
        sys.exit(20)
    if "功能测试员工".encode() not in summary_xml or "病假".encode() not in detail_xml or "育儿假".encode() not in summary_xml or "路程假".encode() not in summary_xml or "司龄假".encode() not in summary_xml or b">1.0<" not in detail_xml:
        sys.exit(21)
try:
    status, _ = request(f"/api/admin/requests/{leave_id}", method="DELETE", cookie=session)
except urllib.error.HTTPError:
    sys.exit(33)
if status != 200:
    sys.exit(34)
with server.db() as connection:
    if connection.execute("SELECT 1 FROM leave_requests WHERE id=?", (leave_id,)).fetchone():
        sys.exit(35)
try:
    status, _ = request(f"/api/admin/users/{target}", method="DELETE", cookie=session)
except urllib.error.HTTPError:
    sys.exit(14)
if status != 200:
    sys.exit(15)
with server.db() as connection:
    inactive = connection.execute("SELECT linear_id,is_active FROM users WHERE lower(email)=?", (TEST_EMAIL,)).fetchone()
    retained = connection.execute("SELECT COUNT(*) FROM leave_requests WHERE user_id=?", (created["user"]["linear_id"],)).fetchone()[0]
    if not inactive or inactive["is_active"] != 0 or retained != 2:
        sys.exit(16)
    connection.execute("DELETE FROM leave_requests WHERE user_id=?", (created["user"]["linear_id"],))
    connection.execute("DELETE FROM users WHERE linear_id=?", (created["user"]["linear_id"],))

print(json.dumps({"ok": True, "ui": True, "create": True, "admin_leave_create": True, "past_admin_leave": True, "formula": True, "remote_weekly_quota": True, "seniority_leave_quota": True, "new_leave_types": True, "attendance_export": True, "request_edit": True, "request_delete": True, "soft_delete": True, "history_retained": True}))
