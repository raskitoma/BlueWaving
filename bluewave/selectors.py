"""Selector catalog for BlueWeb v20 Selenium driving (spec §2, M3 deliverable).

Each module-level constant is a :class:`Selector` instance documenting the
element it identifies. Calling code refers to a selector **by name** — strings
do not appear inline elsewhere. This keeps the migration cost low when
BlueWeb's HTML changes after an upgrade.

**Verification status:** the strings below are *initial best-guesses* derived
from the v20 screenshots. They must be confirmed against the live instance
when the smoke test in ``tests/test_m3_smoke.py`` is run for the first time.
Once verified, update the ``# verified: YYYY-MM-DD`` comment on each entry.

**Denylist (spec §2):** the worker never interacts with anything listed in
:data:`DENYLIST`. ``SafeDriver`` (spec §6 / ``bluewave/driver.py``) enforces
this at runtime — a code path attempting to resolve a denylisted selector
raises :class:`bluewave.exceptions.DenylistedSelector`.

Selectors for later milestones (S3/S4/S6/S7 — report navigation, date inputs,
Get Report / CSV buttons, results table) are intentionally **not** defined
here yet. They land with their owning milestone (M4) so the
"every selector used in exactly one call site" invariant from §10/M3 is
preserved at every milestone boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

from selenium.webdriver.common.by import By


@dataclass(frozen=True)
class Selector:
    """Tuple of (locator strategy, locator string, human description).

    Frozen + hashable so denylist membership tests are O(1) and
    selector identity is safe across modules.
    """

    by: str
    value: str
    description: str


# ---------------------------------------------------------------------------
# S1 — Login page
# ---------------------------------------------------------------------------

# verified: 2026-05-14 against live BlueWeb v20 (Easy Foods Inc.).
LOGIN_USERNAME = Selector(
    By.ID,
    "tbUserName",
    "User Name input on the BlueWeb login form",
)

# verified: 2026-05-14
LOGIN_PASSWORD = Selector(
    By.ID,
    "tbPassword",
    "Password input on the BlueWeb login form",
)

# Not provided by the operator — falling back to a permissive XPath PLUS
# the Enter-key fallback in login.py. ASP.NET login forms almost always
# submit on Enter in the password field, so this is robust even if the
# button selector misses.
LOGIN_SUBMIT = Selector(
    By.XPATH,
    "//*[@id='btnLogin' or @id='Login' or @id='btnSubmit'] | "
    "//button[normalize-space()='Login'] | "
    "//input[(@type='submit' or @type='button' or @type='image') "
    "        and (@value='Login' or normalize-space(@value)='Login' "
    "             or @alt='Login')] | "
    "//a[normalize-space()='Login' and (@onclick or @href)]",
    "Login submit control (best-guess; Enter-key fallback in login.py)",
)

# ---------------------------------------------------------------------------
# S2 — Main menu (after successful login)
#
# We detect login *success* by the appearance of the Reports menu entry — it
# is the destination we need anyway, and it's a more reliable signal than
# any specific welcome text (which can drift by theme/localization).
# ---------------------------------------------------------------------------

# verified: 2026-05-14
MAIN_MENU_REPORTS = Selector(
    By.ID,
    "btnReports_quicklinks",
    "Reports button (Quick Links bar) — present on the main menu and "
    "persists across post-login pages",
)

# ---------------------------------------------------------------------------
# S3, S4, S6, S7 — Reports page
# ---------------------------------------------------------------------------

# verified: 2026-05-14 (ASP.NET ContentPlaceHolder id)
REPORT_TYPE_DROPDOWN = Selector(
    By.ID,
    "ctl00_ContentPlaceHolder1_ddlReportType",
    "Choose Report dropdown on the Reports page",
)

# verified: 2026-05-14
START_DATE_INPUT = Selector(
    By.ID,
    "txtStartDate",
    "Start Date text input on the Reports page (mm/dd/yyyy)",
)

# verified: 2026-05-14
END_DATE_INPUT = Selector(
    By.ID,
    "txtEndDate",
    "End Date text input on the Reports page (mm/dd/yyyy)",
)

# verified: 2026-05-14
GET_REPORT_BUTTON = Selector(
    By.ID,
    "ctl00_ContentPlaceHolder1_btnGetReport",
    "Get Report submit button",
)

# verified: 2026-05-14 — positional path from the DataTables 'buttons' toolbar.
# The toolbar's first div holds Copy / CSV / Excel / PDF / Print as button[1..5];
# CSV is button[2]. Permissive text-based fallback in case ordering changes.
CSV_EXPORT_BUTTON = Selector(
    By.XPATH,
    "//*[@id='tblReportResults_wrapper']/div[1]/button[2] | "
    "//*[@id='tblReportResults_wrapper']//button[normalize-space()='CSV'] | "
    "//button[normalize-space()='CSV']",
    "CSV export button on the DataTables results toolbar",
)

# verified: 2026-05-14
REPORT_RESULTS_FIRST_ROW = Selector(
    By.XPATH,
    "//*[@id='tblReportResults']/tbody/tr[1]",
    "First row of the results table — present whether the report has data "
    "or shows the 'No data' indicator (distinguish via REPORT_NO_RESULTS_INDICATOR)",
)

# verified: 2026-05-14 — DataTables emits a tr with td.dataTables_empty when empty
REPORT_NO_RESULTS_INDICATOR = Selector(
    By.XPATH,
    "//*[@id='tblReportResults']//td[contains(@class,'dataTables_empty')] | "
    "//*[@id='tblReportResults']//td[contains(normalize-space(.),'No data available')] | "
    "//*[@id='tblReportResults']//td[contains(normalize-space(.),'No matching records')]",
    "DataTables 'No data available' indicator inside the results table",
)

# ---------------------------------------------------------------------------
# Denylist — the worker NEVER interacts with these (spec §2 hard rules)
# ---------------------------------------------------------------------------

# Each denylist entry must be defensive: cover both text-based and
# attribute-based shapes since we cannot see the live HTML yet.
_DENY_EMERGENCY_LOCKDOWN_TEXT = Selector(
    By.XPATH,
    "//*[contains(normalize-space(.), 'Emergency Lockdown')]",
    "Red EMERGENCY LOCKDOWN button on the main menu — read-only worker",
)

_DENY_EMERGENCY_LOCKDOWN_ID = Selector(
    By.XPATH,
    "//*[@id='emergency-lockdown' or contains(@id,'lockdown') or "
    "contains(@class,'lockdown')]",
    "Defensive secondary selector for Emergency Lockdown (id/class variant)",
)


DENYLIST: frozenset[Selector] = frozenset({
    _DENY_EMERGENCY_LOCKDOWN_TEXT,
    _DENY_EMERGENCY_LOCKDOWN_ID,
})


def all_selectors() -> dict[str, Selector]:
    """Introspect this module and return every public ``Selector`` constant.

    Used by the M3 greppability test to enforce "every defined selector is
    referenced exactly once". Underscore-prefixed names and ``DENYLIST``
    members are excluded (those are intentional internal references).
    """
    import sys

    mod = sys.modules[__name__]
    result: dict[str, Selector] = {}
    for name in dir(mod):
        if name.startswith("_") or name == "DENYLIST":
            continue
        obj = getattr(mod, name)
        if isinstance(obj, Selector):
            result[name] = obj
    return result
