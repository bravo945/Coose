# Coose

Coose turns any worldbuilding universe into a multiplayer, prompt-based roleplay game. An AI interprets what each player's character does and replies with only what that character would perceive or know: subjective roleplay in an objective world.

This repository holds the Coose server. It is built in milestones. So far:

- **Milestone 1:** accounts, login, story loading and the owner's command line.
- **Milestone 2:** one player, one character. Players pick or create a character and play it in a chatroom with Actions (two AI passes: a Responder judges the attempt, an Author decides the consequences) and Questions (answered only from what the character knows). The server keeps the world clock, hidden needs, each character's knowledge, and the narrator's working memory.

- **Milestone 3:** multiplayer. Each open play page holds a live connection, so chatrooms update instantly and players can see who is nearby and whether a player is there. Dialogue reaches characters at the same location; when a recipient's player is away, the narrator answers for them as `absent_characters` allows (AI-created characters always answer). After each Action, a witness pass decides who noticed it and tells each witness only what they perceived. The narrator can introduce named characters, who become playable unless `ai_characters_public` is false.

- **Milestone 4:** time and rounds. Hidden needs run down with the world clock for every character. An Action longer than `skip_threshold_minutes` becomes a time skip: the character waits for the next round (Questions still work), everyone online is told, and the longest pending skip moves the clock at the round change. If nobody else is online, the skip happens at once. A round comes after `actions_per_round` Actions, or when every online player votes for it (`round_vote`). At each round change the AI rewrites the World State, trims its working memory, updates every character and sends each one a short in-character notice. The old World State and a summary go to `Logbook/Round N.md`, and the story database is snapshotted. Offline characters pass a skip as `offline_during_skips` says: the AI plays them, or they get a backstory when their player returns.

- **Milestone 5:** playtest tooling. `coose playtest` runs a scripted scene through the real server and AI on a scratch copy of a story and writes a transcript (see [docs/playtest.md](docs/playtest.md)). Every API call is recorded, and `coose usage` shows calls and tokens per pass.

Story admin commands, schedules and caps arrive in milestone 6.

## Setup

Requires Python 3.11 or newer.

```sh
git clone https://github.com/bravo945/Coose.git && cd Coose
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp coose.example.toml coose.toml          # then edit it if needed
export ANTHROPIC_API_KEY="..."            # the Claude API key that pays for narration
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
coose rounds my-story         # rounds the story can be rolled back to
coose rollback my-story 3     # undo everything after the start of round 3 (pause the story first)
coose usage                   # Claude API calls and tokens so far, per pass
coose playtest my-story examples/playtest.yaml   # scripted scene with the real AI
coose stories                 # lists story folders and any problems
coose run my-story            # makes it the running story
coose serve                   # starts the player site on host:port from coose.toml
```

Other owner commands: `coose pause`, `coose resume`, `coose status`, `coose account list`, `coose account password <name>`, `coose account remove <name>`.

Only one story runs at a time. Pausing or switching never deletes anything; players who log in meanwhile see a "story offline" notice.

To put the site on the internet, point a Cloudflare tunnel at the `host:port` in `coose.toml`. Leave `host` as `127.0.0.1` so the server is only reachable through the tunnel. Cloudflare tunnels carry the WebSocket at `/ws` that live updates use without extra setup. Run a single server process: who is online is kept in memory.

## Development

```sh
pip install -e '.[dev]'
pytest
```

The design is described in the Coose design spec; the pages use the Waybar Gold stylesheet in `src/coose/web/static/style.css`, pinned to the dark theme.
