import sqlite3
import sys

import server


def fail(code, message):
    print(message)
    raise SystemExit(code)


connection = sqlite3.connect("data/qingjia.db")
connection.row_factory = sqlite3.Row

admins = {
    row["email"].lower()
    for row in connection.execute("SELECT email FROM users WHERE role='admin'")
}
if admins != server.ADMIN_EMAILS:
    fail(41, f"unexpected admins: {sorted(admins)}")

columns = {
    row[1]
    for row in connection.execute("PRAGMA table_info(leave_requests)")
}
required_columns = {
    "primary_decision",
    "primary_decided_at",
    "secondary_decision",
    "secondary_decided_at",
}
if not required_columns.issubset(columns):
    fail(42, f"missing leave workflow columns: {sorted(required_columns - columns)}")

outbox_exists = connection.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_outbox'"
).fetchone()
if not outbox_exists:
    fail(43, "email_outbox table is missing")

if server.PRIMARY_APPROVER not in admins:
    fail(44, f"unexpected primary approver: {server.PRIMARY_APPROVER}")
if server.SECONDARY_APPROVER not in admins:
    fail(45, f"unexpected secondary approver: {server.SECONDARY_APPROVER}")

invalid_status_count = connection.execute(
    "SELECT COUNT(*) FROM leave_requests WHERE status='pending'"
).fetchone()[0]
if invalid_status_count:
    fail(46, f"legacy pending requests remain: {invalid_status_count}")

connection.close()
print("workflow verification passed")
sys.exit(0)
