import pytest

from coose import accounts


def test_create_and_authenticate(conn):
    accounts.create_account(conn, "Mara", "correct horse")
    assert accounts.authenticate(conn, "Mara", "correct horse") == "Mara"
    assert accounts.authenticate(conn, "mara", "correct horse") == "Mara"  # case-insensitive name
    assert accounts.authenticate(conn, "Mara", "wrong password") is None
    assert accounts.authenticate(conn, "Nobody", "correct horse") is None


def test_duplicate_and_invalid(conn):
    accounts.create_account(conn, "Mara", "correct horse")
    with pytest.raises(accounts.AccountError):
        accounts.create_account(conn, "MARA", "another pass")
    with pytest.raises(accounts.AccountError):
        accounts.create_account(conn, "a b", "correct horse")
    with pytest.raises(accounts.AccountError):
        accounts.create_account(conn, "Jon", "short")


def test_password_change_and_removal(conn):
    accounts.create_account(conn, "Jon", "first password")
    accounts.set_password(conn, "Jon", "second password")
    assert accounts.authenticate(conn, "Jon", "first password") is None
    assert accounts.authenticate(conn, "Jon", "second password") == "Jon"
    accounts.delete_account(conn, "Jon")
    assert accounts.list_accounts(conn) == []
    with pytest.raises(accounts.AccountError):
        accounts.delete_account(conn, "Jon")
