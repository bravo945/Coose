"""The public player site: login, character selection and the chatroom."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import accounts, db, game, stories
from ..config import Config
from ..narrator import ClaudeNarrator, Narrator, NarratorError, Scene, load_premise

HERE = Path(__file__).parent
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # two weeks
MAX_PROMPT_CHARS = 4000


def create_app(config: Config, secret_key: str | None = None, narrator: Narrator | None = None) -> FastAPI:
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
    narrator = narrator or ClaudeNarrator(config.model, config.effort, config.api_key_env)

    def conn():
        return db.connect(config.server_db_path)

    def current_user(request: Request) -> str | None:
        user = request.session.get("user")
        if user and not accounts.account_exists(conn(), user):
            request.session.clear()  # the owner removed this account
            return None
        return user

    def render(request: Request, name: str, status_code: int = 200, **context):
        return templates.TemplateResponse(
            request,
            name,
            {"user": current_user(request), "server": stories.status(conn()), **context},
            status_code=status_code,
        )

    @contextmanager
    def running_story():
        """The running story and its database, or (None, None) when offline."""
        st = stories.status(conn())
        if not st.online:
            yield None, None
            return
        story = stories.find_story(config.stories_dir, st.story)
        sconn = stories.open_story_db(story)
        try:
            yield story, sconn
        finally:
            sconn.close()

    def to(path: str):
        return RedirectResponse(path, status_code=303)

    # --- Login ------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if current_user(request):
            return to("/")
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
        return to("/")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return to("/login")

    # --- Playing ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is None:
                return render(request, "offline.html")
            character = game.current_character(sconn, user)
            if character is None:
                return to("/characters")
            settings = story.game_settings()
            view = request.query_params.get("view", "chat")
            rules_file = story.path / "Rulebook" / "Character Rules.md"
            manual_file = story.path / "UserManual.md"
            return render(
                request,
                "play.html",
                story=story,
                character=character,
                view=view,
                messages=game.recent_messages(sconn, character.id),
                world_time=game.world_time(sconn, settings),
                rules=rules_file.read_text(encoding="utf-8") if rules_file.exists() else "",
                manual=manual_file.read_text(encoding="utf-8") if manual_file.exists() else "",
                error=request.session.pop("error", None),
            )

    @app.get("/characters", response_class=HTMLResponse)
    def characters(request: Request):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is None:
                return render(request, "offline.html")
            if game.current_character(sconn, user) is not None:
                return to("/")
            settings = story.game_settings()
            return render(
                request,
                "characters.html",
                story=story,
                characters=game.playable_characters(sconn, user),
                counts=game.player_counts(sconn),
                limit=settings.players_per_character,
                can_create=settings.players_can_create,
                error=request.session.pop("error", None),
            )

    @app.post("/characters/{character_id}/join")
    def join(request: Request, character_id: int):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is None:
                return to("/")
            try:
                game.join(sconn, story.game_settings(), user, character_id)
            except game.GameError as e:
                request.session["error"] = str(e)
                return to("/characters")
        return to("/")

    @app.post("/leave")
    def leave(request: Request):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is not None:
                game.leave(sconn, user)
        return to("/characters")

    @app.get("/characters/new", response_class=HTMLResponse)
    def new_character_form(request: Request):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is None:
                return render(request, "offline.html")
            if not story.game_settings().players_can_create:
                return to("/characters")
            return render(request, "character_new.html", story=story, error=None, form={})

    @app.post("/characters/new", response_class=HTMLResponse)
    def new_character(
        request: Request,
        name: str = Form(""),
        description: str = Form(""),
        location: str = Form(""),
        item1: str = Form(""),
        item2: str = Form(""),
        item3: str = Form(""),
        item4: str = Form(""),
        visibility: str = Form("public"),
    ):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is None:
                return to("/")
            settings = story.game_settings()
            if not settings.players_can_create:
                return to("/characters")
            form = dict(name=name, description=description, location=location,
                        items=[item1, item2, item3, item4], visibility=visibility)
            try:
                if game.current_character(sconn, user) is not None:
                    raise game.GameError("Leave your current character first.")
                character = game.create_character(
                    sconn, settings, name=name, description=description, location=location,
                    items=form["items"], visibility=visibility, owner=user,
                )
                game.join(sconn, settings, user, character.id)
            except game.GameError as e:
                return render(request, "character_new.html", status_code=400, story=story, error=str(e), form=form)
        return to("/")

    @app.post("/prompt")
    def prompt(request: Request, kind: str = Form("action"), text: str = Form("")):
        user = current_user(request)
        if not user:
            return to("/login")
        text = text.strip()
        with running_story() as (story, sconn):
            if story is None:
                return to("/")
            character = game.current_character(sconn, user)
            if character is None:
                return to("/characters")
            if not character.alive:
                request.session["error"] = f"{character.name} is dead. Leave and choose another character."
                return to("/")
            if kind not in game.PLAYER_KINDS:
                request.session["error"] = "That prompt type isn't available yet."
                return to("/")
            if not text:
                return to("/")
            if len(text) > MAX_PROMPT_CHARS:
                request.session["error"] = f"Prompts can be at most {MAX_PROMPT_CHARS} characters."
                return to("/")
            settings = story.game_settings()
            lock = game.character_lock(story.name, character.id)
            if not lock.acquire(blocking=False):
                request.session["error"] = f"{character.name} is still busy with the last action."
                return to("/")
            try:
                character = game.get_character(sconn, character.id)  # fresh, under the lock
                scene = Scene(
                    story=story.display_name,
                    world_time=game.world_time(sconn, settings),
                    world_state=game.world_state(sconn),
                    working_memory=game.working_memory(sconn),
                    character=character,
                    need_levels=game.need_levels(character, settings),
                    recent=game.recent_messages(sconn, character.id, limit=30),
                )
                game.add_message(sconn, character.id, kind, text, username=user)
                premise = load_premise(story.path)
                try:
                    if kind == "question":
                        answer = narrator.question(premise, scene, text)
                        game.add_message(sconn, character.id, "answer", answer)
                    else:
                        result = narrator.action(premise, scene, text)
                        game.apply_action(sconn, character, game.ActionOutcome(**result.model_dump()))
                        game.add_message(sconn, character.id, "narration", result.narration)
                except NarratorError as e:
                    game.add_message(
                        sconn, character.id, "system",
                        f"The narrator couldn't respond: {e}. Nothing changed; you can try again.",
                    )
            finally:
                lock.release()
        return to("/#latest")

    return app
