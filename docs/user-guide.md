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
7. **Make it stick**, in one of three ways:
   - **Make it work...**: the easiest. Pick what the cheat should do, press Start and
     play; the app finds the game code, tries safe fixes one by one, and keeps the one
     that works. The result runs in CheatRunner with the PC off.
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

- **A number**: you can read it, like `2,450` gold. Use a number search (5.2). If it's a
  percent, like `40%`, type it with the % (5.3).
- **A bar or meter**: like a health bar with no number. Fill it up and press **It's full
  now** (5.4).
- **Nothing**: a hidden value. Use "I don't know the number" (5.5).

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

### 5.3 When the game shows a percent

Type the percent with a `%` sign, for example `40%`, and press **Search**. One search
looks for it every way games keep a percent: 40, 0.4 and 400, as decimals and (when they
are whole numbers) as whole numbers. A pause or stats screen that shows the exact
percentage is perfect for this.

Change it in the game, then type the new percent (`25%`) and **Search again**. Each match
stays with the way it was found, so 0.4 is compared as a fraction next time. In this
search a number typed without the % counts as a percent too. Scan files show how each
match is stored in their **Stored as** column.

### 5.4 When it's a bar (or not out of 100)

For a bar, or a value whose most isn't 100 (health 250, 1,700 shield):

1. Fill it up in the game, then press **It's full now**. The app takes a snapshot and
   remembers it as "full".
2. Make it go down (take a hit).
3. If the game shows the percent anywhere, type it (`40%`) and press **Search**. If you
   only see the bar, press **About 75%**, **About 50%** or **About 25%**, whichever it
   looks like.
4. Repeat 2 and 3 a couple of times.

The app keeps values that are now that percent of what they were when full (40% keeps 39
to 41% of full; About 50% keeps roughly 37 to 63%). While very many matches are left, the
hint says *"Too many to check by percent yet"*: that scan counts as **Went down** (or
**Went up**); take another hit or heal, then type the % again.

Sometimes a percent can only be noted, and nothing is dropped: when it's the same as (or
very close to) the last one, when the last one was 0%, or when you pressed **Went up** /
**Went down** yourself since the last percent. Change it in the game again and type the new %. In this search a number
typed without the % also counts as a percent, so it must be 0 to 100; to search for a
plain number such as 180, press **Start over**.

**Went down by** / **Went up by** in this search: the amount is applied to each match as
it is stored. For a match kept as the percent, a fraction or x10, type percentage points
(40% to 30% is **Went down by** 10). For a match kept as a share of its full value ("of
full" under **Stored as**), or not tied to a way yet (while very many are left), the
amount is the raw number the game uses, which you usually can't see (health 250 to 200 is
50); typing the % again is easier.

Pressing **It's full now** again during a search marks full again: fill it up and don't
touch the controller, because it runs a **Same** scan.

### 5.5 When you can't see the number

1. Press **I don't know the number**. The app takes a snapshot of the game.
2. Make the value change in the game (take a hit, use some stamina).
3. Press **Went up**, **Went down**, **Changed** or **Same** to say what happened.
4. Repeat. To clear out values that change on their own (timers, animations), press
   **Stand still** and leave the controller alone while it runs.

If you know roughly where the value sits (say, between 1 and 1000), put that in
**Advanced > Don't know the number, but it's between ... and ...** to start with fewer
matches.

The search looks for decimals (4 bytes) and whole numbers (4 bytes) at the same time.
The match count says how many of each are left; once only one kind is left, the search
carries on with that kind alone. To search a single kind (faster, or 8-byte values),
pick it in **Advanced > Number type** first. **Test the hits** needs a single kind.

### 5.6 When the search finds nothing

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

### 5.7 When several matches all move together

Many games keep a **copy** of a value just for the screen. The copy changes along with the
real one, so searching can't tell them apart.

From 20,000 matches down, the app notes every match's value after each scan, so the changes you make while narrowing down already count. Matches that
always change together (one is always the other times the same number, like health 40 and
its fraction 0.4) are shown as **one row** with **+N copies**; press the arrow to see every
address. A row marked **likely real** has something that changed with it right next to it
in memory at another scale (health next to its fraction), which display copies don't. It's
a hint: nothing is removed. Set, Freeze and Make a cheat on a group's row use its likely
real address; on an expanded row, that address. Values that never changed come last.
Groups show from 500 matches down (and when they fit **Show up to**). They need the value
to have **changed twice** since those 20,000: a **Same** scan doesn't count, because nothing
changed. If the list shows up before that, the hint says how many more changes it needs:
change the value in the game and search again (the new number, or Went up / Went down).
Grouping works for every search, not
just percent ones: a plain number or Went up / Went down search shows the grouped list too.
**Test the hits** then tests one address per group. If the matches keep the number
different ways (say a fraction and a whole number), it tests only those stored the most
common way, so one test value means the same to all of them.

