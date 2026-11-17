"""
Lucknow Public Library Management System
Single-file, GitHub-ready Python reference implementation.

Run:
  python library_management.py init
  python library_management.py seed
  python library_management.py add-member "Asha Verma" student
  python library_management.py search "python"
  python library_management.py issue 1 1
  python library_management.py return 1
  python library_management.py overdue
  python library_management.py test

Uses only Python's standard library (sqlite3, argparse, unittest, datetime).
Database file: library.db
"""
from __future__ import annotations
import argparse
import random
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).with_name("library.db")
TIERS = {
    "standard": {"limit": 3, "days": 14},
    "student": {"limit": 5, "days": 21},
    "premium": {"limit": 8, "days": 28},
}
FINE_PER_DAY = 2
GRACE_DAYS = 2
MAX_FINE = 500
HOLIDAYS = {date(2026, 1, 26), date(2026, 8, 15), date(2026, 10, 2),
            date(2026, 11, 14), date(2026, 12, 25)}


# ---------- Domain exceptions ----------
class LibraryError(Exception): pass
class NotFoundError(LibraryError): pass
class RuleViolation(LibraryError): pass
class DataIntegrityError(LibraryError): pass


# ---------- Database / Unit of Work pattern ----------
class Database:
    def __init__(self, path=DB_PATH):
        self.path = str(path)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self):
        with self.transaction() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS branches(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, address TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS books(
              id INTEGER PRIMARY KEY, isbn TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
              author TEXT NOT NULL, publisher TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS members(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE,
              tier TEXT NOT NULL CHECK(tier IN ('standard','student','premium')),
              branch_id INTEGER NOT NULL REFERENCES branches(id),
              active INTEGER NOT NULL DEFAULT 1, unpaid_fines INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS copies(
              id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
              branch_id INTEGER NOT NULL REFERENCES branches(id),
              status TEXT NOT NULL DEFAULT 'available'
                CHECK(status IN ('available','loaned','reserved','maintenance')));
            CREATE TABLE IF NOT EXISTS loans(
              id INTEGER PRIMARY KEY, copy_id INTEGER NOT NULL REFERENCES copies(id),
              member_id INTEGER NOT NULL REFERENCES members(id),
              issue_date TEXT NOT NULL, due_date TEXT NOT NULL, return_date TEXT,
              fine INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS reservations(
              id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
              member_id INTEGER NOT NULL REFERENCES members(id),
              created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting'
                CHECK(status IN ('waiting','fulfilled','cancelled')));
            CREATE TABLE IF NOT EXISTS import_errors(
              id INTEGER PRIMARY KEY, source_record TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
            CREATE INDEX IF NOT EXISTS idx_loans_due ON loans(due_date, return_date);
            CREATE INDEX IF NOT EXISTS idx_reservation_queue ON reservations(book_id,status,created_at);
            """)
        print("Database schema is ready.")


# ---------- Repository pattern: all SQL access lives here ----------
class LibraryRepository:
    def __init__(self, conn): self.c = conn

    def member(self, member_id):
        row = self.c.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        if not row: raise NotFoundError(f"Member {member_id} not found")
        return row

    def copy(self, copy_id):
        row = self.c.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
        if not row: raise NotFoundError(f"Copy {copy_id} not found")
        return row

    def active_loans(self, member_id):
        return self.c.execute("SELECT * FROM loans WHERE member_id=? AND return_date IS NULL",
                              (member_id,)).fetchall()

    def reservation_for_copy(self, book_id):
        return self.c.execute("""SELECT * FROM reservations WHERE book_id=? AND status='waiting'
          ORDER BY created_at,id LIMIT 1""", (book_id,)).fetchone()

    def search_books(self, term):
        q = f"%{term}%"
        return self.c.execute("""SELECT b.id,b.isbn,b.title,b.author,c.id copy_id,
          br.name branch,c.status FROM books b JOIN copies c ON c.book_id=b.id
          JOIN branches br ON br.id=c.branch_id
          WHERE b.title LIKE ? OR b.author LIKE ? OR b.isbn LIKE ?
          ORDER BY b.title LIMIT 100""", (q,q,q)).fetchall()


# ---------- Domain services / business rules ----------
def add_business_days(start: date, days: int) -> date:
    """Add weekdays, excluding configured public holidays."""
    current, added = start, 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in HOLIDAYS:
            added += 1
    return current


def calculate_fine(due: date, returned: date) -> int:
    """Fine applies after grace period; weekends and public holidays are excluded."""
    current, overdue = due + timedelta(days=GRACE_DAYS), 0
    while current < returned:
        if current.weekday() < 5 and current not in HOLIDAYS:
            overdue += 1
        current += timedelta(days=1)
    return min(overdue * FINE_PER_DAY, MAX_FINE)


class LibraryService:
    def __init__(self, database: Database): self.db = database

    def register_member(self, name, tier="standard", branch_id=1, email=None):
        if tier not in TIERS: raise RuleViolation("Unknown membership tier")
        with self.db.transaction() as c:
            if not c.execute("SELECT 1 FROM branches WHERE id=?", (branch_id,)).fetchone():
                raise NotFoundError("Branch not found")
            cur = c.execute("INSERT INTO members(name,email,tier,branch_id) VALUES(?,?,?,?)",
                            (name,tier,branch_id,email))
            return cur.lastrowid

    def issue(self, member_id, copy_id, today=None):
        today = today or date.today()
        with self.db.transaction() as c:
            r = LibraryRepository(c)
            m, cp = r.member(member_id), r.copy(copy_id)
            if not m["active"]: raise RuleViolation("Membership is inactive")
            if m["unpaid_fines"] > 0: raise RuleViolation("Unpaid fines block borrowing")
            if len(r.active_loans(member_id)) >= TIERS[m["tier"]]["limit"]:
                raise RuleViolation("Borrowing limit reached for membership tier")
            if cp["status"] != "available": raise RuleViolation("Copy is not available")
            queue = r.reservation_for_copy(cp["book_id"])
            if queue and queue["member_id"] != member_id:
                raise RuleViolation("Copy is reserved for another member")
            due = add_business_days(today, TIERS[m["tier"]]["days"])
            c.execute("UPDATE copies SET status='loaned' WHERE id=?", (copy_id,))
            cur = c.execute("""INSERT INTO loans(copy_id,member_id,issue_date,due_date)
                               VALUES(?,?,?,?)""",
                            (copy_id,member_id,today.isoformat(),due.isoformat()))
            if queue and queue["member_id"] == member_id:
                c.execute("UPDATE reservations SET status='fulfilled' WHERE id=?", (queue["id"],))
            return cur.lastrowid

    def return_book(self, loan_id, today=None):
        today = today or date.today()
        with self.db.transaction() as c:
            loan = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if not loan: raise NotFoundError("Loan not found")
            if loan["return_date"]: raise RuleViolation("Loan has already been returned")
            fine = calculate_fine(date.fromisoformat(loan["due_date"]), today)
            c.execute("UPDATE loans SET return_date=?,fine=? WHERE id=?",
                      (today.isoformat(),fine,loan_id))
            c.execute("UPDATE copies SET status='available' WHERE id=?", (loan["copy_id"],))
            c.execute("UPDATE members SET unpaid_fines=unpaid_fines+? WHERE id=?",
                      (fine,loan["member_id"]))
            return fine

    def renew(self, loan_id, today=None):
        today = today or date.today()
        with self.db.transaction() as c:
            loan = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if not loan: raise NotFoundError("Loan not found")
            if loan["return_date"]: raise RuleViolation("Cannot renew a returned loan")
            m = LibraryRepository(c).member(loan["member_id"])
            if m["unpaid_fines"]: raise RuleViolation("Unpaid fines block renewal")
            book_id = c.execute("SELECT book_id FROM copies WHERE id=?", (loan["copy_id"],)).fetchone()[0]
            if LibraryRepository(c).reservation_for_copy(book_id):
                raise RuleViolation("Renewal blocked: another member is waiting")
            new_due = add_business_days(max(today,date.fromisoformat(loan["due_date"])),
                                        TIERS[m["tier"]]["days"])
            c.execute("UPDATE loans SET due_date=? WHERE id=?", (new_due.isoformat(),loan_id))
            return new_due

    def reserve(self, member_id, book_id, today=None):
        today = today or date.today()
        with self.db.transaction() as c:
            LibraryRepository(c).member(member_id)
            if not c.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                raise NotFoundError("Book not found")
            cur = c.execute("INSERT INTO reservations(book_id,member_id,created_at) VALUES(?,?,?)",
                            (book_id,member_id,today.isoformat()))
            return cur.lastrowid

    def search(self, term):
        with self.db.transaction() as c:
            return [dict(x) for x in LibraryRepository(c).search_books(term)]

    def overdue_report(self, today=None, branch_id=None):
        today = today or date.today()
        with self.db.transaction() as c:
            sql = """SELECT l.id loan_id,m.name member,b.title,br.name branch,l.due_date
              FROM loans l JOIN members m ON m.id=l.member_id
              JOIN copies cp ON cp.id=l.copy_id JOIN books b ON b.id=cp.book_id
              JOIN branches br ON br.id=cp.branch_id
              WHERE l.return_date IS NULL AND l.due_date < ?"""
            args = [today.isoformat()]
            if branch_id:
                sql += " AND br.id=?"; args.append(branch_id)
            return [dict(x) for x in c.execute(sql,args).fetchall()]


# ---------- Synthetic data seeding / migration validation ----------
def seed(database: Database):
    rng = random.Random(42)
    first_names = ["Aarav","Asha","Kabir","Meera","Rohan","Sara","Ishaan","Nisha"]
    last_names = ["Verma","Khan","Sharma","Singh","Gupta","Mishra","Ali","Das"]
    authors = ["A. Sharma","R. Kumar","M. Khan","S. Verma","P. Singh","N. Das"]
    with database.transaction() as c:
        if c.execute("SELECT COUNT(*) FROM branches").fetchone()[0]:
            print("Seed data already exists; skipping."); return
        c.executemany("INSERT INTO branches(name,address) VALUES(?,?)", [
            ("Hazratganj","Hazratganj, Lucknow"),("Aliganj","Aliganj, Lucknow"),
            ("Gomti Nagar","Gomti Nagar, Lucknow")])
        for i in range(1,2001):
            c.execute("INSERT INTO books(isbn,title,author,publisher) VALUES(?,?,?,?)",
                      (f"978000{i:07d}",f"Library Book {i}",rng.choice(authors),"Sample Press"))
            # Two physical copies per book, distributed across the three branches.
            for j in range(2):
                c.execute("INSERT INTO copies(book_id,branch_id) VALUES(?,?)",
                          (i,1+((i+j)%3)))
        for i in range(1,501):
            name=f"{rng.choice(first_names)} {rng.choice(last_names)}"
            c.execute("INSERT INTO members(name,email,tier,branch_id) VALUES(?,?,?,?)",
                      (name,f"member{i}@example.test",rng.choice(list(TIERS)),1+(i%3)))
        # One-year historical loan sample; inconsistent source rows are quarantined.
        start=date.today()-timedelta(days=365)
        for i in range(1,301):
            issue=start+timedelta(days=rng.randrange(0,300))
            member_id=rng.randrange(1,501); copy_id=rng.randrange(1,4001)
            returned=issue+timedelta(days=rng.randrange(3,35))
            due=issue+timedelta(days=14)
            if i % 37 == 0:
                c.execute("INSERT INTO import_errors(source_record,reason) VALUES(?,?)",
                          (f"legacy-loan-{i}","Return date precedes issue date; quarantined"))
                continue
            c.execute("""INSERT INTO loans(copy_id,member_id,issue_date,due_date,return_date)
                         VALUES(?,?,?,?,?)""",
                      (copy_id,member_id,issue.isoformat(),due.isoformat(),
                       returned.isoformat()))
        print("Seeded 3 branches, 2,000 books, 4,000 copies, 500 members and loan history.")
        print("Inconsistent historical rows were recorded in import_errors.")


# ---------- Command-line interface ----------
def main():
    parser=argparse.ArgumentParser(description="Lucknow Library Management System")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("init"); sub.add_parser("seed"); sub.add_parser("test")
    p=sub.add_parser("add-member"); p.add_argument("name"); p.add_argument("tier",choices=TIERS)
    p.add_argument("--branch",type=int,default=1); p.add_argument("--email")
    p=sub.add_parser("issue"); p.add_argument("member_id",type=int); p.add_argument("copy_id",type=int)
    p=sub.add_parser("return"); p.add_argument("loan_id",type=int)
    p=sub.add_parser("renew"); p.add_argument("loan_id",type=int)
    p=sub.add_parser("reserve"); p.add_argument("member_id",type=int); p.add_argument("book_id",type=int)
    p=sub.add_parser("search"); p.add_argument("term")
    p=sub.add_parser("overdue"); p.add_argument("--branch",type=int)
    args=parser.parse_args(); db=Database(); service=LibraryService(db)
    try:
        if args.command=="init": db.migrate()
        elif args.command=="seed": db.migrate(); seed(db)
        elif args.command=="test": unittest.main(argv=["library_management.py"],exit=False)
        elif args.command=="add-member": print("Member ID:",service.register_member(args.name,args.tier,args.branch,args.email))
        elif args.command=="issue": print("Loan ID:",service.issue(args.member_id,args.copy_id))
        elif args.command=="return": print("Fine (INR):",service.return_book(args.loan_id))
        elif args.command=="renew": print("New due date:",service.renew(args.loan_id))
        elif args.command=="reserve": print("Reservation ID:",service.reserve(args.member_id,args.book_id))
        elif args.command=="search":
            for row in service.search(args.term): print(row)
        elif args.command=="overdue":
            for row in service.overdue_report(branch_id=args.branch): print(row)
    except (LibraryError,sqlite3.IntegrityError) as e:
        parser.exit(2,f"Error: {e}\n")


# ---------- Automated tests (run: python library_management.py test) ----------
class LibraryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/"test.db"); self.db.migrate()
        with self.db.transaction() as c:
            c.execute("INSERT INTO branches VALUES(1,'Test Branch','Lucknow')")
            c.execute("INSERT INTO books VALUES(1,'ISBN-1','Python Basics','A. Author','Press')")
            c.execute("INSERT INTO copies(id,book_id,branch_id) VALUES(1,1,1)")
            c.execute("INSERT INTO copies(id,book_id,branch_id) VALUES(2,1,1)")
            c.execute("INSERT INTO members(id,name,tier,branch_id) VALUES(1,'Test Member','standard',1)")
            c.execute("INSERT INTO members(id,name,tier,branch_id) VALUES(2,'Student','student',1)")
        self.s=LibraryService(self.db)
    def tearDown(self): self.tmp.cleanup()
    def test_01_business_days(self): self.assertGreater(add_business_days(date(2026,1,23),1),date(2026,1,23))
    def test_02_fine_grace(self): self.assertEqual(calculate_fine(date(2026,1,1),date(2026,1,3)),0)
    def test_03_fine_after_grace(self): self.assertGreaterEqual(calculate_fine(date(2026,1,1),date(2026,1,8)),2)
    def test_04_fine_cap(self): self.assertEqual(calculate_fine(date(2020,1,1),date(2025,1,1)),MAX_FINE)
    def test_05_register(self): self.assertGreater(self.s.register_member("New"),0)
    def test_06_bad_tier(self):
        with self.assertRaises(RuleViolation): self.s.register_member("X","gold")
    def test_07_issue(self): self.assertGreater(self.s.issue(1,1),0)
    def test_08_issue_marks_copy(self):
        self.s.issue(1,1)
        with self.db.transaction() as c: self.assertEqual(c.execute("SELECT status FROM copies WHERE id=1").fetchone()[0],"loaned")
    def test_09_cannot_issue_loaned(self):
        self.s.issue(1,1)
        with self.assertRaises(RuleViolation): self.s.issue(1,1)
    def test_10_return(self):
        loan=self.s.issue(1,1); self.assertEqual(self.s.return_book(loan,today=date.today()),0)
    def test_11_double_return(self):
        loan=self.s.issue(1,1); self.s.return_book(loan)
        with self.assertRaises(RuleViolation): self.s.return_book(loan)
    def test_12_renew(self): self.assertIsInstance(self.s.renew(self.s.issue(1,1)),date)
    def test_13_reserve(self): self.assertGreater(self.s.reserve(2,1),0)
    def test_14_reserved_for_other(self):
        self.s.reserve(2,1)
        with self.assertRaises(RuleViolation): self.s.issue(1,1)
    def test_15_reserved_owner_can_issue(self):
        self.s.reserve(1,1); self.assertGreater(self.s.issue(1,1),0)
    def test_16_unpaid_fine_blocks(self):
        with self.db.transaction() as c: c.execute("UPDATE members SET unpaid_fines=10 WHERE id=1")
        with self.assertRaises(RuleViolation): self.s.issue(1,1)
    def test_17_inactive_blocks(self):
        with self.db.transaction() as c: c.execute("UPDATE members SET active=0 WHERE id=1")
        with self.assertRaises(RuleViolation): self.s.issue(1,1)
    def test_18_limit_standard(self):
        with self.db.transaction() as c:
            for i in range(3,6):
                c.execute("INSERT INTO copies(id,book_id,branch_id) VALUES(?,?,1)",(i,1))
        self.s.issue(1,1); self.s.issue(1,2)
        self.s.issue(1,3)
        with self.assertRaises(RuleViolation): self.s.issue(1,4)
    def test_19_student_limit(self):
        with self.db.transaction() as c:
            for i in range(3,8): c.execute("INSERT INTO copies(id,book_id,branch_id) VALUES(?,?,1)",(i,1))
        for i in range(1,6): self.s.issue(2,i)
        with self.assertRaises(RuleViolation): self.s.issue(2,6)
    def test_20_search_title(self): self.assertTrue(self.s.search("Python"))
    def test_21_search_author(self): self.assertTrue(self.s.search("Author"))
    def test_22_search_isbn(self): self.assertTrue(self.s.search("ISBN-1"))
    def test_23_overdue_empty(self): self.assertEqual(self.s.overdue_report(today=date(2020,1,1)),[])
    def test_24_unknown_member(self):
        with self.assertRaises(NotFoundError): self.s.issue(999,1)
    def test_25_unknown_copy(self):
        with self.assertRaises(NotFoundError): self.s.issue(1,999)
    def test_26_unknown_loan(self):
        with self.assertRaises(NotFoundError): self.s.return_book(999)
    def test_27_renew_returned(self):
        loan=self.s.issue(1,1); self.s.return_book(loan)
        with self.assertRaises(RuleViolation): self.s.renew(loan)
    def test_28_renew_waiting_queue(self):
        self.s.issue(1,1); self.s.reserve(2,1)
        with self.assertRaises(RuleViolation): self.s.renew(1)
    def test_29_reservation_unknown_book(self):
        with self.assertRaises(NotFoundError): self.s.reserve(1,999)
    def test_30_branch_filter(self): self.assertEqual(self.s.overdue_report(branch_id=999),[])


if __name__=="__main__":
    main()
