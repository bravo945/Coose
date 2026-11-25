# Playtesting

`coose playtest` runs a scripted scene through the real server and the real Claude API, then writes a Markdown transcript of everything each character's chatroom received. Use it to check the narrator's judgement, the subjective rule, timing and cost before real players arrive.

```sh
export ANTHROPIC_API_KEY=...          # the same key the server uses
coose playtest my-story examples/playtest.yaml --out playtest.md
```

- It works on a scratch copy of the story, so the real story's live data, Logbook and snapshots are untouched. Add `--keep` to keep the copy and inspect it.
- Players are simulated through the same pages real players use. A player with `online: true` (the default) holds a live connection, so they count as present for votes and skips, and the narrator won't speak for them. `online: false` means nobody is at the keyboard.
- Steps run in order. A step is `user: prompt`, where the prompt starts with `!` (Action) or `?` (Question). `""` is shorthand for speech, which is an Action: `"" Hello` becomes `! I say, "Hello"`; `vote: [users]`; or `wait: seconds`.
- `settings` overrides the story's Settings.yaml for this run only.
- A prompt the server refuses (for example while a character is in a time skip) is shown as `refused:` with the reason.

## What to look for in the transcript

- **The subjective rule.** Does anyone learn something their character couldn't know? Watch witness narrations, Questions and round notices closely.
- **Judgement.** Are outcomes realistic and in line with the Rulebook? Is the time each Action takes believable, and do long ones become skips?
- **Speech and people.** Do the people your character talks to answer in character, does `absent_characters` hold for away players, and does everyone who matters show up under "Here with"?
- **Rounds.** Is the new World State faithful? Did the working memory keep what matters and drop the rest? Is the Logbook entry useful?
- **Cost and speed.** The AI usage table at the end shows calls, tokens and average seconds per pass. Cache reads are the Premise and Rulebook reused between calls.

## Usage on the live server

Every API call the server makes is recorded. `coose usage` shows totals per pass, and `--story` and `--since 2026-11-01` narrow it down.
