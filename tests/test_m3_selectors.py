"""M3 — selector catalog invariants (spec §10/M3 pass criteria).

- DENYLIST must contain at least one entry tagged for Emergency Lockdown.
- Every public Selector defined in ``selectors.py`` must be referenced from
  exactly one production module (greppability rule).
- No production module may inline a raw string that matches a denylist
  value — only the selectors module owns those strings.
"""
from __future__ import annotations

import pathlib

import pytest

from bluewave.selectors import DENYLIST, Selector, all_selectors


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BLUEWAVE_DIR = REPO_ROOT / "bluewave"


def _bluewave_py_files(exclude: set[str]) -> list[pathlib.Path]:
    return [
        p
        for p in BLUEWAVE_DIR.glob("*.py")
        if p.name not in exclude and p.name != "__init__.py"
    ]


# ---------------------------------------------------------------------------
# DENYLIST contents (spec §2 hard rules)
# ---------------------------------------------------------------------------


def test_denylist_is_a_frozenset() -> None:
    """Frozen so it cannot be mutated at runtime."""
    assert isinstance(DENYLIST, frozenset)


def test_denylist_is_nonempty() -> None:
    assert len(DENYLIST) >= 1


def test_denylist_covers_emergency_lockdown() -> None:
    """At least one denylist entry must mention Emergency Lockdown either in
    its locator string or its description."""
    haystacks = [
        (sel.value + " " + sel.description).lower() for sel in DENYLIST
    ]
    assert any("emergency lockdown" in h for h in haystacks), haystacks


def test_denylist_entries_are_selectors() -> None:
    for sel in DENYLIST:
        assert isinstance(sel, Selector)
        assert sel.by
        assert sel.value
        assert sel.description


# ---------------------------------------------------------------------------
# Greppability — every defined selector is used in exactly one place
# ---------------------------------------------------------------------------


def _modules_referencing(name: str, files: list[pathlib.Path]) -> list[str]:
    """Return the distinct module filenames that mention ``name`` as a whole
    token (any number of times)."""
    import re

    pattern = re.compile(rf"\b{re.escape(name)}\b")
    return sorted(
        p.name
        for p in files
        if pattern.search(p.read_text(encoding="utf-8"))
    )


def test_every_public_selector_is_used_in_exactly_one_module() -> None:
    """Spec §10/M3: 'Every selector in selectors.py is referenced by exactly
    one call site (greppable; CI test enforces).'

    Interpretation: each selector is *owned* by exactly one production module
    — the module that interacts with its element. Inside that module, the
    name may appear multiple times (e.g. a ``WebDriverWait`` predicate plus
    a ``SafeDriver.click`` call). Across modules, it must not be duplicated;
    that would mean two scrape paths target the same element and should be
    consolidated.

    Scope: ``bluewave/*.py`` minus ``selectors.py``. Tests are excluded —
    the rule is about production usage.
    """
    files = _bluewave_py_files(exclude={"selectors.py"})

    failures: list[str] = []
    for name in all_selectors():
        modules = _modules_referencing(name, files)
        if len(modules) == 0:
            failures.append(f"{name}: never referenced in any production module")
        elif len(modules) > 1:
            failures.append(
                f"{name}: referenced in multiple modules ({modules}) — "
                f"consolidate into one"
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# No production module may inline a denylist locator string
# ---------------------------------------------------------------------------


def test_no_production_code_inlines_denylist_strings() -> None:
    """Inline use defeats the SafeDriver gate. Only ``selectors.py`` may
    contain a denylist locator string."""
    files = _bluewave_py_files(exclude={"selectors.py"})

    offenders: list[str] = []
    for sel in DENYLIST:
        for path in files:
            text = path.read_text(encoding="utf-8")
            if sel.value in text:
                offenders.append(f"{path.name} contains denylist value {sel.value!r}")

    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# Sanity: M3 selectors all carry a description and a non-empty value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,sel", list(all_selectors().items()))
def test_each_selector_has_metadata(name: str, sel: Selector) -> None:
    assert sel.by, f"{name} has empty `by`"
    assert sel.value, f"{name} has empty `value`"
    assert sel.description and len(sel.description) >= 8, (
        f"{name} description too short — must explain the element"
    )
