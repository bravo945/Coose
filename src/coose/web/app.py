"""The public player site: login, character selection and the chatroom."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from pathlib import Path

import asyncio
import logging
import threading
from collections import defaultdict

from fastapi import BackgroundTasks, FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import accounts, db, game, rounds, stories
from ..config import Config
from ..narrator import AuthorResult, ClaudeNarrator, Narrator, NarratorError, Scene, format_minutes, load_premise
from .live import Hub

log = logging.getLogger(__name__)

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
    def record_usage(record: dict) -> None:
        c = conn()
        try:
            db.log_usage(c, stories.status(c).story, record)
        finally:
            c.close()

    narrator = narrator or ClaudeNarrator(config.model, config.effort, config.api_key_env, usage_log=record_usage)
    hub = app.state.hub = Hub()
    round_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)  # one round change at a time

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

    def nearby(story_name: str, sconn, character: game.Character) -> list[dict]:
        """Who is at the same location, and whether a player is there to answer."""
        return [
            {"id": c.id, "name": c.name, "online": hub.active(story_name, c.id)}
            for c in game.characters_here(sconn, character)
        ]

    def live_context(story, sconn, character: game.Character) -> dict:
        """Everything on the play page that can change while it's open."""
        settings = story.game_settings()
        return dict(
            character=character,
            messages=game.recent_messages(sconn, character.id),
            nearby=nearby(story.name, sconn, character),
            world_time=game.world_time(sconn, settings),
            round_number=rounds.current_round(sconn),
            round_vote=settings.round_vote,
            votes=rounds.votes(sconn),
            online=hub.online_users(story.name),
            skipping=rounds.is_skipping(sconn, character.id),
            skip_coming=bool(rounds.pending_skips(sconn)),
        )

    def make_scene(sconn, story, character: game.Character) -> Scene:
        settings = story.game_settings()
        return Scene(
            story=story.display_name,
            world_time=game.world_time(sconn, settings),
            world_state=game.world_state(sconn),
            working_memory=game.working_memory(sconn),
            character=character,
            need_levels=game.need_levels(sconn, character, settings),
            recent=game.recent_messages(sconn, character.id, limit=30),
            round_number=rounds.current_round(sconn),
        )

    def tell_online(story_name: str, sconn, text: str, exclude: int | None = None) -> None:
        targets = [cid for cid in hub.online_characters(story_name) if cid != exclude]
        for cid in targets:
            game.add_message(sconn, cid, "system", text)
        hub.notify(story_name, targets)

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
            view = request.query_params.get("view", "chat")
            rules_file = story.path / "Rulebook" / "Character Rules.md"
            manual_file = story.path / "UserManual.md"
            return render(
                request,
                "play.html",
                story=story,
                view=view,
                **live_context(story, sconn, character),
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
            hub.notify_story(story.name)
        return to("/")

    @app.post("/leave")
    def leave(request: Request):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is not None:
                game.leave(sconn, user)
                hub.notify_story(story.name)
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
    def prompt(
        request: Request,
        background: BackgroundTasks,
        kind: str = Form("action"),
        text: str = Form(""),
        recipient: list[int] = Form([]),
    ):
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
                request.session["error"] = "That prompt type isn't available."
                return to("/")
            if not text:
                return to("/")
            if len(text) > MAX_PROMPT_CHARS:
                request.session["error"] = f"Prompts can be at most {MAX_PROMPT_CHARS} characters."
                return to("/")
            if round_locks[story.name].locked():
                request.session["error"] = "The round is changing. Try again in a moment."
                return to("/")
            if kind != "question" and rounds.is_skipping(sconn, character.id):
                request.session["error"] = (f"{character.name} is busy until the next round. "
                                            "You can still ask Questions.")
                return to("/")
            lock = game.character_lock(story.name, character.id)
            if not lock.acquire(blocking=False):
                request.session["error"] = f"{character.name} is still busy with the last prompt."
                return to("/")
            try:
                character = game.get_character(sconn, character.id)  # fresh, under the lock
                premise = load_premise(story.path)
                if kind == "dialogue":
                    error = run_dialogue(story, sconn, character, user, text, recipient, premise)
                    if error:
                        request.session["error"] = error
                elif kind == "question":
                    run_question(story, sconn, character, user, text, premise)
                else:
                    run_action(story, sconn, character, user, text, premise, background)
            finally:
                lock.release()
        return to("/#latest")

    def run_question(story, sconn, character, user, text, premise) -> None:
        scene = make_scene(sconn, story, character)
        game.add_message(sconn, character.id, "question", text, username=user)
        hub.notify(story.name, [character.id])
        try:
            answer = narrator.question(premise, scene, text)
            game.add_message(sconn, character.id, "answer", answer)
        except NarratorError as e:
            game.add_message(sconn, character.id, "system", narrator_failed(e))
        hub.notify(story.name, [character.id])

    def run_action(story, sconn, character, user, text, premise, background: BackgroundTasks) -> None:
        settings = story.game_settings()
        scene = make_scene(sconn, story, character)
        game.add_message(sconn, character.id, "action", text, username=user)
        hub.notify(story.name, [character.id])
        others = game.other_characters(sconn, character.id)
        try:
            result = narrator.action(premise, scene, text, existing=[character.name] + [o.name for o in others])
        except NarratorError as e:
            game.add_message(sconn, character.id, "system", narrator_failed(e))
            hub.notify(story.name, [character.id])
            return
        skip = result.minutes_elapsed > settings.skip_threshold_minutes
        game.apply_action(sconn, character, game.ActionOutcome(**result.model_dump(exclude={"new_characters"})),
                          advance_clock=not skip)
        game.add_message(sconn, character.id, "narration", result.narration)
        for new in result.new_characters:
            game.create_ai_character(sconn, settings, name=new.name, description=new.description,
                                     location=new.location)
        skip_now = False
        if skip:
            rounds.add_skip(sconn, character.id, result.minutes_elapsed, text)
            game.add_message(
                sconn, character.id, "system",
                f"This takes about {format_minutes(result.minutes_elapsed)}, so time will skip ahead at the next "
                f"round. Until then {character.name} is occupied, but you can still ask Questions.",
            )
            tell_online(story.name, sconn, "A time skip is coming at the next round.", exclude=character.id)
            # Nobody else is here to keep playing, so the skip happens at once.
            mine = {u.lower() for u in game.players_of(sconn, character.id)}
            skip_now = all(u.lower() in mine for u in hub.online_users(story.name))
        hub.notify(story.name, [character.id])
        if others:
            # Runs after the acting player gets their page back, so they don't wait on it.
            background.add_task(judge_witnesses, story.name, scene, text, result, [o.id for o in others], premise)
        if skip_now or rounds.round_due(sconn, settings):
            background.add_task(change_round, story.name)

    def change_round(story_name: str) -> None:
        """Close the round: one AI pass over the whole world, then Logbook, notices and a snapshot."""
        lock = round_locks[story_name]
        if not lock.acquire(blocking=False):
            return  # already changing
        try:
            story = stories.find_story(config.stories_dir, story_name)
            sconn = stories.open_story_db(story)
            try:
                settings = story.game_settings()
                online = hub.online_characters(story_name)
                context = rounds.round_context(sconn, settings, story.display_name, online)
                try:
                    result = narrator.round_change(load_premise(story.path), context)
                except NarratorError as e:
                    log.warning("Round change in %s failed: %s", story_name, e)
                    tell_online(story_name, sconn, f"The round change failed ({e}). It will be tried again "
                                "after the next Action, or by voting.")
                    return
                outcome = rounds.RoundOutcome(
                    **result.model_dump(exclude={"characters"}),
                    characters=[rounds.CharacterUpdate(**c.model_dump()) for c in result.characters],
                )
                rounds.apply_round(sconn, story.path, settings, outcome, online)
                hub.notify_story(story_name)
            finally:
                sconn.close()
        finally:
            lock.release()

    def run_backfill(story_name: str, character_id: int) -> None:
        """Write a backstory for a character who sat out a skip while offline."""
        lock = game.character_lock(story_name, character_id)
        if not lock.acquire(timeout=300):
            return
        try:
            story = stories.find_story(config.stories_dir, story_name)
            sconn = stories.open_story_db(story)
            try:
                minutes = rounds.take_backfill(sconn, character_id)
                character = game.get_character(sconn, character_id)
                if not minutes or character is None or not character.alive:
                    return
                try:
                    result = narrator.backfill(load_premise(story.path), make_scene(sconn, story, character), minutes)
                except NarratorError as e:
                    rounds.owe_backfill(sconn, character_id, minutes)
                    game.add_message(sconn, character_id, "system",
                                     f"The narrator couldn't write what happened while you were away: {e}. "
                                     "It will try again when you next come back.")
                    hub.notify(story_name, [character_id])
                    return
                game.apply_action(sconn, character, game.ActionOutcome(
                    narration=result.narration, minutes_elapsed=0, location=result.location,
                    condition=result.condition, learned=result.learned,
                ), count=False)
                game.add_message(sconn, character_id, "narration",
                                 f"While you were away ({format_minutes(minutes)}): {result.narration}")
                hub.notify(story_name, [character_id])
            finally:
                sconn.close()
        finally:
            lock.release()

    @app.post("/vote")
    def vote(request: Request, background: BackgroundTasks):
        user = current_user(request)
        if not user:
            return to("/login")
        with running_story() as (story, sconn):
            if story is None or game.current_character(sconn, user) is None:
                return to("/")
            if not story.game_settings().round_vote:
                request.session["error"] = "This story doesn't use round votes."
                return to("/")
            rounds.add_vote(sconn, user)
            online = hub.online_users(story.name) or [user]
            if rounds.vote_passes(sconn, online):
                background.add_task(change_round, story.name)
            hub.notify_story(story.name)
        return to("/")

    def judge_witnesses(story_name: str, scene: Scene, text: str, result: AuthorResult,
                        candidate_ids: list[int], premise: str) -> None:
        story = stories.find_story(config.stories_dir, story_name)
        sconn = stories.open_story_db(story)
        try:
            candidates = [c for c in (game.get_character(sconn, i) for i in candidate_ids) if c and c.alive]
            if not candidates:
                return
            try:
                reports = narrator.witnesses(premise, scene, text, result, candidates)
            except NarratorError as e:
                log.warning("Witness pass for %s failed: %s", scene.character.name, e)
                return
            by_name = {c.name.lower(): c for c in candidates}
            seen = []
            for report in reports:
                witness = by_name.get(report.name.strip().lower())
                if witness is None or witness.id in seen or not report.narration.strip():
                    continue
                seen.append(witness.id)
                game.add_message(sconn, witness.id, "witness", report.narration)
                game.add_knowledge(sconn, witness.id, report.learned)
            hub.notify(story_name, seen)
        finally:
            sconn.close()

    def run_dialogue(story, sconn, speaker, user, text, recipient_ids, premise) -> str | None:
        """Speak to characters at the same place. Returns an error for the player, if any."""
        here = {c.id: c for c in game.characters_here(sconn, speaker)}
        if not here:
            return f"There's nobody here for {speaker.name} to talk to."
        targets = [here[i] for i in dict.fromkeys(recipient_ids) if i in here] or list(here.values())
        names = join_names([t.name for t in targets])
        game.add_message(sconn, speaker.id, "dialogue", f'To {names}: "{text}"', username=user)
        for t in targets:
            also = [o.name for o in targets if o.id != t.id]
            to_whom = f"you and {join_names(also)}" if also else "you"
            game.add_message(sconn, t.id, "heard", f'{speaker.name} says to {to_whom}: "{text}"')
        game.add_memory(sconn, [f'{speaker.name} said to {names}: "{text[:300]}"'])
        hub.notify(story.name, [speaker.id] + [t.id for t in targets])

        mode = story.game_settings().absent_characters
        for t in targets:
            if hub.active(story.name, t.id):
                continue  # their player is here to answer
            ai_character = t.owner is None
            if mode == "freeze" and not ai_character:
                game.add_message(sconn, speaker.id, "narration", f"{t.name} seems distracted and doesn't answer.")
                continue
            try:
                reply = narrator.reply_as(premise, make_scene(sconn, story, t), speaker.name, text,
                                          "act_within_reason" if ai_character else mode)
            except NarratorError as e:
                game.add_message(sconn, speaker.id, "system", f"{t.name} didn't answer: {narrator_failed(e)}")
                continue
            game.add_message(sconn, speaker.id, "heard", f'{t.name} says: "{reply}"')
            game.add_memory(sconn, [f'{t.name} (player away) answered {speaker.name}: "{reply[:300]}"'])
            for o in targets:
                if o.id != t.id:
                    game.add_message(sconn, o.id, "heard", f'{t.name} says: "{reply}"')
            if not ai_character:
                game.add_message(sconn, t.id, "reply",
                                 f'You were away, so the narrator answered {speaker.name} for you: "{reply}"')
            hub.notify(story.name, [speaker.id] + [o.id for o in targets])
        return None

    # --- Live updates -----------------------------------------------------

    @app.get("/live", response_class=HTMLResponse)
    def live_fragment(request: Request):
        """The parts of the play page that change: the chat and who is nearby."""
        user = current_user(request)
        if not user:
            return HTMLResponse("", status_code=401)
        with running_story() as (story, sconn):
            character = game.current_character(sconn, user) if story else None
            if character is None:
                return HTMLResponse("", status_code=409)  # the page reloads and finds its way
            return render(request, "_live.html", story=story, **live_context(story, sconn, character))

    @app.websocket("/ws")
    async def live_socket(ws: WebSocket):
        user = ws.session.get("user")
        st = stories.status(conn())
        character = None
        if user and st.online and accounts.account_exists(conn(), user):
            sconn = stories.open_story_db(stories.find_story(config.stories_dir, st.story))
            try:
                character = game.current_character(sconn, user)
            finally:
                sconn.close()
        if character is None:
            await ws.close(code=1008)
            return
        await ws.accept()
        handle = hub.add(st.story, character.id, ws, user)
        hub.notify_story(st.story, exclude=handle)  # presence changed
        sconn = stories.open_story_db(stories.find_story(config.stories_dir, st.story))
        try:
            owed = character.id in rounds.backfills(sconn)
        finally:
            sconn.close()
        if owed:
            asyncio.get_running_loop().run_in_executor(None, run_backfill, st.story, character.id)
        try:
            while True:
                await ws.receive_text()  # the page pings to keep tunnels from timing out
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove(st.story, character.id, handle)
            hub.notify_story(st.story)

    return app


def narrator_failed(e: NarratorError) -> str:
    return f"The narrator couldn't respond: {e}. Nothing changed; you can try again."


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
