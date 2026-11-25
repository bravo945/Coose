"""The owner's command line: `coose ...` on the server machine."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import accounts, controls, db, rounds, stories
from .config import Config

app = typer.Typer(help="Run and manage a Coose server.", no_args_is_help=True)
account_app = typer.Typer(help="Create and manage player accounts.", no_args_is_help=True)
app.add_typer(account_app, name="account")
admin_app = typer.Typer(help="Appoint story admins.", no_args_is_help=True)
app.add_typer(admin_app, name="admin")
schedule_app = typer.Typer(help="Opening hours: when stories open and close by themselves.", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")

_state: dict = {}


@app.callback()
def main(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to coose.toml (default: ./coose.toml)."
    ),
) -> None:
    _state["config"] = Config.load(config)


def _cfg() -> Config:
    return _state["config"]


def _conn():
    return db.connect(_cfg().server_db_path)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


# --- Accounts ---------------------------------------------------------------

@account_app.command("add")
def account_add(
    username: str,
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True, help="Leave out to be prompted."
    ),
) -> None:
    """Create a player account."""
    try:
        accounts.create_account(_conn(), username, password)
    except accounts.AccountError as e:
        _fail(str(e))
    typer.echo(f"Created account '{username}'.")


@account_app.command("remove")
def account_remove(username: str) -> None:
    """Delete a player account."""
    try:
        accounts.delete_account(_conn(), username)
    except accounts.AccountError as e:
        _fail(str(e))
    typer.echo(f"Removed account '{username}'.")


@account_app.command("password")
def account_password(
    username: str,
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True, help="Leave out to be prompted."
    ),
) -> None:
    """Set a new password for an account."""
    try:
        accounts.set_password(_conn(), username, password)
    except accounts.AccountError as e:
        _fail(str(e))
    typer.echo(f"Password changed for '{username}'.")


@account_app.command("list")
def account_list() -> None:
    """List all accounts."""
    names = accounts.list_accounts(_conn())
    typer.echo("\n".join(names) if names else "No accounts yet.")


# --- Story admins -------------------------------------------------------------

def _story(name: str) -> stories.Story:
    try:
        return stories.find_story(_cfg().stories_dir, name)
    except stories.StoryError as e:
        _fail(str(e))


@admin_app.command("add")
def admin_add(username: str, story: str) -> None:
    """Make USERNAME a story admin of STORY."""
    s = _story(story)
    try:
        controls.add_admin(_conn(), username, s.name)
    except controls.ControlError as e:
        _fail(str(e))
    typer.echo(f"'{username}' is now a story admin of '{s.display_name}'.")


@admin_app.command("remove")
def admin_remove(username: str, story: str) -> None:
    """Stop USERNAME being a story admin of STORY."""
    s = _story(story)
    try:
        controls.remove_admin(_conn(), username, s.name)
    except controls.ControlError as e:
        _fail(str(e))
    typer.echo(f"'{username}' is no longer a story admin of '{s.display_name}'.")


@admin_app.command("list")
def admin_list(story: str) -> None:
    """List STORY's admins, including any listed in its Settings.yaml."""
    names = controls.admins(_conn(), _story(story))
    typer.echo("\n".join(names) if names else "No story admins yet.")


# --- Caps and schedules ---------------------------------------------------------

@app.command("cap")
def cap(
    story: str,
    actions: Optional[int] = typer.Option(None, "--actions", help="Pause after this many more Actions."),
    rounds_: Optional[int] = typer.Option(None, "--rounds", help="Pause after this many more round changes."),
    clear: bool = typer.Option(False, "--clear", help="Remove the cap."),
) -> None:
    """Limit how much STORY can be played before it pauses itself. Caps never reset on their own."""
    s = _story(story)
    if clear:
        controls.clear_cap(_conn(), s.name)
        typer.echo(f"'{s.display_name}' has no cap now.")
        return
    try:
        controls.set_cap(_conn(), s, actions, rounds_)
    except controls.ControlError as e:
        _fail(str(e))
    typer.echo(f"'{s.display_name}': {controls.describe_cap(_conn(), s)}.")


