# Cheat Maker

A Windows PC app that helps you make cheats for offline, single-player games on a
jailbroken PS5 (PS5 games and PS4 games running on the PS5), test them live, and
save them as `.json` / `.shn` files that [CheatRunner](https://github.com/notmaj0r/CheatRunner) loads.

Licence: GPL-3.0 (see `LICENSE`).

## What you need

- A jailbroken PS5 (tested target: firmware 13.60) running **PS5Debug-NG 1.3.2 or newer** (port 744).
- Optional: the **ftpsrv** payload (port 2121) for "Send to PS5". Without it, save to the PC and copy the file yourself.
- CheatRunner on the PS5 to run the saved cheats.

## Run from source (Windows)

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m cheatmaker
```

Settings and logs live in `%APPDATA%\CheatMaker`. Saved cheats go to `Documents\CheatMaker\cheats` by default.

## How it works

1. **Connect**: type the PS5's IP. The app shows the running game, its title ID, version and whether it's a PS4 or PS5 game.
2. **Find a value**: type the number you see (gold, ammo), change it in the game, type the new number, until a few matches are left. Or press "I don't know the number" and use Went up / Went down.
3. **Set / Freeze** a match, or **Make a cheat** from a template (Infinite Health, Infinite Money, Infinite Ammo, Max Skill Points, Set to a Number).
4. **My cheats**: tick to test live. Cheats marked "This session only" live in memory that moves every time the game starts; **Lock it** finds the game code that changes the value and switches it off, which makes a cheat that works after a restart.
5. **Save** as `{titleid}_{version}.json` (or `.shn`), then **Send to PS5** (uploads to `/data/cheatrunner/cheats/json/`).

## Development

```
pytest -q            # 150+ tests, no console needed (FakeConsole)
ruff check src tests tools && ruff format --check src tests tools
```

- `src/cheatmaker/console/`: `ConsoleClient` interface, `FakeConsole`, the real `Ps5DebugNgClient`, uploader.
- `src/cheatmaker/core/`: session, scanner, freezer, Lock it, cheat model, templates.
- `src/cheatmaker/formats/`: GoldHEN `.json` and `.shn`, tested against real files in `tests/fixtures/golden/`.
- `src/cheatmaker/ui/`: PySide6 screens. No protocol code here.
- `tools/record_dump.py`: record a real game's memory for `FakeConsole.from_dump()`.
- `docs/console-tests.md`: checks that need a real PS5.
- `.claude/agents/`: the five workflow agents (requirements-challenger, architect, feature-implementor, pr-reviewer, qa-engineer).

The PS5Debug-NG wire format follows PS5Debug-NG's PROTOCOL.md and was cross-checked
against the GPL-3.0 `ps5dbg` package.
