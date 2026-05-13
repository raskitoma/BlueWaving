"""``python -m bluewave.rotate_config``: re-encrypt every config field with
the current head key (spec L21 / §8.4 / §13.8).

Usage::

    docker compose exec bluewave-worker python -m bluewave.rotate_config

After the operator prepends a new key to ``CONFIG_ENC_KEYS`` and restarts,
this command rewrites the SQLite config file so every field is encrypted
with the new head key. The old key can then be dropped on the next deploy.

Exits 0 on success, 1 on decrypt failure, 2 on missing config.
"""
from __future__ import annotations

import os
import sys

from .config import (
    DEFAULT_CONFIG_PATH,
    ConfigStore,
    build_multifernet,
    key_fingerprint,
    parse_keys_env,
)


def main() -> int:
    raw = os.environ.get("CONFIG_ENC_KEYS")
    keys = parse_keys_env(raw)
    if len(keys) < 2:
        sys.stderr.write(
            "warning: CONFIG_ENC_KEYS has only one key — nothing to rotate to.\n"
            "         Prepend a new key (comma-separated) first.\n"
        )

    mf = build_multifernet(keys)
    path = os.environ.get("BLUEWAVE_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    store = ConfigStore(path, mf)

    cfg = store.load()
    if cfg is None:
        sys.stderr.write(f"no config at {path}; nothing to rotate\n")
        return 2

    try:
        store.reencrypt_all()
    except Exception as e:
        sys.stderr.write(f"rotate_config failed: {e}\n")
        return 1

    print(
        f"ok: re-encrypted config with head key fingerprint "
        f"{key_fingerprint(keys[0])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
