# Cheat Maker: getting started

This guide shows you how to make your own cheats for offline, single-player games on a
jailbroken PS5, using the Cheat Maker app on your Windows PC. You don't need to know
anything about memory or code. If you can read a number off the screen, you can make a
cheat.

It works for any game: PS5 games (title IDs that start with PPSA) and PS4 games running
on the PS5 (CUSA). We use **Returnal** as the worked example near the end, because it is
one of the harder games and shows every trick you may need.

> **Back up your saves first.** A wrong cheat can crash a game or damage a save file. Use
> cheats only in offline, single-player games.

---

## 1. What you need

| On the PS5 | On the PC |
|---|---|
| A jailbroken PS5 (tested on firmware 13.60) | Windows PC on the same network as the PS5 |
| **PS5Debug-NG 1.3.2 or newer** loaded | Cheat Maker (see the README for how to run it) |
| **CheatRunner**, to use your cheats without the PC | |
| Optional: the **ftpsrv** payload, so the app can send files straight to the PS5 | |

You also need the PS5's IP address. On the PS5 go to **Settings > Network > Connection
Status** and write it down (for example `192.168.0.193`).

---

## 2. The screen at a glance

- **Top bar**: type the PS5's IP and press **Connect**. Once connected it shows the game
  that's running, its title ID, its version, and whether it's a PS4 or PS5 game. Press
  **Refresh** after you start a different game.
- **Tab 1. Find a value**: where you hunt down a number (gold, ammo, health) in the
  game's memory.
- **Tab 2. My cheats**: the cheats you've made. Tick one to turn it on, then save.
- **Bottom line**: a short message telling you what to do next. If you're ever stuck, read
  it. **Copy log** copies the app's log for a bug report.

Every screen has a one-line hint at the top. The app also lights up the button it thinks
you should press next. You can always ignore it and press something else.

---

## 3. The suggested workflow

If this is your first time, follow these steps in order. Start with an **easy value in an
easy game** so you learn the moves before you take on a hard one.

1. **Pick a good first target.** A number you can see and change easily: money, ammo, a
   crafting material. Avoid health for your first try (it often hides as a decimal).
2. **Connect** and check that the game name and version look right.
3. **Find the value** on tab 1 by searching, changing it in the game, and searching again
   until only a few matches are left.
4. **Try it out** with **Set** or **Freeze** before you make a cheat. If the number on
   screen changes and stays changed, you found the real one.
5. **Make a cheat** from it (Infinite Money, Infinite Ammo, and so on).
6. Go to **My cheats** and look at the **Works after restart?** column:
   - **Yes**: you're done. Go to step 8.
   - **This session only**: the value moves every time the game starts. Do step 7.
7. **Make it stick**, in one of two ways:
   - **Lock it**: finds the game code that changes the value and switches it off. This
     makes a cheat CheatRunner can run with the PC off.
   - **Find it next time**: teaches the app how the game keeps the value, so it finds it
     again by itself after a restart or a new area (this needs the PC connected).
8. **Save** the cheats and **Send to PS5** (or copy the file yourself). Start the game
   with CheatRunner and switch the cheat on.

The rest of this guide explains each step.

---

## 4. Connect

1. Load PS5Debug-NG on the PS5.
2. Start the game and get into actual gameplay (not the title screen).
3. In Cheat Maker, type the PS5's IP and press **Connect**.

You should see something like:

`Returnal · PPSA01285 · version 03.000.005 · PS5 game · eboot.bin`

If it says **"No game running"**, start the game and press **Refresh**. The app remembers
the IP for next time.

---

## 5. Find a value (tab 1)

### 5.1 Answer "What does the game show?"

The Find tab first asks what the game shows for this value:

- **A number**: you can read it, like `2,450` gold. Use a number search (5.2).
- **A bar or meter**: like a health bar with no number. Use "I don't know the number"
  (5.3).
- **Nothing**: a hidden value. Also use "I don't know the number" (5.3).

### 5.2 When you can see the number

1. Type the number you see (for example `2450`) and press **Search**.
   - The first search reads the whole game. On big PS5 games it can take **up to two or
     three minutes**. The progress bar shows the time left. Don't touch the controller.
2. You'll get lots of matches. Go back to the game and **change the number**: spend some
   gold, fire a shot, pick something up.