@schedule_app.command("add")
def schedule_add(
    story: str,
    days: str = typer.Argument(..., help="daily, weekdays, weekends, or days like fri,sat"),
    start: str = typer.Argument(..., metavar="FROM", help="Opening time, e.g. 19:00"),
    end: str = typer.Argument(..., metavar="TO", help="Closing time, e.g. 23:00 (earlier than FROM runs past midnight)"),
) -> None:
    """Open STORY on DAYS from FROM to TO, in the server machine's local time."""
    s = _story(story)
    try:
        span = controls.add_span(_conn(), s.name, days, start, end)
    except controls.ControlError as e:
        _fail(str(e))
    typer.echo(f"'{s.display_name}' opens {span.describe()}. Schedules run while `coose serve` is running.")


@schedule_app.command("list")
def schedule_list() -> None:
    """Show every story's opening hours."""
    found = controls.all_schedules(_conn())
    if not found:
        typer.echo("No schedules.")
    for name, spans in found.items():
        typer.echo(f"{name}: " + "; ".join(s.describe() for s in spans))


@schedule_app.command("clear")
def schedule_clear(story: str) -> None:
    """Remove all of STORY's opening hours."""
    s = _story(story)
    controls.clear_schedule(_conn(), s.name)
    typer.echo(f"'{s.display_name}' has no schedule now.")


# --- Stories ----------------------------------------------------------------

@app.command("stories")
def list_stories() -> None:
    """List story folders and whether each one can run."""
    found = stories.list_stories(_cfg().stories_dir)
    if not found:
        typer.echo(f"No stories in {_cfg().stories_dir}. Copy story-template/ there to start one.")
        return
    for s in found:
        mark = "ok" if s.valid else "problems: " + "; ".join(s.problems)
        typer.echo(f"{s.name}  ({mark})")


@app.command("run")
def run(story: str) -> None:
    """Make STORY the running story. The previous one is paused and saved."""
    if controls.story_cap_reached(_conn(), _story(story)):
        _fail(f"'{story}' has reached its cap. Raise it with `coose cap {story} ...` or remove it with --clear.")
    try:
        s = stories.run_story(_conn(), _cfg().stories_dir, story)
    except stories.StoryError as e:
        _fail(str(e))
    typer.echo(f"Now running '{s.display_name}'.")


@app.command("pause")
def pause() -> None:
    """Pause the running story. Players see a 'story offline' notice."""
    try:
        stories.pause(_conn())
    except stories.StoryError as e:
        _fail(str(e))
    typer.echo("Story paused.")


@app.command("resume")
def resume() -> None:
    """Resume the running story."""
    st = stories.status(_conn())
    if st.story and controls.story_cap_reached(_conn(), _story(st.story)):
        _fail(f"'{st.story}' has reached its cap. Raise it with `coose cap {st.story} ...` or remove it with --clear.")
    try:
        stories.resume(_conn())
    except stories.StoryError as e:
        _fail(str(e))
    typer.echo("Story resumed.")


@app.command("rounds")
def list_rounds(story: str) -> None:
    """List the rounds STORY can be rolled back to."""
    try:
        s = stories.find_story(_cfg().stories_dir, story)
    except stories.StoryError as e:
        _fail(str(e))
    available = rounds.snapshots(s.path)
    typer.echo(f"Rounds you can roll back to: {', '.join(map(str, available))}" if available
               else "No snapshots yet. The story hasn't been run.")


