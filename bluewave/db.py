"""MySQL connection factory (spec §4.2 / L19).

Single rule: **fresh connection per use, kwargs only**.

- No connection pool — sidesteps MySQL ``wait_timeout`` (default 8 h) entirely.
- No URL DSN parsing — a MySQL password containing ``@``, ``:`` or ``/`` would
  break a DSN, but cannot break keyword args.
- ``charset='utf8mb4'`` is non-negotiable per L20.
"""
from __future__ import annotations

from dataclasses import dataclass

import pymysql
import pymysql.cursors


# Spec §3 calls for utf8mb4 everywhere. Hardcoded here so it cannot drift.
MYSQL_CHARSET = "utf8mb4"

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def connect(
    cfg: MysqlConfig,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> pymysql.connections.Connection:
    """Open a fresh MySQL connection.

    Always uses keyword arguments; never a URL DSN. ``charset='utf8mb4'``
    is enforced. Caller is responsible for ``.close()`` (use ``with`` block).
    """
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset=MYSQL_CHARSET,
        connect_timeout=connect_timeout,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