3. Type the **new** number and press **Search again**.
4. Repeat until only a handful are left. Usually 3 to 5 rounds is enough.

Once there are few enough (you choose how many with **Show up to**), the list shows each
match with its **Value now**, updated live. Watch the list while you change the number in
the game. The real one moves with it.

**Know how much it changed but not the new total?** Type the amount in **Know how much it
changed?** and press **Went up by** or **Went down by**.

### 5.3 When you can't see the number

1. Press **I don't know the number**. The app takes a snapshot of the game.
2. Make the value change in the game (take a hit, use some stamina).
3. Press **Went up**, **Went down**, **Changed** or **Same** to say what happened.
4. Repeat. To clear out values that change on their own (timers, animations), press
   **Stand still** and leave the controller alone while it runs.

If you know roughly where the value sits (say, between 1 and 1000), put that in
**Advanced > Don't know the number, but it's between ... and ...** to start with fewer
matches.

### 5.4 When the search finds nothing

Games don't always store the number you see. The app's hint will suggest the next thing
to try, and **Try it** does it for you. In order, it tries:

1. the number as shown,
2. a fraction from 0 to 1 (a bar at 87% is stored as 0.87),
3. the number x10,
4. the number x100,
5. an unknown value (Went up / Went down).

You can set these yourself under **Advanced**:

- **Number type**: "Pick for me" tries the most common types. If you know better, pick
  one: *Whole number* (1, 2, 4 or 8 bytes) for counters, *Decimal* (4 or 8 bytes) for
  health, stamina and anything that can be fractional.
- **Game stores it as**: the number shown, a fraction (0 to 1) of max, x10, x100 or x1000.

### 5.5 When several matches all move together

Many games keep a **copy** of a value just for the screen. The copy changes along with the
real one, so searching can't tell them apart. Two ways to find the real one:

- **Set test**: pick a row, type a new value, press **Set**. If the number on screen
  jumps back the next time it changes, that row was a copy.
- **Test the hits** (easier): the app writes a test value to half of the matches for a
  moment, then asks *"Did the number in the game change?"*. Answer **Yes, it changed**,
  **No** or **Not sure**. It halves the list each time, so 500 matches take about 9
  questions. Every value is put back after each question. When it's done, the real
  address is added to the list.

> Test the hits writes to the game's memory. Save your game first.

### 5.6 Try it, then make a cheat

Pick the row you think is right:

- **Set**: type a value and press Set to change it once.
- **Freeze**: hold it at its current value. Press again (**Unfreeze**) to let go.
- **Make a cheat**: pick a template and press it. The cheat appears in **My cheats**.

| Template | What it does |
|---|---|
| Infinite Health | Keeps your health where it is now |
| Infinite Money | Sets your money high (999,999) and keeps it there |
| Infinite Ammo | Keeps your ammo where it is now |
| Max Skill Points | Gives you lots of skill points once |
| Set to a Number | Sets the value to a number you choose |

If you type a value in the **New value** box before pressing **Make a cheat**, the cheat
uses that value instead.

### 5.7 Other tools on the Find tab

- **Filter list by value**: show only rows near a number (`100` or `90-110`).
- **Add address**: type an address you got from somewhere else to watch it.
- **Pointer scan**: looks for a path from the game's fixed memory to this value, so you
  can reach it after a restart. Run it, restart the game, find the value again, then
  press **Re-check with this address** to keep only the paths that still work. This takes
  a few minutes. Most people won't need it: **Find it next time** (section 6.3) does the
  same job for you.
- **Scan files**: every scan (the first one and each one after it) saves its matches,
  address in hex and value, to its own text file: `scan-001.txt`, `scan-002.txt`, and so
  on. Each new search gets a new folder under
  `Documents\CheatMaker\scans\<title id>_<version>\<date and time>`. The button opens this
  search's folder; hover over the match count to see the last file. A search with millions
  of matches lists the first 100,000 and says how many there were in all. To turn the
  files off or change that number, set `save_scan_files` or `scan_file_max` in
  `%APPDATA%\CheatMaker\settings.json`.
- **Search speed**: a search runs on 4 connections to the PS5 at once and asks
  PS5Debug-NG to read the game's memory in place. If a search ever hangs or the game
  stutters, set `"scan_connections": 1` and/or `"fast_scan": false` in the same
  `settings.json` and reconnect.

