# Coose

Coose turns any worldbuilding universe into a multiplayer, prompt-based roleplay game. An AI interprets what each player's character does and replies with only what that character would perceive or know: subjective roleplay in an objective world.

This repository holds the Coose server. It is built in milestones. So far:

- **Milestone 1:** accounts, login, story loading and the owner's command line.
- **Milestone 2:** one player, one character. Players pick or create a character and play it in a chatroom with Actions (two AI passes: a Responder judges the attempt, an Author decides the consequences) and Questions (answered only from what the character knows). The server keeps the world clock, hidden needs, each character's knowledge, and the narrator's working memory.

- **Milestone 3:** multiplayer. Each open play page holds a live connection, so chatrooms update instantly and players can see who is nearby and whether a player is there. (Dialogue was its own prompt type here; since the first server test, speech is part of an Action.) After each Action, a witness pass decides who noticed it and tells each witness only what they perceived. The narrator can introduce named characters, who become playable unless `ai_characters_public` is false.

- **Milestone 4:** time and rounds. Hidden needs run down with the world clock for every character. An Action longer than `skip_threshold_minutes` becomes a time skip: the character waits for the next round (Questions still work), everyone online is told, and the longest pending skip moves the clock at the round change. If nobody else is online, the skip happens at once. A round comes after `actions_per_round` Actions, or when every online player votes for it (`round_vote`). At each round change the AI rewrites the World State, trims its working memory, updates every character and sends each one a short in-character notice. The old World State and a summary go to `Logbook/Round N.md`, and the story database is snapshotted. Offline characters pass a skip as `offline_during_skips` says: the AI plays them, or they get a backstory when their player returns.

- **Milestone 5:** playtest tooling. `coose playtest` runs a scripted scene through the real server and AI on a scratch copy of a story and writes a transcript (see [docs/playtest.md](docs/playtest.md)). Every API call is recorded, and `coose usage` shows calls and tokens per pass.

- **Milestone 6:** story admins, caps and schedules. The owner appoints story admins (`coose admin add`), who get an Admin chatroom on the site for out-of-character commands: force a round, roll back, move the clock, edit characters and the World State, give and take items, kill and revive, remove players, approve AI-written lore, and `/intervene` or `/rewrite` to have the AI carry out any change, telling each character only what they perceive. The owner can cap a story (`coose cap`), which pauses it after a number of Actions or rounds, and give it opening hours (`coose schedule`), which open and close it while `coose serve` runs. The Premise is now cached for an hour between AI calls, and `coose usage` and playtests show estimated dollar costs.

- **Milestone 7:** collective characters, AI-written lore and the owner panel. Players can request a collective character, such as a militia or a town; once a story admin approves it (`/approve <name>`), it is played like any other but can split into parts in different places, and the narrator stops knowledge travelling between parts by unrealistic means. When the AI invents lasting lore at a round change or during `/intervene`, it writes it to `Premise/AI Additions/` for story admins to approve into the Premise or reject; it never edits the author's files. `coose panel` serves a private owner panel with the terminal's controls (stories, pause and resume, caps, schedules, story admins, accounts, AI costs) for the owner's Tailscale network.

- **After the first server test:** cheaper AI, characters that stick, and a console.
  - *Cost.* The first real session cost about $5 for 16 prompts, three quarters of it cache writes: each kind of AI pass had its own output schema, and the schema is part of the cached prefix, so every pass kind paid its own write of the whole Premise (and paid again whenever the AI added lore). Every pass now shares one output schema, so one cache serves them all; AI-written lore sits behind its own cache breakpoint; the scene is cached for a few minutes so the Author and witness passes read it instead of paying for it again; and older chat in the scene is shortened. The owner can pick the model, effort and thinking for each AI task per story (`coose ai`, or the panel), and run Actions as one combined pass instead of two.
  - *Characters.* Speech is part of an Action now; the Dialogue prompt type is gone. Each scene lists the people the game tracks, who is right there, and whether the narrator may voice them (`absent_characters` still decides for away players). The narrator adds people who matter as characters even before anyone knows their name, says who is with the acting character so "Here with" lines up, and a Question can ask it to add someone.
  - *Console.* The owner panel has a console that runs the same `coose` commands as the terminal.

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

### AI models, effort and cost

```sh
coose ai show my-story                                   # model and effort per task
coose ai set my-story witnesses --model claude-haiku-5-5 --effort low
coose ai set my-story question --model claude-sonnet-5-5 --effort low --thinking off
coose ai set my-story all --effort low                   # every task at once
coose ai passes my-story 1                               # Actions in one combined pass (cheaper)
coose ai reset my-story                                  # back to coose.toml's [ai] defaults
```

Tasks are `action` (Responder and Author), `witnesses`, `question`, `round`, `backfill` and `admin` (`/intervene`, `/rewrite`). Changes apply from the next prompt. Lower effort means less thinking, and thinking is billed as output. Claude Opus 5.5 always thinks, so lower its effort instead; other models can turn thinking off at `high` effort or below. Each model keeps its own cache of the Premise, so every model in use pays a cache write about once an hour: almost nothing on Haiku, more on Opus. Server-wide defaults go in `coose.toml` (see `[ai]` and `[ai.tasks.<task>]` in `coose.example.toml`). `coose usage` and the panel show what each pass costs.

### Story admins, caps and schedules

```sh
coose admin add ray my-story             # ray gets the Admin chatroom for my-story
coose cap my-story --actions 200         # pause after 200 more Actions (or --rounds 10; --clear to remove)
coose schedule add my-story fri,sat 19:00 23:00   # opening hours, in the server's local time
coose schedule list
```

Accounts listed under `admins` in a story's `Settings.yaml` are story admins too. A capped story stays paused until the owner raises or clears the cap. Schedules only act when an opening starts or ends, so a manual pause or resume in between is left alone; when openings overlap, the one that started later wins. Story admins type `/help` in their Admin chatroom for the list of commands.

### The owner panel

```sh
export COOSE_PANEL_PASSWORD="..."        # the panel's own password
coose panel                               # serves [panel] host:port from coose.toml
```

The panel's **Console** runs any `coose` command (`status`, `usage`, `cap ...`, `ai ...`, `--help`) as if typed in the server's terminal; `serve`, `panel` and `playtest` stay in the terminal. It can't answer prompts, so add `--yes` to `rollback`. Each story's card also has an **AI models and effort** section.

Set `[panel] host` in `coose.toml` to this machine's Tailscale address (`tailscale ip -4`) so only your tailnet can reach it. Never point the Cloudflare tunnel at the panel's port. `coose panel` refuses to listen on every network (`0.0.0.0`). It runs alongside `coose serve` as a second process.

Only one story runs at a time. Pausing or switching never deletes anything; players who log in meanwhile see a "story offline" notice.

To put the site on the internet, point a Cloudflare tunnel at the `host:port` in `coose.toml`. Leave `host` as `127.0.0.1` so the server is only reachable through the tunnel. Cloudflare tunnels carry the WebSocket at `/ws` that live updates use without extra setup. Run a single server process: who is online is kept in memory.

## Development

```sh
pip install -e '.[dev]'
pytest
```

The design is described in the Coose design spec; the pages use the Waybar Gold stylesheet in `src/coose/web/static/style.css`, pinned to the dark theme.
