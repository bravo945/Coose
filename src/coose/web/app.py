"""The public player site: login, and (from milestone 2) the game itself."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import accounts, db, stories
from ..config import Config

HERE = Path(__file__).parent
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # two weeks


def create_app(config: Config, secret_key: str | None = None) -> FastAPI:
    app = FastAPI(title="Coose", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key or config.secret_key() or secrets.token_urlsafe(48),
        session_cookie="coose_session",
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,  # the Cloudflare tunnel terminates HTTPS in front of us
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    def conn():
        return db.connect(config.server_db_path)

    def current_user(request: Request) -> str | None:
        user = request.session.get("user")
        if user and not accounts.account_exists(conn(), user):
            request.session.clear()  # the owner removed this account
            return None
        return user

    def render(request: Request, name: str, status_code: int = 200, **context):
        st = stories.status(conn())
        return templates.TemplateResponse(
            request,
            name,
            {"user": current_user(request), "server": st, **context},
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if not current_user(request):
            return RedirectResponse("/login", status_code=303)
        st = stories.status(conn())
        if not st.online:
            return render(request, "offline.html")
        story = stories.find_story(config.stories_dir, st.story)
        return render(request, "home.html", story=story)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error=None)

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, username: str = Form(""), password: str = Form("")):
        user = accounts.authenticate(conn(), username.strip(), password)
        if not user:
            return render(
                request,
                "login.html",
                status_code=401,
                error="That username and password don't match an account.",
                username=username,
            )
        request.session.clear()
        request.session["user"] = user
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    return app
