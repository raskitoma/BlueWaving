"""Generate a Fernet key for ``CONFIG_ENC_KEYS`` (spec §8.4 / §13.8).

Usage::

    python -m bluewave.keygen

Prints one 44-char URL-safe base64 key to stdout. Each invocation produces a
distinct key. For rotation, prepend the new key to ``CONFIG_ENC_KEYS``
(comma-separated, newest first).
"""
from __future__ import annotations

from cryptography.fernet import Fernet


def main() -> None:
    print(Fernet.generate_key().decode("ascii"))


if __name__ == "__main__":
    main()
