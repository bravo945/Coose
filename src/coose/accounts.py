"""Player accounts. Only the server owner creates them; there is no sign-up."""

from __future__ import annotations

import re
import sqlite3

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so a failed login takes
# the same time whether or not the account is real.
_DUMMY_HASH = _hasher.hash("coose-dummy-password")


class AccountError(ValueError):
    pass


def validate_username(username: str) -> None:
    if not USERNAME_RE.match(username):
        raise AccountError(
            "Usernames are 3 to 32 characters: letters, digits, '_' or '-'."
        )


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")


def create_account(conn: sqlite3.Connection, username: str, password: str) -> None:
    validate_username(username)
    validate_password(password)
    try:
        with conn:
            conn.execute(
                "INSERT INTO accounts (username, password_hash) VALUES (?, ?)",
                (username, _hasher.hash(password)),
            )
    except sqlite3.IntegrityError:
        raise AccountError(f"An account named '{username}' already exists.") from None


def delete_account(conn: sqlite3.Connection, username: str) -> None:
    with conn:
        cur = conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
    if cur.rowcount == 0:
        raise AccountError(f"No account named '{username}'.")


def set_password(conn: sqlite3.Connection, username: str, password: str) -> None:
    validate_password(password)
    with conn:
        cur = conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE username = ?",
            (_hasher.hash(password), username),
        )
    if cur.rowcount == 0:
        raise AccountError(f"No account named '{username}'.")


def list_accounts(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT username FROM accounts ORDER BY username COLLATE NOCASE")
    return [r["username"] for r in rows]


def account_exists(conn: sqlite3.Connection, username: str) -> bool:
    return conn.execute("SELECT 1 FROM accounts WHERE username = ?", (username,)).fetchone() is not None


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> str | None:
    """Return the account's stored username if the password is right, else None."""
    row = conn.execute(
        "SELECT username, password_hash FROM accounts WHERE username = ?", (username,)
    ).fetchone()
    stored = row["password_hash"] if row else _DUMMY_HASH
    try:
        _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return None
    if row is None:
        return None
    if _hasher.check_needs_rehash(stored):
        with conn:
            conn.execute(
                "UPDATE accounts SET password_hash = ? WHERE username = ?",
                (_hasher.hash(password), row["username"]),
            )
    return row["username"]
