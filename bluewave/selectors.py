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

# verified: TBD (placeholder — confirm via test_m3_smoke against live BlueWeb)
LOGIN_USERNAME = Selector(
    By.XPATH,
    "//input[(@name='username') or (@id='username') or "
    "(preceding::*[normalize-space()='User Name'][1])]",
    "User Name input on the BlueWeb login form",
)

# verified: TBD
LOGIN_PASSWORD = Selector(
    By.XPATH,
    "//input[(@type='password') or (@name='password') or (@id='password')]",
    "Password input on the BlueWeb login form",
)

# verified: TBD
LOGIN_SUBMIT = Selector(
    By.XPATH,
    "//button[normalize-space()='Login'] | "
    "//input[(@type='submit' or @type='button' or @type='image') "
    "        and (@value='Login' or normalize-space(@value)='Login' "
    "             or @alt='Login')] | "
    "//a[normalize-space()='Login' and (@onclick or @href)]",
    "Login submit control on the BlueWeb login form (button/input/styled link)",
)

# ---------------------------------------------------------------------------
# S2 — Main menu (after successful login)
#
# We detect login *success* by the appearance of the Reports menu entry — it
# is the destination we need anyway, and it's a more reliable signal than
# any specific welcome text (which can drift by theme/localization).
# ---------------------------------------------------------------------------

# verified: TBD
MAIN_MENU_REPORTS = Selector(
    By.XPATH,
    "//a[normalize-space()='Reports'] | "
    "//*[normalize-space(text())='Reports' and (self::button or @onclick "
    "    or ancestor::a[1] or ancestor::*[contains(@class,'icon')][1])]",
    "Reports entry on the BlueWeb main menu (icon button / link / labelled tile)",
)

# ---------------------------------------------------------------------------
# S3, S4, S6, S7 — Reports page
# ---------------------------------------------------------------------------

# verified: TBD
REPORT_TYPE_DROPDOWN = Selector(
    By.XPATH,
    "//select[contains(@name,'eport') or contains(@id,'eport')] | "
    "//*[normalize-space(.)='Choose Report:']/following::select[1]",
    "Choose Report dropdown on the Reports page",
)

# verified: TBD
START_DATE_INPUT = Selector(
    By.XPATH,
    "//input[contains(@name,'tart') or contains(@id,'tart')] | "
    "//*[normalize-space(.)='Start Date']/following::input[1]",
    "Start Date text input on the Reports page",
)

# verified: TBD
END_DATE_INPUT = Selector(
    By.XPATH,
    "//input[contains(@name,'nd') and contains(@name,'ate') or "
    "(contains(@id,'nd') and contains(@id,'ate'))] | "
    "//*[normalize-space(.)='End Date']/following::input[1]",
    "End Date text input on the Reports page",
)

# verified: TBD
GET_REPORT_BUTTON = Selector(
    By.XPATH,
    "//button[normalize-space(.)='Get Report'] | "
    "//input[@type='submit' and "
    "(@value='Get Report' or normalize-space(@value)='Get Report')]",
    "Get Report submit button",
)

# verified: TBD
CSV_EXPORT_BUTTON = Selector(
    By.XPATH,
    "//button[normalize-space(.)='CSV'] | "
    "//a[normalize-space(.)='CSV']",
    "CSV export button on the results toolbar",
)

# verified: TBD
REPORT_RESULTS_FIRST_ROW = Selector(
    By.CSS_SELECTOR,
    "table tbody tr",
    "First data row of the rendered results table (presence ⇒ report has data)",
)

# verified: TBD — DataTables convention
REPORT_NO_RESULTS_INDICATOR = Selector(
    By.XPATH,
    "//td[contains(normalize-space(.), 'No data available')] | "
    "//*[contains(normalize-space(.), 'No matching records')]",
    "'No data available in table' / similar indicator when report is empty",
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