---

## 6. My cheats (tab 2)

Each cheat has:

- **On**: tick to turn it on in the game right now, untick to turn it off.
- **Name** and **Value**: double-click to change them.
- **Works after restart?**
  - **Yes**: the value lives in the same place every time. You can save it as it is.
  - **This session only**: the value moves every time the game starts. It works now but
    can't be saved for CheatRunner yet.
  - **Found again each run**: the app has learned how to find it after a restart (6.3).

### 6.1 Lock it

For a "This session only" cheat, **Lock it** finds the game code that changes the value
and switches that code off. The result is a normal cheat file CheatRunner can run with
the PC off.

1. Select the cheat and press **Lock it**.
2. Press **Start**, then make the value change in the game within 30 seconds (take a hit
   for health, spend money for money).
3. The app lists the bits of game code it caught changing the value. Pick the first
   option and press **Test it**, then play a little:
   - If the value stops changing, press **Keep it**.
   - If it still changes, pick the next option and **Test it** again.
   - **Test all at once** switches on every option, which is a quick way to see whether
     any of them works.
4. If the game acts strangely (enemies don't take damage, the game freezes), try another
   option.

**Enemies stopped taking damage too?** Many games use the same code for you and for every
enemy. Press **Player only...**, then **Collect** and fight for 20 seconds: get hit *and*
hit enemies. The app finds what marks you apart from enemies and changes only your value.
Pick the top field, press **Test it**, take a hit (your value should stay, enemies should
still die), then **Keep it**.

### 6.2 Find it again

**Find it again** looks for all your remembered values in the game now. Before changing
anything after a restart, the app asks once: *"Is this your health? It shows 100 right
now."* Take a hit and watch the number. Press **Yes** only if it changes with yours. If it
found more than one candidate, it asks you to pick the one that changes.

### 6.3 Find it next time

For values that move every restart or every new area, **Find it next time** teaches the
app how the game keeps them.

1. Select the cheat and press **Find it next time**.
2. Press **Start** and play normally for about a minute. Take a hit if you can.
   - Tick *Also look for a memory path* to try a slower extra method on big games.
3. When it's done it tells you how it will find the value next time, for example *"Ready:
   health will be found by the game's code."*

Next time you connect, the app finds the value by itself, checks it, and asks you once
before changing anything.

While connected, the app adds a small hook to the game's code to do this. It's taken out
when you disconnect or close Cheat Maker.

**Also make a PC-off cheat**: if this button shows up after learning, press it. It makes a
cheat from the same place in the game's code that keeps your value (only yours), which
CheatRunner can run with the PC off.

### 6.4 Save and send

Press **Save...**:

- **Format**: `.json` (recommended) or `.shn`. The file is named for the game and version,
  like `PPSA01285_03.000.005.json`, which is what CheatRunner looks for.
- **Save to PC** puts it in `Documents\CheatMaker\cheats` (or a folder you choose).
- **Send to PS5** uploads it to the PS5 if ftpsrv is running. If not, copy the saved file
  to `/data/cheatrunner/cheats/json/` on the PS5 yourself.

"This session only" cheats aren't saved; Lock them first. Cheats that are found again each
run are kept in the app's own notes for this game, and come back the next time you
connect.

### 6.5 After a game update

When you open a cheat file made for an older version of the game, a warning appears with
an **Update for this version** button. It finds your cheats in the new version's code and
moves them there. Tick each one to test it, then **Save**: the new file is named for the
new version.

---

## 7. Worked example: Returnal (PS5)

Returnal is a hard game for cheats. Its values move every time you restart or reach a new
biome, the screen shows copies of the real numbers, and health is a decimal. We'll do
**Obolites** (the currency) first, then **health**.

Connect, and check the top bar shows `Returnal · PPSA01285 · version 03.000.005 · PS5
game`. Your version may be different; that's fine.

### 7.1 Obolites: a currency with a trap

Say you have 10 Obolites.

1. On **Find a value**, press **A number**, type `10` and press **Search**. Wait for the
   first search (about two minutes on Returnal).
2. Pick up some Obolites. You now have 51. Type `51`, press **Search again**.
3. Spend or collect more: 13, then 35. After a few rounds about 4 matches are left.
4. Press **Freeze** on one and pick up another Obolite.

**The trap.** With "Pick for me", the app searches 4-byte whole numbers first, and in
Returnal those 4 matches are all **screen copies**. Freezing them holds the copy, but the
real count snaps back the moment you pick something up. The hint says so: *"If the game
puts the number back, search again with Advanced > 8-byte whole number."*

5. Press **Start over**. Open **Advanced** and set **Number type** to **Whole number (8
   bytes)**.
6. Search for your current Obolites again, change it in the game, search again. This
   time you'll get down to about 2 matches. These are the real counter.
7. Select one, type `1000000` in **New value**, pick **Infinite Money**, and press **Make a
   cheat**.

In **My cheats**, tick it. Your Obolites jump and stay there.

### 7.2 Health: a decimal that moves every restart

Returnal shows health as a number (say 100), but stores it as a decimal, and in a
different place every time the game starts.

1. Press **Start over**. In **Advanced**, set **Number type** to **Decimal (4 bytes)**.
   (With "Pick for me", 100 would be tried as a whole number first and find thousands of
   wrong matches.)
2. Type `100` and press **Search**.
3. Take a hit. Health is now 80. Type `80`, **Search again**. Decimal searches allow for
   rounding, so 79.6 on screen still matches 80.
4. Repeat until only a few are left. If they all move together, press **Test the hits**
   and answer its questions.
5. Select the real one, pick **Infinite Health**, and press **Make a cheat**.

In **My cheats** the health cheat says **This session only**: it'll be lost when you
restart or reach a new biome. You have two ways to keep it:

- **Lock it** (works with the PC off): press **Lock it**, **Start**, take a hit within 30
  seconds. Test the options one by one. In games like Returnal, the code that lowers your
  health usually lowers enemies' health too. If enemies stop dying, use **Player only...**
  so only you are protected.
- **Find it next time** (needs the PC): press it, press **Start** and play for a minute,
  taking a hit or two. Next time you play, press **Connect**, then **Find it again**;
  answer *"Is this your health?"* by taking a hit and watching the number.

Finally press **Save...**, then **Send to PS5**, to keep the Obolites cheat and any locked
health cheat for CheatRunner.

### 7.3 What Returnal teaches for other games

- If a cheat holds but the game puts the number back, you found a **screen copy**. Search
  again as an 8-byte whole number, or use **Test the hits**.
- Health, stamina and bars are usually **decimals**. Pick **Decimal (4 bytes)** or use
  **A bar or meter**.
- If **Works after restart?** says **This session only**, use **Lock it** for a PC-off
  cheat or **Find it next time** to have the app track it.
- Simple games are usually much easier. Many values show **Works after restart? Yes** on
  the first try, with nothing else to do.

---

## 8. When something goes wrong

| Message | What to do |
|---|---|
| Can't reach the PS5. Load PS5Debug-NG on the PS5, then try again. | Check the IP and that PS5Debug-NG is loaded. PC and PS5 must be on the same network. |
| Your PS5Debug-NG is too old. | Load version 1.3.2 or newer. |
| No game running. | Start the game, get into gameplay, press **Refresh**. |
| Lost connection to the PS5. | Check the PS5 is on, press **Connect**. |
| Nothing found. | Check the number, then press **Try it** to search another way (section 5.4). |
| The value keeps going back after Set or Freeze. | You found a copy. See section 5.5 and the Obolites example. |
| Couldn't send the file to the PS5. | Load the ftpsrv payload, or copy the saved file yourself. |
| The PS5 answered in a way we didn't expect. | Restart PS5Debug-NG and try again. |
| Something went wrong. | Press **Copy log** and send it with your bug report. |

---

## 9. Words you'll see

- **Value**: a number the game keeps in memory, like your gold or health.
- **Match / hit**: a place in memory that holds the number you searched for.
- **Copy**: a second place that just shows the number on screen. Changing it does nothing
  lasting.
- **Freeze**: keep writing the same value so it can't change.
- **This session only**: the value is somewhere different each time the game starts.
- **Lock it**: switch off the bit of game code that changes the value.
- **CheatRunner**: the program on the PS5 that runs saved cheat files without the PC.

---

## Good to know

Some of the newer features (Lock it, Player only, Find it next time and Test the hits)
are still being checked on a real PS5. If one of them doesn't behave as described, press
**Copy log** and report it.