@app.command("rollback")
def rollback(
    story: str,
    round_number: int = typer.Argument(..., metavar="ROUND"),
    yes: bool = typer.Option(False, "--yes", help="Don't ask for confirmation."),
) -> None:
    """Restore STORY to the start of ROUND. Everything after that is undone."""
    try:
        s = stories.find_story(_cfg().stories_dir, story)
    except stories.StoryError as e:
        _fail(str(e))
    st = stories.status(_conn())
    if st.story == s.name and st.online:
        _fail(f"'{s.name}' is online. Pause it first with: coose pause")
    if not yes:
        typer.confirm(f"Undo everything in '{s.display_name}' after the start of round {round_number}?", abort=True)
    sconn = stories.open_story_db(s)
    try:
        rounds.rollback(s.path, sconn, round_number)
    except rounds.RoundError as e:
        _fail(str(e))
    finally:
        sconn.close()
    typer.echo(f"'{s.display_name}' is back at the start of round {round_number}.")


@app.command("status")
def status() -> None:
    """Show which story is running and whether it is paused."""
    st = stories.status(_conn())
    if st.story is None:
        typer.echo("No story is running.")
    else:
        typer.echo(f"Story: {st.story}  ({'paused' if st.paused else 'online'})")
        s = _story(st.story)
        typer.echo(f"Cap: {controls.describe_cap(_conn(), s)}")
        admins = controls.admins(_conn(), s)
        typer.echo(f"Story admins: {', '.join(admins) or 'none'}")
    for name, spans in controls.all_schedules(_conn()).items():
        typer.echo(f"Schedule for {name}: " + "; ".join(sp.describe() for sp in spans))
    typer.echo(f"Accounts: {len(accounts.list_accounts(_conn()))}")


@app.command("usage")
def usage(
    story: Optional[str] = typer.Option(None, "--story", help="Only this story."),
    since: Optional[str] = typer.Option(None, "--since", help="Only calls on or after this date, e.g. 2026-11-01."),
) -> None:
    """Show how many Claude API calls and tokens play has used, per pass."""
    rows = db.usage_summary(_conn(), story, since)
    if not rows:
        typer.echo("No AI calls recorded yet.")
        return
    typer.echo(f"{'pass':<10} {'calls':>6} {'input':>10} {'output':>10} {'cache read':>11} {'cache write':>12} "
               f"{'avg s':>6} {'est. $':>8}")
    for r in rows:
        cost = "?" if r["cost"] is None else f"{r['cost']:.2f}"
        typer.echo(f"{r['pass']:<10} {r['calls']:>6} {r['input']:>10} {r['output']:>10} {r['cache_read']:>11} "
                   f"{r['cache_write']:>12} {r['avg_seconds']:>6} {cost:>8}")
    typer.echo(f"Estimated total: ${sum(r['cost'] or 0 for r in rows):.2f}")
    typer.echo("Input counts tokens sent fresh; cache reads are the cached Premise and Rulebook, billed at a "
               "fraction of the input price. Dollar figures are estimates; the Claude Console has the real bill.")


@app.command("playtest")
def playtest(
    story: str,
    scenario: Path = typer.Argument(..., exists=True, dir_okay=False, help="Scenario YAML (see docs/playtest.md)."),
    out: Path = typer.Option(Path("playtest.md"), "--out", "-o", help="Where to write the transcript."),
    keep: bool = typer.Option(False, "--keep", help="Keep the scratch copy of the story to inspect."),
) -> None:
    """Run a scripted scenario through the real server and AI on a scratch copy of STORY."""
    from . import playtest as pt

    cfg = _cfg()
    try:
        plan = pt.Scenario.load(scenario)
        scratch = pt.run(cfg, story, plan, out, keep=keep, log=typer.echo)
    except (pt.ScenarioError, stories.StoryError) as e:
        _fail(str(e))
    typer.echo(f"Transcript written to {out}")
    if keep:
        typer.echo(f"Scratch copy kept at {scratch}")


@app.command("serve")
def serve() -> None:
    """Start the web server for players."""
    import uvicorn

    from .web.app import create_app

    cfg = _cfg()
    if not cfg.secret_key():
        _fail(f"Set the {cfg.secret_key_env} environment variable before serving (see coose.example.toml).")
    uvicorn.run(create_app(cfg, run_scheduler=True), host=cfg.host, port=cfg.port, proxy_headers=True)


if __name__ == "__main__":
    app()