Three ways to find the real one:

- **Check which is real** (easiest, no questions): keep playing (don't pause) and press
  it. The app writes a test value to each listed match for about a second, one per group
  at a time, and watches what the game does. A copy gets put back by the game; the real
  value keeps the test value and its copies change with it. The row the others followed
  is marked **checked: real** and moves to the top of its group (so Set, Freeze and Make
  a cheat use it); others show **checked: copy** or **checked: kept the test value**.
  Every value the game didn't put back itself gets its own value back. A group of 5
  takes about 8 seconds. If every row comes out **checked: copy**, the real value isn't
  in the list at all: the hint then suggests **Find the real one behind it** (section
  5.10).
- **Set test**: pick a row, type a new value, press **Set**. If the number on screen
  jumps back the next time it changes, that row was a copy.
- **Test the hits** (easier): the app writes a test value to half of the matches for a
  moment, then asks *"Did the number in the game change?"*. Answer **Yes, it changed**,
  **No** or **Not sure**. It halves the list each time, so 500 matches take about 9
  questions. Every value is put back after each question. When it's done, the real
  address is added to the list.

> Check which is real and Test the hits write to the game's memory. Save your game first.

### 5.8 Try it, then make a cheat

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

### 5.9 Other tools on the Find tab

- **Filter list by value**: show only rows near a number (`100` or `90-110`).
- **Add address**: type an address you got from somewhere else to watch it.
- **Pointer scan**: looks for a path from the game's fixed memory to this value, so you
  can reach it after a restart. Run it, restart the game, find the value again, then
  press **Re-check with this address** to keep only the paths that still work. This takes
  a few minutes. Most people won't need it: **Find it next time** (section 6.4) does the
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

### 5.10 Find the real one behind it

Sometimes everything you can find is a copy: the screen shows 87, but the game keeps
health as 0.87 or as 870, or as a number of a different size, and your search only found
the HUD's copies. **Find the real one behind it** follows the copy back to the value the
game really uses.

It is still being tested on a real PS5, so for now it's under **Advanced**, labelled
**Find the real one behind it (being tested)**. The hint suggests it (and the button
turns bold) when **Check which is real** marks every row a copy, or when a value you
Set or Freeze is put back by the game within 2 seconds.

1. Pick **one row** in the list (a copy is fine: that's the point) and press the button.
   The first time, it asks you to **save your game first**: it writes test values and
   briefly pauses the game.
2. Press **Start** and keep playing. When it says so, make the number change (take a
   hit, spend some) within 30 seconds. The app watches which bit of the game's code
   writes the copy, reads that code to see where the number comes from, and checks the
   place it found.
3. It may ask you to **change the number once more** so it can check what it found, and
   then puts a test value in for about a second to see that the game really uses it.
   Every value it changes is put back.
4. If the place it found is itself a copy, it follows that one too (up to 4 steps, at
   most about 4 minutes). **Cancel** stops it at any time and leaves nothing changed.

When it finds the real value it says how the game keeps it, for example *"Found the
value the game uses: 0.87, a decimal; the screen shows it x100."* and adds it to the
list, marked **checked: real** (or **not sure** if it couldn't test it).

**On a found row, you type what the screen shows.** Value now shows both, like
`0.87 (screen: 87)`; Stored as shows how the game keeps it; Notes say which copy it was
found behind. Set, Freeze and Make a cheat take the number as you see it on screen, so
typing 100 sets the game's 1.0. A cheat saved from it holds the game's own number.

**Search for it that way**: the result also offers to start a new search with the right
number type and "Game stores it as" already filled in, and today's on-screen number typed
in. Press **Search**, then narrow it down as usual. Useful next time the game starts,
when the found address is somewhere else.

Other answers you may see:

| Message | What it means |
|---|---|
| This is already the value the game uses. | The row you picked is the real one. Use it. |
| Nothing wrote to it. Make the number change while it watches, then try again. | The number didn't change during the 30 s watch. |
| The game works this number out in a way the app can't follow yet. | Try Check which is real, or I don't know the number (section 5.5). |
| The game scrambles this value, which isn't supported yet. | The game hides this value on purpose. |
| Followed it N steps without reaching the real value. | The last place it checked is added to the list, marked not sure. |
| This address can't be watched. | Pick another row of the same group. |

The button waits while a search or a check is still running. A frozen row or a cheat on
the same value is paused during the trace and put back afterwards.
**Advanced: show details** in its window lists each step: what it watched, which code
wrote it, how it worked out the place, and why other places were dropped.

For the curious: one step uses an execute breakpoint as a last resort. It's off until
it's been checked on a console; to try it, set `"trace_exec_breakpoint": true` in
`%APPDATA%\CheatMaker\settings.json`.

---

## 6. My cheats (tab 2)

Each cheat has:

- **On**: tick to turn it on in the game right now, untick to turn it off.
- **Name** and **Value**: double-click to change them.
- **Works after restart?**
  - **Yes**: the value lives in the same place every time. You can save it as it is.
  - **This session only**: the value moves every time the game starts. It works now but
    can't be saved for CheatRunner yet.
  - **Found again each run**: the app has learned how to find it after a restart (6.4).
  - **Check after restart**: made by **Make it work** with **Only me**, using a detail
    of your character that may change after a restart. Restart the game once and check
    it still works (6.2).

**Combine**: some games need two values kept at once (Returnal's health needs two). Make
a cheat for each, Ctrl-click both in **My cheats**, and press **Combine**. You get one
cheat that turns both on with a single tick. A combined cheat's value can't be edited,
and Lock it works on one address at a time, so run Lock it before combining.

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

**Player only cheats made before November 11, 2026** keep their old game code, which could
confuse the game's own checks right after a hit (for example a game that tests "health
at zero?" right after the damage). Player only now leaves the game's checks alone. If an
old Player only cheat acts strangely, delete it and make it again (or use **Make it
work** with **Only me**).

### 6.2 Make it work

**Make it work...** does what Lock it and Player only do, by itself: it finds the game
code that changes the value, tries only fixes that are safe, tests each one while you
play, and keeps the first one that works. If a fix doesn't work, the game is put back
before the next one is tried.

You'll find the button on the **Find** tab (next to **Make a cheat**) and in **My cheats**
(next to **Lock it**). It works on one value at a time: a decimal, or a whole number of 4
or 8 bytes. For a combined cheat or one that is found again each run, the button is off
("Pick a single value.").

1. **Save your game first.** The first time each run, the app reminds you: a fix that
   doesn't work may make the game restart.
2. Pick what the cheat should do:
   - **Never goes down**: anything that would lower the value is ignored. Raising it
     still works (you can still pick up health or money).
   - **Always full**: the value always goes back to **Full**. Fill it up before you press
     Start: the value at Start is Full. To use another number, type it in the box.
   - **Half damage**: anything that lowers the value lowers it by half as much.
   - **Only me** (tick box): the cheat works for your character only. Enemies go through
     the game's own code, so they still take damage.
3. Press **Start**. If the row is in a group that hasn't been checked yet, the app first
   runs **Check which is real** on that group (section 5.7). If it can't tell, it uses
   your row and says "Not sure this is the real one."
4. **Make the value change** within 30 seconds (take a hit, spend money). With Only me,
   the app then asks you to **get hit and hit enemies** for 20 seconds.
5. The app tests each fix for 20 seconds: "Testing fix 1 of 3: keep playing and take a
   hit." Keep playing and take a hit. Sometimes it asks a question:
   - **Did you take a hit?**: for a fix it can't watch by itself. Press **Yes, I got
     hit** only if you did.
   - **Did enemies still take damage?**: with Only me, when it couldn't see an enemy get
     hurt. Press **Yes** only if they did.
6. When a fix works, the cheat goes into **My cheats**, switched on, named like "Health:
   Never goes down". From My cheats it replaces the row you started from.

Good to know:

- If you didn't get hit during a test, the app tries the same fix once more.
- If enemies stop dying, make it again with **Only me** ticked.
- **Cancel**, closing the window, or disconnecting puts the game back as it was.
- If the game stops responding during a test, the app puts the game back and tries the
  next fix. A fix that crashed the game is never tried again for that game version
  (leaving the game with the PS button doesn't count as a crash). If the PS5 didn't
  answer at all, the app stops and says "Restart the game: fixes only live in memory
  until then."
- If no cheat is made, the My cheats row you started from is switched back on.
- **Advanced: show details** lists the game code it caught, the ones it skipped and why,
  and every fix it tried.
- Values that need two places held at once (like Returnal's health and its fraction) are
  not done by Make it work yet: make it work on one and use Lock it or Combine for the
  other.

### 6.3 Find it again

**Find it again** looks for all your remembered values in the game now. Before changing
anything after a restart, the app asks once: *"Is this your health? It shows 100 right
now."* Take a hit and watch the number. Press **Yes** only if it changes with yours. If it
found more than one candidate, it asks you to pick the one that changes.

### 6.4 Find it next time

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

### 6.5 Save and send

Press **Save...**:

- **Format**: `.json` (recommended) or `.shn`. The file is named for the game and version,
  like `PPSA01285_03.000.005.json`, which is what CheatRunner looks for.
- **Save to PC** puts it in `Documents\CheatMaker\cheats` (or a folder you choose).
- **Send to PS5** uploads it to the PS5 if ftpsrv is running. If not, copy the saved file
  to `/data/cheatrunner/cheats/json/` on the PS5 yourself.

"This session only" cheats aren't saved; Lock them first. Cheats that are found again each
run are kept in the app's own notes for this game, and come back the next time you
connect.

### 6.6 After a game update

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

   Faster: Returnal's pause screen shows your health as an exact percentage. With full
   health press **It's full now**, take a hit, open the pause screen and type the percent
   it shows (`65%`). Take another hit and type the new percent. The list then shows health
   as one row marked **likely real** (health and its fraction sit side by side) with the
   screen copies folded under it.
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
| Nothing found. | Check the number, then press **Try it** to search another way (section 5.6). |
| The value keeps going back after Set or Freeze. | You found a copy. See section 5.7 and the Obolites example; if every match is a copy, use Find the real one behind it (section 5.10). |
| Couldn't send the file to the PS5. | Load the ftpsrv payload, or copy the saved file yourself. |
| The PS5 answered in a way we didn't expect. | Restart PS5Debug-NG and try again. |
| Something went wrong. | Press **Copy log** and send it with your bug report. |
| Nothing changed it. Try again and make it change while the bar moves. | Make it work didn't see the value change. Press Start again and take a hit (or spend) straight away. |
| None of the safe fixes worked for this value. Your game is unchanged. | Try another effect, tick Only me, or use Lock it. **Advanced: show details** shows what was skipped and why. |
| Couldn't tell you apart from enemies. | During Collect, get hit *and* hit several enemies, then try again. |
| This kind of value isn't supported yet. Use Lock it. | Make it work handles decimals and whole numbers of 4 or 8 bytes only. |
| Restart the game: fixes only live in memory until then. | The PS5 stopped answering during a test. Restarting the game removes the test fix. |
| Fix N crashed the game last time. Your game is unchanged after a restart. | Nothing to do: that fix won't be tried again. |

---

## 9. Words you'll see

- **Value**: a number the game keeps in memory, like your gold or health.
- **Match / hit**: a place in memory that holds the number you searched for.
- **Copy**: a second place that just shows the number on screen. Changing it does nothing
  lasting.
- **Likely real**: in a group of matches that change together, the one with a related value
  right next to it in memory. Usually the one to use.
- **Stored as**: how a match keeps a percent: the percent itself, a fraction (0 to 1), x10,
  or a share of its value when full ("of full").
- **Freeze**: keep writing the same value so it can't change.
- **This session only**: the value is somewhere different each time the game starts.
- **Lock it**: switch off the bit of game code that changes the value.
- **Make it work**: let the app find, test and keep a safe fix for a value by itself.
- **Find the real one behind it**: follow a screen copy back to the value the game uses.
- **Only me**: a cheat that changes your character's value but not the enemies'.
- **CheatRunner**: the program on the PS5 that runs saved cheat files without the PC.

---

## Good to know

Some of the newer features (Make it work, Find the real one behind it, Lock it, Player only, Find it next time, Test
the hits and the percent search with It's full now) are still being checked on a real PS5. If one of them doesn't behave as described, press
**Copy log** and report it.
