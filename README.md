# Coose

Coose turns any worldbuilding universe into a multiplayer, prompt-based roleplay game. An AI interprets what each player's character does and replies with only what that character would perceive or know: subjective roleplay in an objective world.

This repository holds the Coose server. It is built in milestones; this is **milestone 1**: accounts, login, story loading and the owner's command line. Play itself (characters, prompts, the AI) arrives in milestone 2.

## Setup

Requires Python 3.11 or newer.

```sh
git clone https://github.com/bravo945/Coose.git && cd Coose
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp coose.example.toml coose.toml          # then edit it if needed
export COOSE_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

Keep `COOSE_SECRET_KEY` the same between restarts (for example in a systemd `Environment=` line), or everyone is logged out each time the server restarts.

## Stories

Each story is a folder in the `stories/` directory, built from [`story-template/`](story-template):

```sh
cp -r story-template stories/my-story
```

Write the Premise, Rulebook and characters with any tools you like, then set `display_name` and the other options in `Rulebook/Settings.yaml`. The server keeps its live data for a story in `stories/<story>/.coose/`.

## Running the server

```sh
coose account add mara        # prompts for a password; players can't sign up
coose stories                 # lists story folders and any problems
coose run my-story            # makes it the running story
coose serve                   # starts the player site on host:port from coose.toml
```

Other owner commands: `coose pause`, `coose resume`, `coose status`, `coose account list`, `coose account password <name>`, `coose account remove <name>`.

Only one story runs at a time. Pausing or switching never deletes anything; players who log in meanwhile see a "story offline" notice.

To put the site on the internet, point a Cloudflare tunnel at the `host:port` in `coose.toml`. Leave `host` as `127.0.0.1` so the server is only reachable through the tunnel.

## Development

```sh
pip install -e '.[dev]'
pytest
```

The design is described in the Coose design spec; the pages use the Waybar Gold stylesheet in `src/coose/web/static/style.css`, pinned to the dark theme.
