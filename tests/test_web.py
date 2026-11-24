import pytest
from fastapi.testclient import TestClient

from coose import accounts, stories
from coose.web.app import create_app


@pytest.fixture
def client(config, conn):
    accounts.create_account(conn, "Mara", "correct horse")
    return TestClient(create_app(config, secret_key="test-secret"))


def login(client, password="correct horse"):
    return client.post(
        "/login", data={"username": "Mara", "password": password}, follow_redirects=False
    )


def test_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = client.get("/login")
    assert page.status_code == 200
    assert 'data-theme="dark"' in page.text
    assert "/static/style.css" in page.text


def test_wrong_password(client):
    r = login(client, "nope nope")
    assert r.status_code == 401
    assert "match an account" in r.text


def test_offline_when_no_story(client):
    assert login(client).status_code == 303
    r = client.get("/")
    assert "Story offline" in r.text
    assert "No story is running" in r.text


def test_online_and_paused_story(client, conn, config, story_dir):
    stories.run_story(conn, config.stories_dir, "demo")
    login(client)
    assert "My Story" in client.get("/").text
    stories.pause(conn)
    r = client.get("/")
    assert "Story offline" in r.text and "paused" in r.text


def test_logout_and_removed_account(client, conn):
    login(client)
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303
    login(client)
    accounts.delete_account(conn, "Mara")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
