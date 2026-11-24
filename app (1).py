"""
Library Inventory Management System
-----------------------------------
Stack : Python + Streamlit (UI) + SQLite (database, stored in library.db)
Run   : streamlit run app.py

Core idea: a BOOK (title) is one record, and every physical COPY of it is its
own record with its own status. That is what makes multiple copies, issuing,
losing and fining work correctly.

Tables : books, copies, members, loans, settings, log
"""
import os
import sqlite3
import datetime as dt
from contextlib import contextmanager

import pandas as pd
import streamlit as st

DB_PATH = os.environ.get("LIB_DB", "library.db")

# Library rules. Every value can be changed on the Settings page.
DEFAULTS = {
    "fine_per_day": 2.0,         # Rs per day late
    "fine_cap_pct": 100.0,       # late fine never exceeds this % of the book price
    "loan_days_student": 14,
    "loan_days_faculty": 30,
    "max_books_student": 3,
    "max_books_faculty": 6,
    "max_renewals": 2,
    "block_fine_threshold": 50.0,  # unpaid fines at/above this block new issues
    "lost_fee": 50.0,            # processing fee added to the price of a lost book
    "damage_fee_pct": 30.0,      # damage charge as % of the book price
}

CATEGORIES = ["Computer Science", "Programming", "Electronics", "Mathematics",
              "Science", "Fiction", "History", "Biography", "Reference", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    isbn      TEXT UNIQUE NOT NULL,
    title     TEXT NOT NULL,
    author    TEXT NOT NULL,
    category  TEXT NOT NULL DEFAULT 'Other',
    price     REAL NOT NULL DEFAULT 0,
    shelf     TEXT DEFAULT '',
    archived  INTEGER NOT NULL DEFAULT 0,
    added_on  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copies (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id   INTEGER NOT NULL REFERENCES books(id),
    copy_no   INTEGER NOT NULL,
    status    TEXT NOT NULL DEFAULT 'Available'
              CHECK (status IN ('Available','Issued','Lost','Damaged','Withdrawn')),
    UNIQUE (book_id, copy_no)
);
CREATE TABLE IF NOT EXISTS members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_code TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    email       TEXT DEFAULT '',
    mtype       TEXT NOT NULL DEFAULT 'Student',
    joined_on   TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_id     INTEGER NOT NULL REFERENCES copies(id),
    member_id   INTEGER NOT NULL REFERENCES members(id),
    issue_date  TEXT NOT NULL,
    due_date    TEXT NOT NULL,
    return_date TEXT,
    renewals    INTEGER NOT NULL DEFAULT 0,
    fine_amount REAL NOT NULL DEFAULT 0,
    fine_paid   REAL NOT NULL DEFAULT 0,
    fine_status TEXT NOT NULL DEFAULT 'none'
                CHECK (fine_status IN ('none','unpaid','paid','waived')),
    fine_reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL
);
"""

LOAN_SQL = """
SELECT l.id AS loan_id, l.copy_id, l.member_id, l.issue_date, l.due_date,
       l.return_date, l.renewals, l.fine_amount, l.fine_paid, l.fine_status,
       l.fine_reason, m.member_code, m.name AS member_name, m.mtype,
       b.id AS book_id, b.title, b.author, b.price, c.copy_no
