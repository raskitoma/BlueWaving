"""Generate a bcrypt hash for ``WEB_PASS_HASH`` (spec §7.2 / §13.1).

Usage::

    python -m bluewave.hashpw                 # interactive (TTY)
    echo 'mypassword' | python -m bluewave.hashpw --stdin   # scriptable

The interactive form prompts twice and confirms. The ``--stdin`` form reads one
line from stdin and emits the hash. Both apply the same minimum-length rule.
"""
from __future__ import annotations

import getpass
import sys

import bcrypt


MIN_PASSWORD_LENGTH = 8
BCRYPT_ROUNDS = 12


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash for ``plaintext``. Pure for testability."""
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    h = bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
    return h.decode("ascii")


def _read_interactive() -> str:
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2:
        sys.stderr.write("Passwords do not match.\n")
        sys.exit(1)
    return pw1


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv

    if "--stdin" in args:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = _read_interactive()

    try:
        print(hash_password(pw))
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
