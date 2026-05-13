"""Operator-configurable settings stored in an encrypted SQLite file.

Schema is a single-row table ``config``. Password fields are Fernet-encrypted
at the column level; all other fields are plaintext. Reading a row decrypts;
writing a row encrypts. Empty password on write = keep existing.

The encryption key list (``CONFIG_ENC_KEYS``) is the only bootstrap secret;
without it the SQLite file leaks nothing useful. Multi-key supported per
spec L21 / §8.4 — the FIRST key encrypts new writes, ALL keys try to decrypt.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


DEFAULT_CONFIG_PATH = "/var/lib/bluewave-worker/config.sqlite"


# ---------------------------------------------------------------------------
# Fernet key handling (L21)
# ---------------------------------------------------------------------------


def parse_keys_env(raw: str | None) -> list[bytes]:
    """Parse ``CONFIG_ENC_KEYS`` (comma-separated). First key encrypts."""
    if not raw or not raw.strip():
        raise ValueError("CONFIG_ENC_KEYS is empty; run python -m bluewave.keygen")
    keys = [k.strip().encode("ascii") for k in raw.split(",") if k.strip()]
    if not keys:
        raise ValueError("CONFIG_ENC_KEYS contained no non-empty entries")
    return keys


def build_multifernet(keys: list[bytes]) -> MultiFernet:
    return MultiFernet([Fernet(k) for k in keys])


def key_fingerprint(key: bytes) -> str:
    """First 8 chars of SHA-256(key) — for the ``key.rotated`` self-audit row."""
    import hashlib

    return hashlib.sha256(key).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """The operator-managed settings (spec §7.1)."""

    site_label: str
    blueweb_url: str
    blueweb_user: str
    blueweb_password: str         # decrypted at read time
    operator_timezone: str
    mysql_host: str
    mysql_port: int
    mysql_database: str
    mysql_user: str
    mysql_password: str           # decrypted at read time
    schedule_local: str           # "HH:MM"
    catch_up_cap_days: int


_DEFAULTS = {
    "schedule_local": "03:00",
    "catch_up_cap_days": 14,
    "mysql_port": 3306,
}


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    site_label               TEXT NOT NULL,
    blueweb_url              TEXT NOT NULL,
    blueweb_user             TEXT NOT NULL,
    blueweb_password_enc     TEXT NOT NULL,
    operator_timezone        TEXT NOT NULL,
    mysql_host               TEXT NOT NULL,
    mysql_port               INTEGER NOT NULL,
    mysql_database           TEXT NOT NULL,
    mysql_user               TEXT NOT NULL,
    mysql_password_enc       TEXT NOT NULL,
    schedule_local           TEXT NOT NULL,
    catch_up_cap_days        INTEGER NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ConfigStore:
    """Encapsulates persistence of :class:`Config` to an encrypted SQLite file.

    Thread-safe under SQLite's default coarse locking — the worker only has
    one writer (the web service in the same process).
    """

    def __init__(self, path: str | os.PathLike[str], multi: MultiFernet) -> None:
        self.path = Path(path)
        self._mf = multi
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    # ----- low-level encryption -------------------------------------------

    def _encrypt(self, plaintext: str) -> str:
        return self._mf.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def _decrypt(self, token: str) -> str:
        return self._mf.decrypt(token.encode("ascii")).decode("utf-8")

    # ----- public API -----------------------------------------------------

    def exists(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM config WHERE id = 1").fetchone()
        return row is not None

    def load(self) -> Optional[Config]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM config WHERE id = 1").fetchone()
        if row is None:
            return None
        return Config(
            site_label=row["site_label"],
            blueweb_url=row["blueweb_url"],
            blueweb_user=row["blueweb_user"],
            blueweb_password=self._decrypt(row["blueweb_password_enc"]),
            operator_timezone=row["operator_timezone"],
            mysql_host=row["mysql_host"],
            mysql_port=int(row["mysql_port"]),
            mysql_database=row["mysql_database"],
            mysql_user=row["mysql_user"],
            mysql_password=self._decrypt(row["mysql_password_enc"]),
            schedule_local=row["schedule_local"],
            catch_up_cap_days=int(row["catch_up_cap_days"]),
        )

    def save(self, cfg: Config) -> None:
        """Insert or replace the single config row."""
        with self._conn() as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM config")
            conn.execute(
                """
                INSERT INTO config
                  (id, site_label, blueweb_url, blueweb_user, blueweb_password_enc,
                   operator_timezone, mysql_host, mysql_port, mysql_database,
                   mysql_user, mysql_password_enc, schedule_local, catch_up_cap_days)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cfg.site_label,
                    cfg.blueweb_url,
                    cfg.blueweb_user,
                    self._encrypt(cfg.blueweb_password),
                    cfg.operator_timezone,
                    cfg.mysql_host,
                    cfg.mysql_port,
                    cfg.mysql_database,
                    cfg.mysql_user,
                    self._encrypt(cfg.mysql_password),
                    cfg.schedule_local,
                    cfg.catch_up_cap_days,
                ),
            )
            conn.execute("COMMIT")

    def reencrypt_all(self) -> None:
        """Re-encrypt every encrypted field with the current head key (L21).

        Used by ``python -m bluewave.rotate_config`` after the operator adds
        a new key to ``CONFIG_ENC_KEYS``.
        """
        cfg = self.load()
        if cfg is None:
            return
        self.save(cfg)