FROM loans l
JOIN members m ON m.id = l.member_id
JOIN copies  c ON c.id = l.copy_id
JOIN books   b ON b.id = c.book_id
"""


class RuleError(Exception):
    """Raised when an action breaks a library rule (shown to the user as an error)."""


# ------------------------------------------------------------------ database
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        for k, v in DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, str(v)))


def fetch(sql, params=()):
    with db() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def frame(sql, params=()):
    with db() as con:
        return pd.read_sql_query(sql, con, params=params)


def S():
    d = {r["key"]: float(r["value"]) for r in fetch("SELECT key, value FROM settings")}
    return {**DEFAULTS, **d}


def today():
    return dt.date.today()


def log(con, action, detail):
    con.execute("INSERT INTO log (ts, action, detail) VALUES (?, ?, ?)",
                (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, detail))


def money(x):
    return f"₹{x:,.2f}".replace(".00", "")


# --------------------------------------------------------------- fine maths
def overdue_fine(due_iso, price, on=None, s=None):
    """Return (days_late, late_fine). Fine is capped at fine_cap_pct of price."""
    s = s or S()
    on = on or today()
    days = (on - dt.date.fromisoformat(due_iso)).days
    if days <= 0:
        return 0, 0.0
    fine = days * s["fine_per_day"]
    if price and price > 0:
        fine = min(fine, price * s["fine_cap_pct"] / 100)
    return days, round(fine, 2)


# --------------------------------------------------------------------- books
def _add_copies(con, book_id, n):
    start = con.execute("SELECT COALESCE(MAX(copy_no), 0) FROM copies WHERE book_id=?",
                        (book_id,)).fetchone()[0]
    for i in range(1, n + 1):
        con.execute("INSERT INTO copies (book_id, copy_no, status) VALUES (?, ?, 'Available')",
                    (book_id, start + i))


def add_book(isbn, title, author, category, price, shelf, copies):
    isbn = "".join(ch for ch in isbn if ch.isalnum()).upper()
    title, author = title.strip(), author.strip()
    if not title or not author:
        raise RuleError("Title and author are required.")
    if len(isbn) not in (10, 13):
        raise RuleError("ISBN must have 10 or 13 characters (hyphens and spaces are ignored).")
    copies = int(copies)
    if not 1 <= copies <= 100:
        raise RuleError("Number of copies must be between 1 and 100.")
    if price < 0:
        raise RuleError("Price cannot be negative.")
    with db() as con:
        ex = con.execute("SELECT id, title, archived FROM books WHERE isbn=?", (isbn,)).fetchone()
        if ex:  # same ISBN = same title: add copies instead of a duplicate record
            if ex["archived"]:
                con.execute("UPDATE books SET archived=0 WHERE id=?", (ex["id"],))
            _add_copies(con, ex["id"], copies)
            log(con, "ADD COPIES", f"{copies} copy(ies) added to existing title '{ex['title']}'")
            return (f"This ISBN already exists as '{ex['title']}'. Added {copies} new "
                    f"cop{'y' if copies == 1 else 'ies'} to it instead of creating a duplicate.")
        cur = con.execute(
            "INSERT INTO books (isbn, title, author, category, price, shelf, added_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (isbn, title, author, category, float(price), shelf.strip(), today().isoformat()))
        _add_copies(con, cur.lastrowid, copies)
        log(con, "ADD BOOK", f"'{title}' by {author} with {copies} copy(ies)")
        return f"Added '{title}' with {copies} cop{'y' if copies == 1 else 'ies'}."


def add_copies(book_id, n):
    n = int(n)
    if not 1 <= n <= 100:
        raise RuleError("Number of copies must be between 1 and 100.")
    with db() as con:
        b = con.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
        _add_copies(con, book_id, n)
        log(con, "ADD COPIES", f"{n} copy(ies) added to '{b['title']}'")
    return f"Added {n} cop{'y' if n == 1 else 'ies'}."


def change_copy_status(copy_id, new_status):
    with db() as con:
        c = con.execute("SELECT c.*, b.title FROM copies c JOIN books b ON b.id=c.book_id "
                        "WHERE c.id=?", (copy_id,)).fetchone()
        if c["status"] == "Issued":
            raise RuleError("This copy is currently issued. Process its return first.")
        con.execute("UPDATE copies SET status=? WHERE id=?", (new_status, copy_id))
        log(con, "COPY STATUS", f"'{c['title']}' copy #{c['copy_no']}: {c['status']} -> {new_status}")
    return f"Copy #{c['copy_no']} is now {new_status}."


def delete_copy(copy_id):
    with db() as con:
        c = con.execute("SELECT c.*, b.title FROM copies c JOIN books b ON b.id=c.book_id "
                        "WHERE c.id=?", (copy_id,)).fetchone()
        if c["status"] == "Issued":
            raise RuleError("This copy is currently issued. Process its return first.")
        hist = con.execute("SELECT COUNT(*) FROM loans WHERE copy_id=?", (copy_id,)).fetchone()[0]
        if hist:  # keep the history, just take the copy out of circulation
            con.execute("UPDATE copies SET status='Withdrawn' WHERE id=?", (copy_id,))
            msg = f"Copy #{c['copy_no']} has loan history, so it was withdrawn (history kept)."
        else:
            con.execute("DELETE FROM copies WHERE id=?", (copy_id,))
            msg = f"Copy #{c['copy_no']} deleted."
        log(con, "REMOVE COPY", f"'{c['title']}' copy #{c['copy_no']}")
    return msg


def delete_book(book_id):
    with db() as con:
        b = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        issued = con.execute("SELECT COUNT(*) FROM copies WHERE book_id=? AND status='Issued'",
                             (book_id,)).fetchone()[0]
        if issued:
            raise RuleError(f"Cannot delete: {issued} cop{'y is' if issued == 1 else 'ies are'} "
                            f"still issued. Get them returned first.")
        hist = con.execute("SELECT COUNT(*) FROM loans l JOIN copies c ON c.id=l.copy_id "
                           "WHERE c.book_id=?", (book_id,)).fetchone()[0]
        if hist:  # archive instead of erasing, so past records and fines stay valid
            con.execute("UPDATE books SET archived=1 WHERE id=?", (book_id,))
            con.execute("UPDATE copies SET status='Withdrawn' WHERE book_id=?", (book_id,))
            msg = f"'{b['title']}' has loan history, so it was archived (hidden), not erased."
        else:
            con.execute("DELETE FROM copies WHERE book_id=?", (book_id,))
            con.execute("DELETE FROM books WHERE id=?", (book_id,))
            msg = f"'{b['title']}' deleted permanently."
        log(con, "DELETE BOOK", msg)
    return msg


# ------------------------------------------------------------------- members
def add_member(code, name, email, mtype):
    code, name, email = code.strip().upper(), name.strip(), email.strip()
    if not code or not name:
        raise RuleError("Member ID and name are required.")
    if email and "@" not in email:
        raise RuleError("That email address does not look valid.")
    try:
        with db() as con:
            con.execute("INSERT INTO members (member_code, name, email, mtype, joined_on) "
                        "VALUES (?, ?, ?, ?, ?)", (code, name, email, mtype, today().isoformat()))
            log(con, "ADD MEMBER", f"{code} {name} ({mtype})")
    except sqlite3.IntegrityError:
        raise RuleError(f"Member ID {code} already exists.")
    return f"Member {name} ({code}) added."


def set_member_active(member_id, active):
    with db() as con:
        m = con.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        if not active:
            n = con.execute("SELECT COUNT(*) FROM loans WHERE member_id=? AND return_date IS NULL",
                            (member_id,)).fetchone()[0]
            if n:
                raise RuleError(f"{m['name']} still has {n} book(s) issued. Return them first.")
            owed = con.execute("SELECT COALESCE(SUM(fine_amount-fine_paid),0) FROM loans "
                               "WHERE member_id=? AND fine_status='unpaid'", (member_id,)).fetchone()[0]
            if owed > 0:
                raise RuleError(f"{m['name']} has unpaid fines of {money(owed)}. Clear them first.")
        con.execute("UPDATE members SET active=? WHERE id=?", (1 if active else 0, member_id))
        log(con, "MEMBER", f"{m['member_code']} {'reactivated' if active else 'deactivated'}")
    return f"{m['name']} is now {'active' if active else 'inactive'}."


def unpaid_fines(member_id):
    r = fetch("SELECT COALESCE(SUM(fine_amount-fine_paid),0) AS t FROM loans "
              "WHERE member_id=? AND fine_status='unpaid'", (member_id,))
    return r[0]["t"]


def active_loans(member_id=None):
    sql = LOAN_SQL + " WHERE l.return_date IS NULL"
    params = ()
    if member_id:
        sql += " AND l.member_id=?"
        params = (member_id,)
    return fetch(sql + " ORDER BY l.due_date", params)


# ------------------------------------------------------------------ circulation
def issue_book(member_id, book_id, issue_date=None):
    s = S()
    issue_date = issue_date or today()
    with db() as con:
        m = con.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        if not m or not m["active"]:
            raise RuleError("Member not found or inactive.")
        b = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not b or b["archived"]:
            raise RuleError("This book is not in the catalogue.")
        kind = m["mtype"].lower()
        limit = int(s[f"max_books_{kind}"])
        act = con.execute(
            "SELECT l.due_date, c.book_id FROM loans l JOIN copies c ON c.id=l.copy_id "
            "WHERE l.member_id=? AND l.return_date IS NULL", (member_id,)).fetchall()
        if len(act) >= limit:
            raise RuleError(f"{m['name']} already has {len(act)} books issued (limit {limit} "
                            f"for {m['mtype'].lower()}s).")
        if any(dt.date.fromisoformat(a["due_date"]) < today() for a in act):
            raise RuleError(f"{m['name']} has an overdue book. It must be returned before "
                            f"issuing another.")
        owed = con.execute("SELECT COALESCE(SUM(fine_amount-fine_paid),0) FROM loans "
                           "WHERE member_id=? AND fine_status='unpaid'", (member_id,)).fetchone()[0]
        if owed >= s["block_fine_threshold"]:
            raise RuleError(f"{m['name']} has unpaid fines of {money(owed)} (limit "
                            f"{money(s['block_fine_threshold'])}). Collect the fine first.")
        if any(a["book_id"] == book_id for a in act):
            raise RuleError(f"{m['name']} already has a copy of '{b['title']}'.")
        copy = con.execute("SELECT * FROM copies WHERE book_id=? AND status='Available' "
                           "ORDER BY copy_no LIMIT 1", (book_id,)).fetchone()
        if not copy:
            raise RuleError(f"No copy of '{b['title']}' is available right now "
                            f"(all copies are issued, lost or damaged).")
        due = issue_date + dt.timedelta(days=int(s[f"loan_days_{kind}"]))
        con.execute("INSERT INTO loans (copy_id, member_id, issue_date, due_date) VALUES (?,?,?,?)",
                    (copy["id"], member_id, issue_date.isoformat(), due.isoformat()))
        con.execute("UPDATE copies SET status='Issued' WHERE id=?", (copy["id"],))
        log(con, "ISSUE", f"'{b['title']}' copy #{copy['copy_no']} -> {m['member_code']} {m['name']}, due {due}")
    return {"title": b["title"], "copy_no": copy["copy_no"], "due": due, "member": m["name"]}


def preview_return(loan, condition, on=None):
    """Return (days_late, fine, reason) without changing anything."""
    s = S()
    price = loan["price"] or 0
    days, late = overdue_fine(loan["due_date"], price, on, s)
    if condition == "Lost":
        fine = price + s["lost_fee"]
        return days, round(fine, 2), f"Lost book: price {money(price)} + processing fee {money(s['lost_fee'])}"
    if condition == "Damaged":
        dmg = round(price * s["damage_fee_pct"] / 100, 2)
        reason = f"Damaged: {money(dmg)} damage charge"
        if late:
            reason += f" + {days} day(s) late {money(late)}"
        return days, round(late + dmg, 2), reason
    return days, late, (f"{days} day(s) late" if late else "")


def return_loan(loan_id, condition="Good", return_date=None):
    with db() as con:
        rows = con.execute(LOAN_SQL + " WHERE l.id=?", (loan_id,)).fetchall()
    if not rows or rows[0]["return_date"]:
        raise RuleError("Loan not found or already returned.")
    loan = dict(rows[0])
    rd = return_date or today()
    days, fine, reason = preview_return(loan, condition, rd)
    new_status = {"Good": "Available", "Damaged": "Damaged", "Lost": "Lost"}[condition]
    with db() as con:
        con.execute("UPDATE loans SET return_date=?, fine_amount=?, fine_status=?, fine_reason=? "
                    "WHERE id=?", (rd.isoformat(), fine, "unpaid" if fine > 0 else "none", reason, loan_id))
        con.execute("UPDATE copies SET status=? WHERE id=?", (new_status, loan["copy_id"]))
        log(con, "RETURN" if condition != "Lost" else "LOST",
            f"'{loan['title']}' copy #{loan['copy_no']} from {loan['member_code']} ({condition}), fine {money(fine)}")
    return {"fine": fine, "days": days, "reason": reason, "title": loan["title"]}


def renew_loan(loan_id):
    s = S()
    with db() as con:
        rows = con.execute(LOAN_SQL + " WHERE l.id=?", (loan_id,)).fetchall()
        if not rows or rows[0]["return_date"]:
            raise RuleError("Loan not found or already returned.")
        l = rows[0]
        if dt.date.fromisoformat(l["due_date"]) < today():
            raise RuleError("This book is overdue and cannot be renewed. Please return it.")
        if l["renewals"] >= int(s["max_renewals"]):
            raise RuleError(f"Renewal limit reached ({int(s['max_renewals'])} per loan).")
        days = int(s[f"loan_days_{l['mtype'].lower()}"])
        new_due = dt.date.fromisoformat(l["due_date"]) + dt.timedelta(days=days)
        con.execute("UPDATE loans SET due_date=?, renewals=renewals+1 WHERE id=?",
                    (new_due.isoformat(), loan_id))
        log(con, "RENEW", f"'{l['title']}' for {l['member_code']}, new due {new_due}")
    return new_due


# ---------------------------------------------------------------------- fines
def pay_fine(loan_id, amount):
    with db() as con:
        l = con.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not l or l["fine_status"] != "unpaid":
            raise RuleError("This loan has no unpaid fine.")
        owed = l["fine_amount"] - l["fine_paid"]
        if amount <= 0:
            raise RuleError("Enter an amount greater than zero.")
        if amount > owed + 0.001:
            raise RuleError(f"Amount is more than the outstanding {money(owed)}.")
        paid = l["fine_paid"] + amount
        status = "paid" if paid >= l["fine_amount"] - 0.001 else "unpaid"
        con.execute("UPDATE loans SET fine_paid=?, fine_status=? WHERE id=?", (paid, status, loan_id))
        log(con, "FINE PAID", f"loan {loan_id}: {money(amount)} received ({status})")
    return f"Received {money(amount)}. " + ("Fine cleared." if status == "paid"
                                            else f"Remaining {money(owed - amount)}.")


def waive_fine(loan_id, reason):
    if not reason.strip():
        raise RuleError("A reason is required to waive a fine (it is recorded in the log).")
    with db() as con:
        l = con.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not l or l["fine_status"] != "unpaid":
            raise RuleError("This loan has no unpaid fine.")
        owed = l["fine_amount"] - l["fine_paid"]
        con.execute("UPDATE loans SET fine_status='waived' WHERE id=?", (loan_id,))
        log(con, "FINE WAIVED", f"loan {loan_id}: {money(owed)} waived. Reason: {reason.strip()}")
    return f"Waived {money(owed)}."


# ------------------------------------------------------------------ reporting
def catalog_frame(include_archived=False):
    return frame(f"""
        SELECT b.id AS book_id, b.isbn AS ISBN, b.title AS Title, b.author AS Author,
               b.category AS Category, b.shelf AS Shelf, b.price AS Price,
               COUNT(c.id) AS Total,
               COALESCE(SUM(c.status='Available'),0) AS Available,
               COALESCE(SUM(c.status='Issued'),0)    AS Issued,
               COALESCE(SUM(c.status IN ('Lost','Damaged')),0) AS "Lost/Damaged"
        FROM books b
        LEFT JOIN copies c ON c.book_id=b.id AND c.status != 'Withdrawn'
        {'' if include_archived else 'WHERE b.archived=0'}
        GROUP BY b.id ORDER BY b.title""")


def active_loan_frame(member_id=None):
    rows = active_loans(member_id)
    s = S()
    out = []
    for r in rows:
        days, fine = overdue_fine(r["due_date"], r["price"], s=s)
        out.append({"loan_id": r["loan_id"], "Member ID": r["member_code"], "Member": r["member_name"],
                    "Title": r["title"], "Copy": r["copy_no"], "Issued": r["issue_date"],
                    "Due": r["due_date"], "Days late": days, "Fine so far": fine,
                    "Renewals": r["renewals"]})
    return pd.DataFrame(out, columns=["loan_id", "Member ID", "Member", "Title", "Copy", "Issued",
                                      "Due", "Days late", "Fine so far", "Renewals"])


def stats():
    s = {}
    s["titles"] = fetch("SELECT COUNT(*) n FROM books WHERE archived=0")[0]["n"]
    st_rows = fetch("SELECT c.status, COUNT(*) n FROM copies c JOIN books b ON b.id=c.book_id "
                    "WHERE b.archived=0 AND c.status!='Withdrawn' GROUP BY c.status")
    s["by_status"] = {r["status"]: r["n"] for r in st_rows}
    s["copies"] = sum(s["by_status"].values())
    s["members"] = fetch("SELECT COUNT(*) n FROM members WHERE active=1")[0]["n"]
    al = active_loan_frame()
    s["active_loans"] = len(al)
    s["overdue"] = int((al["Days late"] > 0).sum()) if len(al) else 0
    billed = fetch("SELECT COALESCE(SUM(fine_amount-fine_paid),0) t FROM loans "
                   "WHERE fine_status='unpaid'")[0]["t"]
    s["fines_outstanding"] = billed
    s["fines_accruing"] = float(al["Fine so far"].sum()) if len(al) else 0.0
    return s


# ------------------------------------------------------------------- demo data
def seed_demo():
    books = [
        ("9780000000011", "Wings of Fire", "A.P.J. Abdul Kalam", "Biography", 250, "B-01", 3),
        ("9780000000028", "Clean Code", "Robert C. Martin", "Programming", 550, "P-02", 2),
        ("9780000000035", "Python Crash Course", "Eric Matthes", "Programming", 600, "P-03", 4),
        ("9780000000042", "Digital Design", "M. Morris Mano", "Electronics", 500, "E-01", 2),
        ("9780000000059", "Microelectronic Circuits", "Sedra & Smith", "Electronics", 900, "E-02", 3),
        ("9780000000066", "Signals and Systems", "Alan Oppenheim", "Electronics", 450, "E-03", 2),
        ("9780000000073", "Introduction to Algorithms", "Thomas Cormen", "Computer Science", 1200, "C-01", 2),
        ("9780000000080", "Discrete Mathematics", "Kenneth Rosen", "Mathematics", 700, "M-01", 3),
        ("9780000000097", "Malgudi Days", "R.K. Narayan", "Fiction", 200, "F-01", 2),
        ("9780000000103", "The Discovery of India", "Jawaharlal Nehru", "History", 350, "H-01", 1),
        ("9780000000110", "Fundamentals of Physics", "Halliday & Resnick", "Science", 800, "S-01", 2),
    ]
    for b in books:
        add_book(*b)
    for m in [("M001", "Aarav Sharma", "aarav@example.com", "Student"),
              ("M002", "Diya Nair", "diya@example.com", "Student"),
              ("M003", "Rohan Verma", "rohan@example.com", "Student"),
              ("M004", "Dr. Meera Kulkarni", "meera@example.com", "Faculty"),
              ("M005", "Sneha Iyer", "sneha@example.com", "Student"),
              ("M006", "Kabir Singh", "kabir@example.com", "Student")]:
        add_member(*m)
    bid = {r["title"]: r["id"] for r in fetch("SELECT id, title FROM books")}
    mid = {r["member_code"]: r["id"] for r in fetch("SELECT id, member_code FROM members")}
    d = today()
    issue_book(mid["M001"], bid["Wings of Fire"], d - dt.timedelta(days=3))            # on time
    issue_book(mid["M001"], bid["Python Crash Course"], d - dt.timedelta(days=5))
    issue_book(mid["M002"], bid["Clean Code"], d - dt.timedelta(days=20))              # overdue
    issue_book(mid["M004"], bid["Introduction to Algorithms"], d - dt.timedelta(days=10))
    issue_book(mid["M004"], bid["Discrete Mathematics"], d - dt.timedelta(days=2))
    issue_book(mid["M006"], bid["Digital Design"], d - dt.timedelta(days=4))
    issue_book(mid["M003"], bid["Digital Design"], d - dt.timedelta(days=1))           # all copies out
    # a book returned late (fine unpaid)
    issue_book(mid["M003"], bid["Malgudi Days"], d - dt.timedelta(days=30))
    lid = fetch("SELECT l.id FROM loans l JOIN copies c ON c.id=l.copy_id "
                "WHERE l.member_id=? AND c.book_id=?", (mid["M003"], bid["Malgudi Days"]))[0]["id"]
    return_loan(lid, "Good", d - dt.timedelta(days=10))
    # a lost book, part-paid
    issue_book(mid["M005"], bid["Signals and Systems"], d - dt.timedelta(days=25))
    lid = fetch("SELECT l.id FROM loans l JOIN copies c ON c.id=l.copy_id "
                "WHERE l.member_id=? AND c.book_id=?", (mid["M005"], bid["Signals and Systems"]))[0]["id"]
    return_loan(lid, "Lost", d - dt.timedelta(days=3))
    pay_fine(lid, 200)


def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()


# --------------------------------------------------------------------- UI bits
def flash(kind, msg):
    st.session_state.setdefault("flash", []).append((kind, msg))


def show_flash():
    for kind, msg in st.session_state.pop("flash", []):
        getattr(st, kind)(msg)


def table(df, **kw):
    if df is None or len(df) == 0:
        st.info("Nothing to show here yet.")
    else:
        st.dataframe(df, hide_index=True, **kw)


def do(action, *args, ok_kind="success", rerun=True, **kwargs):
    """Run a business action; show rule errors nicely."""
    try:
        res = action(*args, **kwargs)
    except RuleError as e:
        st.error(str(e))
        return None
    if isinstance(res, str):
        flash(ok_kind, res)
    if rerun:
        st.rerun()
    return res


# ------------------------------------------------------------------------ pages
def page_dashboard():
    st.title("Dashboard")
    s = stats()
    c = st.columns(5)
    c[0].metric("Book titles", s["titles"])
    c[1].metric("Total copies", s["copies"])
    c[2].metric("Available now", s["by_status"].get("Available", 0))
    c[3].metric("Issued", s["by_status"].get("Issued", 0))
    c[4].metric("Lost / damaged", s["by_status"].get("Lost", 0) + s["by_status"].get("Damaged", 0))
    c = st.columns(4)
    c[0].metric("Active members", s["members"])
    c[1].metric("Books on loan", s["active_loans"])
    c[2].metric("Overdue books", s["overdue"])
    c[3].metric("Fines outstanding", money(s["fines_outstanding"] + s["fines_accruing"]),
                help="Unpaid billed fines plus fines still accruing on overdue books")

    left, right = st.columns(2)
    with left:
        st.subheader("Copies by status")
        if s["by_status"]:
            st.bar_chart(pd.Series(s["by_status"], name="copies"))
        else:
            st.info("No copies yet.")
    with right:
        st.subheader("Titles by category")
        cat = frame("SELECT category, COUNT(*) AS titles FROM books WHERE archived=0 GROUP BY category")
        if len(cat):
            st.bar_chart(cat.set_index("category")["titles"])
        else:
            st.info("No books yet.")

    al = active_loan_frame()
    st.subheader("Overdue books")
    table(al[al["Days late"] > 0].drop(columns=["loan_id"]) if len(al) else al)
    st.subheader("Due in the next 3 days")
    if len(al):
        soon = al[(al["Days late"] <= 0) &
                  (pd.to_datetime(al["Due"]) <= pd.Timestamp(today() + dt.timedelta(days=3)))]
        table(soon.drop(columns=["loan_id", "Days late", "Fine so far"]))
    else:
        table(al)
    st.subheader("Low stock (one or no copies available)")
    cat = catalog_frame()
    low = cat[cat["Available"] <= 1][["Title", "Author", "Total", "Available", "Issued"]]
    table(low)


def page_catalog():
    st.title("Book catalogue")
    t1, t2, t3 = st.tabs(["Browse and search", "Add a book", "Manage copies and delete"])

    with t1:
        cat = catalog_frame()
        a, b, c = st.columns([3, 2, 2])
        q = a.text_input("Search title, author, ISBN or shelf", placeholder="e.g. clean code")
        cats = ["All"] + sorted(cat["Category"].unique().tolist()) if len(cat) else ["All"]
        cf = b.selectbox("Category", cats)
        av = c.selectbox("Availability", ["All", "Available now", "Fully issued"])
        if len(cat):
            if q:
                ql = q.lower()
                mask = (cat["Title"].str.lower().str.contains(ql) | cat["Author"].str.lower().str.contains(ql)
                        | cat["ISBN"].str.lower().str.contains(ql) | cat["Shelf"].str.lower().str.contains(ql))
                cat = cat[mask]
            if cf != "All":
                cat = cat[cat["Category"] == cf]
            if av == "Available now":
                cat = cat[cat["Available"] > 0]
            elif av == "Fully issued":
                cat = cat[cat["Available"] == 0]
        st.caption(f"{len(cat)} title(s) found")
        table(cat.drop(columns=["book_id"]) if len(cat) else cat)

    with t2:
        st.write("If the ISBN already exists, the copies are added to that title. "
                 "No duplicate record is created.")
        with st.form("add_book", clear_on_submit=True):
            a, b = st.columns(2)
            isbn = a.text_input("ISBN (10 or 13 characters)")
            title = b.text_input("Title")
            a, b = st.columns(2)
            author = a.text_input("Author")
            category = b.selectbox("Category", CATEGORIES)
            custom = st.text_input("If category is 'Other', type it here (optional)")
            a, b, c = st.columns(3)
            price = a.number_input("Price (₹)", min_value=0.0, value=300.0, step=10.0)
            shelf = b.text_input("Shelf / location", placeholder="e.g. E-02")
            copies = c.number_input("Number of copies", min_value=1, max_value=100, value=1)
            if st.form_submit_button("Add book"):
                cat_final = custom.strip() if (category == "Other" and custom.strip()) else category
                do(add_book, isbn, title, author, cat_final, price, shelf, copies)

    with t3:
        books = fetch("SELECT id, title, author, isbn FROM books WHERE archived=0 ORDER BY title")
        if not books:
            st.info("Add a book first.")
            return
        bmap = {b["id"]: f"{b['title']} - {b['author']} ({b['isbn']})" for b in books}
        bid = st.selectbox("Choose a book", list(bmap), format_func=bmap.get)
        copies_df = frame("""
            SELECT c.id AS copy_id, c.copy_no AS "Copy #", c.status AS Status,
                   m.name AS "Held by", l.due_date AS "Due date"
            FROM copies c
            LEFT JOIN loans l ON l.copy_id=c.id AND l.return_date IS NULL
            LEFT JOIN members m ON m.id=l.member_id
            WHERE c.book_id=? AND c.status != 'Withdrawn' ORDER BY c.copy_no""", (bid,))
        table(copies_df.drop(columns=["copy_id"]) if len(copies_df) else copies_df)

        a, b = st.columns(2)
        with a:
            st.markdown("**Add more copies**")
            n = st.number_input("How many?", min_value=1, max_value=100, value=1, key="addn")
            if st.button("Add copies"):
                do(add_copies, bid, n)
        with b:
            st.markdown("**Change or remove one copy**")
            if len(copies_df):
                cmap = {int(r.copy_id): f"Copy #{r['Copy #']} ({r.Status})" for _, r in copies_df.iterrows()}
                cid = st.selectbox("Copy", list(cmap), format_func=cmap.get)
                new = st.selectbox("Set status to", ["Available", "Lost", "Damaged", "Withdrawn",
                                                     "Delete this copy"])
                if st.button("Apply to copy"):
                    if new == "Delete this copy":
                        do(delete_copy, cid)
                    else:
                        do(change_copy_status, cid, new)
            else:
                st.info("No copies in circulation.")

        st.markdown("**Delete this book**")
        st.caption("Blocked while any copy is issued. If the book has loan history it is archived "
                   "instead of erased, so past records and fines stay correct.")
        sure = st.checkbox("I understand this removes the book from the catalogue")
        if st.button("Delete book", type="primary", disabled=not sure):
            do(delete_book, bid)


def page_members():
    st.title("Members")
    t1, t2, t3 = st.tabs(["All members", "Add a member", "Member profile"])
    with t1:
        df = frame("""
            SELECT m.member_code AS "Member ID", m.name AS Name, m.mtype AS Type, m.email AS Email,
                   CASE m.active WHEN 1 THEN 'Active' ELSE 'Inactive' END AS Status,
                   (SELECT COUNT(*) FROM loans l WHERE l.member_id=m.id AND l.return_date IS NULL) AS "Books held",
                   (SELECT COALESCE(SUM(fine_amount-fine_paid),0) FROM loans l
                    WHERE l.member_id=m.id AND l.fine_status='unpaid') AS "Unpaid fines"
            FROM members m ORDER BY m.member_code""")
        table(df)
    with t2:
        with st.form("add_member", clear_on_submit=True):
            a, b = st.columns(2)
            code = a.text_input("Member ID", placeholder="e.g. M007")
            name = b.text_input("Full name")
            a, b = st.columns(2)
            email = a.text_input("Email (optional)")
            mtype = b.selectbox("Member type", ["Student", "Faculty"])
            if st.form_submit_button("Add member"):
                do(add_member, code, name, email, mtype)
    with t3:
        ms = fetch("SELECT id, member_code, name, mtype, active FROM members ORDER BY member_code")
        if not ms:
            st.info("No members yet.")
            return
        mmap = {m["id"]: f"{m['member_code']} - {m['name']}" for m in ms}
        mid = st.selectbox("Choose a member", list(mmap), format_func=mmap.get)
        m = next(x for x in ms if x["id"] == mid)
        s = S()
        al = active_loan_frame(mid)
        limit = int(s[f"max_books_{m['mtype'].lower()}"])
        c = st.columns(4)
        c[0].metric("Type", m["mtype"])
        c[1].metric("Books held", f"{len(al)} / {limit}")
        c[2].metric("Unpaid fines", money(unpaid_fines(mid)))
        c[3].metric("Accruing (overdue)", money(float(al["Fine so far"].sum()) if len(al) else 0))
        st.markdown("**Currently issued**")
        table(al.drop(columns=["loan_id", "Member ID", "Member"]) if len(al) else al)
        st.markdown("**Loan history**")
        hist = frame("""
            SELECT b.title AS Title, c.copy_no AS Copy, l.issue_date AS Issued, l.due_date AS Due,
                   l.return_date AS Returned, l.fine_amount AS Fine, l.fine_status AS "Fine status"
            FROM loans l JOIN copies c ON c.id=l.copy_id JOIN books b ON b.id=c.book_id
            WHERE l.member_id=? ORDER BY l.issue_date DESC""", (mid,))
        table(hist)
        if m["active"]:
            if st.button("Deactivate this member"):
                do(set_member_active, mid, False)
        else:
            st.warning("This member is inactive and cannot borrow.")
            if st.button("Reactivate this member"):
                do(set_member_active, mid, True)


def page_circulation():
    st.title("Issue, return and renew")
    t1, t2, t3 = st.tabs(["Issue a book", "Return a book", "Renew a loan"])
    s = S()

    with t1:
        ms = fetch("SELECT id, member_code, name, mtype FROM members WHERE active=1 ORDER BY member_code")
        cat = catalog_frame()
        if not ms or not len(cat):
            st.info("You need at least one member and one book.")
        else:
            mmap = {m["id"]: f"{m['member_code']} - {m['name']} ({m['mtype']})" for m in ms}
            mid = st.selectbox("Member", list(mmap), format_func=mmap.get, key="iss_m")
            bmap = {int(r.book_id): f"{r.Title} - {r.Author}  [{int(r.Available)} available]"
                    for _, r in cat.iterrows()}
            bid = st.selectbox("Book", list(bmap), format_func=bmap.get, key="iss_b")
            m = next(x for x in ms if x["id"] == mid)
            kind = m["mtype"].lower()
            held = len(active_loans(mid))
            due = today() + dt.timedelta(days=int(s[f"loan_days_{kind}"]))
            st.caption(f"{m['name']} holds {held} of {int(s[f'max_books_{kind}'])} allowed books. "
                       f"If issued today, the due date is {due:%d %b %Y}.")
            if st.button("Issue book", type="primary"):
                res = do(issue_book, mid, bid, rerun=False)
                if res:
                    flash("success", f"Issued '{res['title']}' (copy #{res['copy_no']}) to {res['member']}. "
                                     f"Due {res['due']:%d %b %Y}.")
                    st.rerun()

    with t2:
        al = active_loans()
        if not al:
            st.info("No books are currently issued.")
        else:
            lmap = {}
            for l in al:
                late = " [OVERDUE]" if dt.date.fromisoformat(l["due_date"]) < today() else ""
                lmap[l["loan_id"]] = (f"{l['member_name']} - {l['title']} (copy #{l['copy_no']}) "
                                      f"due {l['due_date']}{late}")
            lid = st.selectbox("Issued book", list(lmap), format_func=lmap.get, key="ret_l")
            loan = next(x for x in al if x["loan_id"] == lid)
            cond = st.radio("Condition on return", ["Good", "Damaged", "Lost"], horizontal=True)
            days, fine, reason = preview_return(loan, cond)
            if fine > 0:
                st.warning(f"Fine to charge: {money(fine)}. {reason}")
            else:
                st.success("No fine. Returned on time.")
            if st.button("Process return", type="primary"):
                res = do(return_loan, lid, cond, rerun=False)
                if res:
                    msg = f"'{res['title']}' marked {cond.lower()}."
                    if res["fine"] > 0:
                        msg += f" Fine of {money(res['fine'])} added to the member's account."
                    flash("success", msg)
                    st.rerun()

    with t3:
        al = [l for l in active_loans()]
        if not al:
            st.info("No books are currently issued.")
        else:
            lmap = {l["loan_id"]: f"{l['member_name']} - {l['title']} (due {l['due_date']}, "
                                  f"renewed {l['renewals']}/{int(s['max_renewals'])})" for l in al}
            lid = st.selectbox("Loan to renew", list(lmap), format_func=lmap.get, key="ren_l")
            st.caption("Overdue books cannot be renewed. Each renewal extends the due date by one loan period.")
            if st.button("Renew loan"):
                res = do(renew_loan, lid, rerun=False)
                if res:
                    flash("success", f"Renewed. New due date: {res:%d %b %Y}.")
                    st.rerun()


def page_fines():
    st.title("Fines")
    row = fetch("SELECT COALESCE(SUM(fine_amount),0) billed, COALESCE(SUM(fine_paid),0) paid, "
                "COALESCE(SUM(CASE WHEN fine_status='waived' THEN fine_amount-fine_paid ELSE 0 END),0) waived, "
                "COALESCE(SUM(CASE WHEN fine_status='unpaid' THEN fine_amount-fine_paid ELSE 0 END),0) owed "
                "FROM loans")[0]
    c = st.columns(4)
    c[0].metric("Total billed", money(row["billed"]))
    c[1].metric("Collected", money(row["paid"]))
    c[2].metric("Waived", money(row["waived"]))
    c[3].metric("Outstanding", money(row["owed"]))

    unpaid = fetch(LOAN_SQL + " WHERE l.fine_status='unpaid' ORDER BY l.return_date")
    st.subheader("Unpaid fines")
    if unpaid:
        table(pd.DataFrame([{"Member": u["member_name"], "Title": u["title"], "Reason": u["fine_reason"],
                             "Fine": u["fine_amount"], "Paid": u["fine_paid"],
                             "Outstanding": u["fine_amount"] - u["fine_paid"]} for u in unpaid]))
    else:
        st.success("No unpaid fines.")

    st.subheader("Still accruing (books not yet returned)")
    al = active_loan_frame()
    acc = al[al["Days late"] > 0] if len(al) else al
    table(acc[["Member", "Title", "Due", "Days late", "Fine so far"]] if len(acc) else acc)
    st.caption("These become real fines when the book is returned.")

    if unpaid:
        umap = {u["loan_id"]: f"{u['member_name']} - {u['title']} (owes "
                              f"{money(u['fine_amount'] - u['fine_paid'])})" for u in unpaid}
        a, b = st.columns(2)
        with a:
            st.subheader("Collect a payment")
            lid = st.selectbox("Fine", list(umap), format_func=umap.get, key="pay_l")
            u = next(x for x in unpaid if x["loan_id"] == lid)
            owed = float(u["fine_amount"] - u["fine_paid"])
            amt = st.number_input("Amount received (₹)", min_value=0.0, value=owed, step=1.0, key=f"amt{lid}")
            if st.button("Record payment", type="primary"):
                do(pay_fine, lid, amt)
        with b:
            st.subheader("Waive a fine")
            lid2 = st.selectbox("Fine ", list(umap), format_func=umap.get, key="wv_l")
            why = st.text_input("Reason (required)", placeholder="e.g. library closed, medical emergency")
            if st.button("Waive fine"):
                do(waive_fine, lid2, why)


def page_reports():
    st.title("Reports")
    t1, t2, t3, t4 = st.tabs(["Loan history", "Overdue", "Inventory", "Activity log"])
    with t1:
        hist = frame("""
            SELECT l.id AS "Loan", m.member_code AS "Member ID", m.name AS Member, b.title AS Title,
                   c.copy_no AS Copy, l.issue_date AS Issued, l.due_date AS Due,
                   l.return_date AS Returned, l.fine_amount AS Fine, l.fine_status AS "Fine status"
            FROM loans l JOIN members m ON m.id=l.member_id JOIN copies c ON c.id=l.copy_id
            JOIN books b ON b.id=c.book_id ORDER BY l.id DESC""")
        f = st.selectbox("Show", ["All loans", "Open (not returned)", "Returned"])
        if f == "Open (not returned)":
            hist = hist[hist["Returned"].isna()]
        elif f == "Returned":
            hist = hist[hist["Returned"].notna()]
        table(hist)
        if len(hist):
            st.download_button("Download CSV", hist.to_csv(index=False), "loan_history.csv", "text/csv")
    with t2:
        al = active_loan_frame()
        od = al[al["Days late"] > 0].drop(columns=["loan_id"]) if len(al) else al
        table(od)
        if len(od):
            st.download_button("Download CSV", od.to_csv(index=False), "overdue.csv", "text/csv", key="odcsv")
    with t3:
        cat = catalog_frame().drop(columns=["book_id"])
        table(cat)
        if len(cat):
            st.download_button("Download CSV", cat.to_csv(index=False), "inventory.csv", "text/csv", key="invcsv")
    with t4:
        table(frame("SELECT ts AS Time, action AS Action, detail AS Detail FROM log ORDER BY id DESC LIMIT 200"))


def page_settings():
    st.title("Settings")
    s = S()
    st.subheader("Library rules")
    with st.form("settings"):
        a, b = st.columns(2)
        fpd = a.number_input("Fine per day late (₹)", min_value=0.0, value=float(s["fine_per_day"]), step=1.0)
        cap = b.number_input("Fine cap (% of book price)", min_value=0.0, value=float(s["fine_cap_pct"]), step=10.0)
        a, b = st.columns(2)
        lds = a.number_input("Loan days: students", min_value=1, value=int(s["loan_days_student"]))
        ldf = b.number_input("Loan days: faculty", min_value=1, value=int(s["loan_days_faculty"]))
        a, b = st.columns(2)
        mbs = a.number_input("Max books: students", min_value=1, value=int(s["max_books_student"]))
        mbf = b.number_input("Max books: faculty", min_value=1, value=int(s["max_books_faculty"]))
        a, b = st.columns(2)
        mr = a.number_input("Max renewals per loan", min_value=0, value=int(s["max_renewals"]))
        bt = b.number_input("Block new issues at unpaid fines of (₹)", min_value=0.0,
                            value=float(s["block_fine_threshold"]), step=10.0)
        a, b = st.columns(2)
        lf = a.number_input("Lost book processing fee (₹)", min_value=0.0, value=float(s["lost_fee"]), step=10.0)
        dp = b.number_input("Damage charge (% of price)", min_value=0.0, value=float(s["damage_fee_pct"]), step=5.0)
        if st.form_submit_button("Save rules"):
            new = {"fine_per_day": fpd, "fine_cap_pct": cap, "loan_days_student": lds,
                   "loan_days_faculty": ldf, "max_books_student": mbs, "max_books_faculty": mbf,
                   "max_renewals": mr, "block_fine_threshold": bt, "lost_fee": lf, "damage_fee_pct": dp}
            with db() as con:
                for k, v in new.items():
                    con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, str(v)))
                log(con, "SETTINGS", "Library rules updated")
            flash("success", "Rules saved.")
            st.rerun()

    st.subheader("Demo data and reset")
    empty = fetch("SELECT COUNT(*) n FROM books")[0]["n"] == 0
    if st.button("Load demo data", disabled=not empty,
                 help="Only available on an empty database"):
        seed_demo()
        flash("success", "Demo data loaded: 11 titles, 6 members, loans, an overdue book, a lost book.")
        st.rerun()
    if not empty:
        st.caption("Demo data can only be loaded into an empty database.")
    sure = st.checkbox("I want to erase ALL data")
    if st.button("Reset database", disabled=not sure):
        reset_db()
        flash("warning", "Database reset.")
        st.rerun()


PAGES = {
    "Dashboard": page_dashboard,
    "Catalogue": page_catalog,
    "Members": page_members,
    "Issue and Return": page_circulation,
    "Fines": page_fines,
    "Reports": page_reports,
    "Settings": page_settings,
}


def main():
    st.set_page_config(page_title="Library Inventory Manager", page_icon="📚", layout="wide")
    init_db()
    with st.sidebar:
        st.title("📚 Library Manager")
        page = st.radio("Go to", list(PAGES), label_visibility="collapsed")
        st.divider()
        st.caption(f"Today: {today():%A, %d %B %Y}")
    show_flash()
    PAGES[page]()


if not os.environ.get("LIB_NO_UI"):
    main()
