"""The owner's command line: `coose ...` on the server machine."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import accounts, db, rounds, stories
from .config import Config

app = typer.Typer(help="Run and manage a Coose server.", no_args_is_help=True)
account_app = typer.Typer(help="Create and manage player accounts.", no_args_is_help=True)
app.add_typer(account_app, name="account")

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
    typer.echo(f"{'pass':<10} {'calls':>6} {'input':>10} {'output':>10} {'cache read':>11} {'cache write':>12} {'avg s':>6}")
    for r in rows:
        typer.echo(f"{r['pass']:<10} {r['calls']:>6} {r['input']:>10} {r['output']:>10} {r['cache_read']:>11} "
                   f"{r['cache_write']:>12} {r['avg_seconds']:>6}")
    typer.echo("Input counts tokens sent fresh; cache reads are the cached Premise and Rulebook, billed at a "
               "fraction of the input price.")


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
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, proxy_headers=True)


if __name__ == "__main__":
    app()
