"""Exception hierarchy keyed to the §6.4.5 closed status enum.

Every uncaught exception in ``run_start()`` (spec §6.4.1) maps to a status
string written to ``z_audit_logs_efk_runs.status``. Subclasses here carry
that mapping on the class.

The set is **closed** — any uncaught exception that is not a ``RunFailure``
becomes ``ingest_failed`` as the catch-all (spec §6.4.5).
"""
from __future__ import annotations


class RunFailure(Exception):
    """Base. Subclasses must set ``status`` to one of the §6.4.5 values."""

    status: str = "ingest_failed"


class AuthFailed(RunFailure):
    """S1 login submitted but no welcome banner appeared. Wrong creds, locked
    account, or BlueWeb returned an error page."""

    status = "auth_failed"


class NavFailed(RunFailure):
    """The browser could not reach BlueWeb, or a required navigation element
    failed to render. Catch-all for S1–S2 and S3 layout failures."""

    status = "nav_failed"


class ReportTimeout(RunFailure):
    """S6 Get Report returned neither a tbody row nor a no-rows indicator
    within the timeout."""

    status = "report_timeout"


class DownloadFailed(RunFailure):
    """S7 CSV button clicked but the expected file never materialized."""

    status = "download_failed"


class ParseFailed(RunFailure):
    """Downloaded CSV failed schema or value parsing (spec §5)."""

    status = "parse_failed"


class IngestFailed(RunFailure):
    """MySQL write failed (connection, transaction, dialect)."""

    status = "ingest_failed"


class SchemaDriftError(RunFailure):
    """``z_audit_logs_efk`` exists but does not match expected shape (M2)."""

    status = "schema_drift"


class DenylistedSelector(RunFailure):
    """Code attempted to interact with an element on the denylist (spec §2
    hard rules). Treated as a navigation failure for status purposes — the
    worker has navigated somewhere it should not be."""

    status = "nav_failed"
