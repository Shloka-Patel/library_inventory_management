# Library Inventory Management System

A small library management app: catalogue with multiple copies, members,
issue / return / renew, fines, reports and an activity log.

**Stack:** Python, Streamlit (UI), SQLite (database file `library.db`).

## Run it
```
pip install -r requirements.txt
streamlit run app.py
```
Then open **Settings > Load demo data** to fill it with sample books, members,
an overdue book and a lost book.

## How it is organised
- `books`   : one row per title (ISBN, author, price, shelf)
- `copies`  : one row per physical copy, each with its own status
  (Available, Issued, Lost, Damaged, Withdrawn)
- `members` : students and faculty
- `loans`   : every issue and return, including the fine charged
- `settings`, `log` : library rules and an audit trail

See `DESIGN_NOTES.md` for why these features were chosen and the full list of
scenarios handled.
