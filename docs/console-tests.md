# Console tests (need the real PS5)

Everything else is covered by the automated tests on the fake PS5. These checks
prove the app against the real console. Run them in order; each says what to send back.

Setup on the PC: install as in the README, then `python -m cheatmaker`.
Setup on the PS5: fw 13.60, load **PS5Debug-NG 1.3.2+**. For C1-C3 have one **PS5 game (PPSA)**
and one **PS4 game (CUSA)** ready. Back up your saves first.

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C1 (spike S1) | Connect and detect | Start a PS5 game. Type the PS5's IP, press Connect. Repeat with a PS4 game (press Refresh after switching). | "Connected" within 5 s; the top line shows the right title, `PPSA…`/`CUSA…`, version, and "PS4 game" for the PS4 one. | The whole top line for both games. Then run `python tools/record_dump.py <PS5 IP> dumps\ps5game --max-mb 16` (and `dumps\ps4game`) and send the two `manifest.json` files (no .bin files needed). |
| C2 | Not reachable | Close PS5Debug-NG (or type a wrong IP), press Connect. | Within 10 s: "Can't reach the PS5. Load PS5Debug-NG…". | Nothing if it passes. |
| C3 (spike S2) | Find, set, freeze | In each game: type your gold/ammo, press Search; spend some, type the new number, Search again, until ≤10 rows. Set it to 99999. Then Freeze ammo and shoot. | First search under 60 s; value changes in the game; frozen value doesn't drop. | How long the first search took in each game, and the match counts after each search. |
| C4 (spike S3) | Lock it | Make "Infinite Health" from a found health value. My cheats → select it → Lock it → Start → take damage within 30 s. Pick Option 1 → Test it → take damage. Allow the Windows Firewall prompt (port 755) if it appears. | One or more options appear; with Test it on, health stops dropping and the game keeps running. | Which option worked, and whether enemies also stopped taking damage. |
| C5 (spike S4) | Save and CheatRunner | Keep it → Save… → Save to PC. Load **ftpsrv**, press Send to PS5. Open CheatRunner (`http://<PS5 IP>:9999`). Restart the game and turn the cheat on in CheatRunner. Do it for the PS5 game and the PS4 game. | CheatRunner lists the file and cheat by name; it works after the restart. | The file name the app made (it uses the version the PS5 reports) and whether CheatRunner picked it up. |
| C6 | Lost connection | Freeze a value, then turn off the PS5's network (or pull the cable). | Within 5 s: "Lost connection to the PS5", every cheat unticked, app still running. | Nothing if it passes. |

If anything fails: press **Copy log** and paste it into the thread.

## Known gaps until these pass

- **"I don't know the number"** works on the fake PS5 only. On the real PS5 it says it isn't ready yet: the
  first unknown-value search must run on the console (architecture Q3), and the wire format for that
  mode needs C3 data first. See `docs/implementation-notes.md`.
- The **game version** comes from PS5Debug-NG's foreground-app command. If 1.3.2 doesn't have it, the app
  falls back to the process list and shows version "unknown"; C1 tells us which.
- The main module is found by the map name `eboot.bin`; otherwise the lowest executable map is used and logged. C1's
  `manifest.json` confirms the real names for PS5 and PS4 games.
