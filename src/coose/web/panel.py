"""The owner's private control panel: the same controls as the terminal, in a
browser. Run it with `coose panel`, bound to the machine's Tailscale address,
so only the owner's tailnet can reach it. It is a separate app on its own
port and is never served through the public site or the Cloudflare tunnel.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import accounts, controls, db, stories
from ..config import Config

HERE = Path(__file__).parent


def create_panel(config: Config, password: str, secret_key: str | None = None) -> FastAPI:
    app = FastAPI(title="Coose panel", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key or secrets.token_urlsafe(48),
                       session_cookie="coose_panel", max_age=60 * 60 * 12, same_site="strict", https_only=False)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    def conn():
        return db.connect(config.server_db_path)

    def to(path: str):
        return RedirectResponse(path, status_code=303)

    def signed_in(request: Request) -> bool:
        return request.session.get("owner") is True

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "panel_login.html", {"error": None, "server": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, password_given: str = Form("", alias="password")):
        if not hmac.compare_digest(password_given.encode(), password.encode()):
            return templates.TemplateResponse(request, "panel_login.html",
                                              {"error": "Wrong password.", "server": None}, status_code=401)
        request.session["owner"] = True
        return to("/")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return to("/login")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if not signed_in(request):
            return to("/login")
        c = conn()
        st = stories.status(c)
        found = stories.list_stories(config.stories_dir)
        rows = []
        for s in found:
            rows.append(dict(
                story=s,
                cap=controls.describe_cap(c, s) if s.valid else "",
                schedule="; ".join(sp.describe() for sp in controls.get_schedule(c, s.name)),
                admins=controls.admins(c, s),
            ))
        return templates.TemplateResponse(request, "panel.html", dict(
            server=st, user="owner", rows=rows, accounts=accounts.list_accounts(c),
            usage=db.usage_summary(c), total=sum(r["cost"] or 0 for r in db.usage_summary(c)),
            message=request.session.pop("message", None), error=request.session.pop("error", None),
        ))

    def action(request: Request, do: Callable[[], str]):
        """Run one control and come back to the panel with its result."""
        if not signed_in(request):
            return to("/login")
        try:
            request.session["message"] = do()
        except (stories.StoryError, controls.ControlError, accounts.AccountError, ValueError) as e:
            request.session["error"] = str(e)
        return to("/")

    def story(name: str) -> stories.Story:
        return stories.find_story(config.stories_dir, name)

    @app.post("/run")
    def run(request: Request, name: str = Form("")):
        def do():
            if controls.story_cap_reached(conn(), story(name)):
                raise controls.ControlError(f"'{name}' has reached its cap. Raise or clear it first.")
            return f"Now running '{stories.run_story(conn(), config.stories_dir, name).display_name}'."
        return action(request, do)

    @app.post("/pause")
    def pause(request: Request):
        return action(request, lambda: (stories.pause(conn()), "Story paused.")[1])

    @app.post("/resume")
    def resume(request: Request):
        def do():
            st = stories.status(conn())
            if st.story and controls.story_cap_reached(conn(), story(st.story)):
                raise controls.ControlError(f"'{st.story}' has reached its cap. Raise or clear it first.")
            stories.resume(conn())
            return "Story resumed."
        return action(request, do)

    def number(text: str) -> int | None:
        text = text.strip()
        if not text:
            return None
        if not text.isdigit():
            raise controls.ControlError(f"'{text}' isn't a whole number.")
        return int(text)

    @app.post("/cap")
    def cap(request: Request, name: str = Form(""), actions: str = Form(""), rounds: str = Form(""),
            clear: str = Form("")):
        def do():
            s = story(name)
            if clear:
                controls.clear_cap(conn(), s.name)
                return f"'{s.display_name}' has no cap now."
            controls.set_cap(conn(), s, number(actions), number(rounds))
            return f"'{s.display_name}': {controls.describe_cap(conn(), s)}."
        return action(request, do)

    @app.post("/schedule")
    def schedule(request: Request, name: str = Form(""), days: str = Form(""), start: str = Form(""),
                 end: str = Form(""), clear: str = Form("")):
        def do():
            s = story(name)
            if clear:
                controls.clear_schedule(conn(), s.name)
                return f"'{s.display_name}' has no schedule now."
            span = controls.add_span(conn(), s.name, days, start, end)
            return f"'{s.display_name}' opens {span.describe()}."
        return action(request, do)

    @app.post("/admins")
    def admins(request: Request, name: str = Form(""), username: str = Form(""), remove: str = Form("")):
        def do():
            s = story(name)
            if remove:
                controls.remove_admin(conn(), username.strip(), s.name)
                return f"'{username}' is no longer a story admin of '{s.display_name}'."
            controls.add_admin(conn(), username.strip(), s.name)
            return f"'{username}' is now a story admin of '{s.display_name}'."
        return action(request, do)

    @app.post("/accounts")
    def account(request: Request, username: str = Form(""), password: str = Form(""), do_what: str = Form("add")):
        def do():
            name = username.strip()
            if do_what == "remove":
                accounts.delete_account(conn(), name)
                return f"Removed account '{name}'."
            if do_what == "password":
                accounts.set_password(conn(), name, password)
                return f"Password changed for '{name}'."
            accounts.create_account(conn(), name, password)
            return f"Created account '{name}'."
        return action(request, do)

    return app
