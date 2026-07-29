"""Data access, one layer (ARCH-4).

Plain functions in, dicts out. **Not an ORM** — 8 runtime dependencies stay 8, and every
function here is a named SQL statement you can read in one screen.

Why it exists: 21 modules were executing SQL directly, `web_dashboard.py` alone holding 39
statements — the same order as `store.py`, whose entire job is SQL. A schema change meant
grepping 21 files, and there was no single place to add caching, logging or a query budget.
The measured cost of having no such place: `/api/status` was running 313 statements per
request before anyone counted (see `tests/test_query_budget.py`).

Boundaries:
  - `database.py` still owns connections, WAL and migrations. `repo/` sits on top.
  - `networking/store.py` and `networking/touches.py` already ARE repositories for their
    tables. They are not wrapped again here — a second abstraction over the same table is
    exactly the thing the ticket warns against.
  - `job_events` likewise: `database.log_event` / `database.get_job_events` already own it,
    so there is deliberately no `repo/events.py`. Wrapping two working functions to satisfy
    a filename in the ticket would add indirection and nothing else.
"""

from applypilot.repo import jobs

__all__ = ["jobs"]
