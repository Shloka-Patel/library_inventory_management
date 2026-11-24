# Design notes: Library Inventory Management

## 1. Assumptions (stated because the brief is open-ended)
- One small library (school or college). Currency is Indian rupees.
- A title is identified by its ISBN. The same ISBN means the same title.
- Two kinds of borrowers, students and faculty, with different limits.
- Fines are charged per day late and only when the book is returned. Until
  then the app shows the fine "accruing" without billing it.
- All rules (fine per day, loan days, limits) live on the Settings page, not in the code.

## 2. The central design decision
A **book title** and a **physical copy** are different things. "Wings of Fire"
is one title, but the library may own three copies. Each copy has its own
number and status, so the system always knows *which* copy a member holds,
which copies are on the shelf, and which one was lost.

## 3. Why these features (reasoning)
| Feature | Why a real library needs it |
|---|---|
| Add book with copies, ISBN duplicate check | Staff add stock in batches. Typing the same ISBN twice should add copies, not create a second record. |
| Copy-level status (Available / Issued / Lost / Damaged / Withdrawn) | "Quantity: 3" cannot say that one copy is lost or being repaired. Statuses keep the counts honest. |
| Delete or archive | A book with history cannot be erased without breaking old records and fines, so it is archived. A book never lent is deleted for real. |
| Members, student and faculty | Different loan periods and limits are the most common real rule. |
| Issue with checks | Stops the common problems before they happen: no copy left, limit reached, overdue book, unpaid fines, same title twice. |
| Return with condition | A return is not always "fine". Books come back damaged or are reported lost, and each has a different charge. |
| Renew | Very common; allowing it for overdue books would make fines meaningless, so it is blocked. |
| Fines: collect, part-pay, waive with reason | Money handling needs part payments and a recorded reason for any waiver. |
| Dashboard | The librarian's first question each day: what is overdue, what is low in stock? |
| Reports and CSV export | Needed for stock-taking and reporting to the head of department. |
| Activity log | Every issue, return, fine and waiver is recorded, so any dispute can be traced. |
| Settings page | Rules change between libraries and years. No code change is needed. |

## 4. Scenarios accounted for
**Catalogue**
1. Add a new title with several copies (copy numbers are created automatically).
2. Add a title whose ISBN already exists: copies are added to the existing title.
3. Reject an invalid ISBN, a missing title or author, a negative price, or 0 or more than 100 copies.
4. Search by title, author, ISBN or shelf; filter by category and availability.
5. Add more copies to an existing title later.
6. Mark a copy Lost, Damaged or Withdrawn; set it back to Available if it is found or repaired.
7. Delete one copy (deleted if it has no history, otherwise withdrawn).
8. Delete a book never lent: removed permanently.
9. Delete a book with loan history: archived, so history and fines stay correct.
10. Block deleting a book or copy while it is issued.
11. Re-adding the ISBN of an archived book brings it back.

**Members**
12. Add a member; reject a duplicate member ID or a bad email.
13. Student and faculty have different loan periods and limits.
14. View a member's profile: books held, unpaid fines, accruing fines, full history.
15. Deactivate a member (blocked while they hold books or owe fines); reactivate later.
16. Inactive members cannot borrow.

**Issuing**
17. The due date is calculated from the member type.
18. The first available copy is chosen automatically.
19. Block when no copy is available (all issued, lost or damaged).
20. Block when the member has reached their book limit.
21. Block when the member has an overdue book.
22. Block when unpaid fines reach the limit.
23. Block the same member taking two copies of the same title.

**Returning and renewing**
24. Return on time: no fine.
25. Return late: fine per day, capped at a percentage of the book's price.
26. Return damaged: damage charge plus any late fine; the copy is marked Damaged.
27. Report lost: price plus a processing fee; the copy is marked Lost.
28. Fine preview is shown before the return is confirmed.
29. Renew a loan up to the renewal limit; blocked if overdue.

**Fines**
30. Fines accrue on overdue books that are not yet returned and are shown separately.
31. Collect a full payment or a part payment; block overpayment.
32. Waive a fine; a reason is mandatory and is logged.
33. Unpaid fines block new issues until cleared.

**Reports and administration**
34. Loan history (all, open, returned), overdue report, inventory report, all with CSV export.
35. Activity log of every action.
36. Library rules changed from the Settings page.
37. Data is saved in a database file, so nothing is lost when the app restarts.
38. Demo data and reset, for testing.

## 5. Not included (deliberately)
Reservations or hold queues, barcode scanning, email or SMS reminders, and
multiple branches. They are natural next steps, but the brief asks for the
common, frequent cases first.
