"""FastAPI app factory + route mounting (spec §7).

Architecture:

- App state holds the :class:`ConfigStore`, the :class:`Orchestrator`, and
  the current :class:`Config` snapshot (None when unconfigured).
- Routes live in :mod:`bluewave.routes`.
- Env validation happens at module import (M1 contract) so misconfigured
  containers refuse to start.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Mapping

from fastapi import FastAPI

from . import __version__


# Spec §4.5 env catalog — vars that MUST be present and non-empty.
REQUIRED_ENV: tuple[str, ...] = (
    "CONFIG_ENC_KEYS",
    "WEB_USER",
    "WEB_PASS_HASH",
    "WEB_ALLOW_HTTP",
)


def validate_env(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []

    for k in REQUIRED_ENV:
        if not env.get(k):
            errors.append(f"missing required env var: {k} (spec §4.5)")

    if env.get("WEB_ALLOW_HTTP") and env["WEB_ALLOW_HTTP"] != "1":
        errors.append(
            "WEB_ALLOW_HTTP must be exactly '1' (spec L13). "
            "HTTP-only on private network is the chosen posture; this env "
            "var is the explicit acknowledgment."
        )

    tz = env.get("TZ", "UTC")
    if tz != "UTC":
        errors.append(
            f"TZ must be 'UTC' (got {tz!r}). Internal time math is UTC-only; "
            f"operator timezone is a separate, web-UI-configured value "
            f"(spec L17 / §4.5)."
        )

    return errors


def _enforce_env_or_exit() -> None:
    errors = validate_env(os.environ)
    if errors:
        sys.stderr.write("FATAL: container env failed validation:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write("See BLUEWEB_AUDIT_INGEST_SPEC.md §4.5.\n")
        sys.exit(2)


_BOOT_TS = time.time()


def create_app(
    config_path: str | None = None,
    store=None,
    orchestrator=None,
    scheduler=None,
    start_scheduler: bool = False,
) -> FastAPI:
    """Build the FastAPI app. Arguments allow test injection.

    Lazy imports keep the module cheap to import.
    """
    from .auth import require_basic_auth
    from .config import (
        DEFAULT_CONFIG_PATH,
        ConfigStore,
        build_multifernet,
        parse_keys_env,
    )
    from .orchestrator import Orchestrator
    from .routes import register_routes

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    docs_enabled = log_level == "DEBUG"

    app = FastAPI(
        title="BlueWeb Audit Ingest Worker",
        version=__version__,
        # /docs is gated behind LOG_LEVEL=DEBUG (L24) and behind Basic Auth.
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.boot_ts = _BOOT_TS
    app.state.version = __version__

    if store is None:
        keys = parse_keys_env(os.environ.get("CONFIG_ENC_KEYS"))
        mf = build_multifernet(keys)
        store = ConfigStore(config_path or DEFAULT_CONFIG_PATH, mf)
    app.state.store = store

    if orchestrator is None:
        orchestrator = Orchestrator(store)
    app.state.orchestrator = orchestrator

    if scheduler is None:
        from .scheduler import Scheduler
        scheduler = Scheduler(store, orchestrator)
    app.state.scheduler = scheduler

    if start_scheduler:
        scheduler.start()

        @app.on_event("shutdown")
        def _stop_scheduler() -> None:
            scheduler.shutdown(wait=False)

    register_routes(app)

    # Defensively gate /docs and /openapi.json behind Basic Auth when present.
    if docs_enabled:
        @app.middleware("http")
        async def gate_docs(request, call_next):
            from .auth import _check_credentials
            from fastapi.security.utils import get_authorization_scheme_param
            if request.url.path in ("/docs", "/openapi.json"):
                auth = request.headers.get("Authorization", "")
                scheme, param = get_authorization_scheme_param(auth)
                if scheme.lower() != "basic":
                    from fastapi.responses import Response
                    return Response(
                        status_code=401,
                        headers={"WWW-Authenticate": "Basic"},
                    )
            return await call_next(request)

    return app


def main() -> None:
    _enforce_env_or_exit()
    import uvicorn

    bind = os.environ.get("WEB_BIND", "0.0.0.0:8080")
    host, _, port = bind.rpartition(":")
    app = create_app(start_scheduler=True)
    uvicorn.run(
        app,
        host=host or "0.0.0.0",
        port=int(port or "8080"),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
