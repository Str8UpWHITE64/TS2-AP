# TimeSplitters 2 (GameCube) — Archipelago

An [Archipelago](https://archipelago.gg) randomizer for **TimeSplitters 2** (GameCube, USA), played in Dolphin.
Story missions, objectives, Arcade League matches and Challenges are locked behind items from the multiworld,
and completing them sends checks to everyone else.

Features:

- **Everything is a check**: mission clears on each difficulty, mission objectives, and Arcade League and
  Challenge trophies (Bronze to Platinum).
- **Weapon Gating and Weapon Shuffle**: weapons become items, and story levels can hand out a different gun in
  each weapon's place.
- **Skip Intro Cutscenes** (on by default): story missions start straight away. The cutscenes still unlock in
  the gallery.
- **DeathLink, traps and bonus packs**: share deaths with the multiworld, get hit with Big Head mode at the worst
  moment, or get a timely health, armor or ammo refill.
- **Mouse look** built in: no extra program to install or run.
- **One Launcher entry**: the client patches your disc, starts Dolphin and connects, all on its own.

> **Playing the PC version too?** The PC (TS2 Redux) world is now `TimeSplitters 2 Redux`, in
> `ts2redux.apworld` (TS2Redux-AP 1.1.0). Its 1.0.x apworld used this world's file name and game name, so
> installing this one replaces it: install `ts2redux.apworld` as well to keep playing the PC game.

## What you need

- [Archipelago](https://github.com/ArchipelagoMW/Archipelago/releases) 0.6.4 or newer.
- [Dolphin](https://dolphin-emu.org/) (a recent build).
- Your own **TimeSplitters 2 (USA)** disc image (game ID `GTSE4F`) as `.iso`, `.gcm` or `.ciso`. RVZ images
  cannot be patched: convert one to ISO in Dolphin first (right-click the game, *Convert File...*).
- Windows, for mouse look. Everything else works wherever Archipelago and Dolphin do.

## Installing

Download `timesplitters2.apworld` from the [releases](../../releases) and double-click it, or copy it into
Archipelago's `custom_worlds` folder.

## Playing

1. Make a yaml: in the Archipelago Launcher, **Generate Template Options** writes one with every option
   explained (`TimeSplitters 2.yaml`). Generate and host as usual.
2. Open the Archipelago Launcher and click **TimeSplitters 2 Client**.
3. The first time, it asks for `Dolphin.exe` and your disc image. It remembers both (in `host.yaml`, under
   `timesplitters2_options`).
4. The client writes a patched copy of your disc beside the original, `<name> (AP).<ext>`, and launches it in
   Dolphin. Your original is never changed, and the copy is refreshed automatically after an update.
5. Connect with `/connect <server>` and your slot name, then **create a new profile** for this seed in the
   game. The client claims it by itself and plays the seed on that profile only.

**One profile per seed.** The client claims only a profile with no progress, and ignores every other profile,
so a profile you played outside this seed never has its trophies and clears sent as checks. Load the seed's
profile whenever you come back to it; the client names it if you load the wrong one. To play a seed on a profile
that already has progress anyway (sending that progress as checks), load it and type `/use_profile`.

Unlocks show up the next time the game rebuilds a menu, so back out of a menu and into it again if something
you just received is not there yet.

**If the game or the client crashes, restart both**: close Dolphin and start the client again, and it launches
the game fresh. The client tells you when it loses the game.

**Do not enable cheat codes (Gecko or Action Replay) for this game.** Dolphin loads its cheat handler into the
same spot in memory the randomizer's patches use.

Client commands: `/gate` shows what is unlocked, `/weapons` what each weapon currently is, `/dolphin` the
emulator connection, `/deathlink` turns DeathLink on or off for the session, `/use_profile` plays the seed on
the profile loaded now, and `/readonly` stops (or resumes) writing to the game.

### Mouse look

On by default. Dolphin's port 1 must be a **Standard Controller**. Click into the Dolphin window and the mouse
aims, on foot and on mounted guns and remote cameras. **F8** turns capture on and off, **F6** / **F7** lower and
raise the sensitivity. Turn it off with `mouse_look: false` under `timesplitters2_options` in `host.yaml`.
The game's own Inverse Look setting is followed.

## Options

The template yaml from the Launcher lists every option with its choices.

- **Game modes**: any of Story, Arcade, Challenge. With Story, the goal is to clear the final mission
  (*Space Station*) on your Max Story Difficulty, which stays locked until you hold enough **Time Crystals**.
  Without it, the goal is a percentage of the trophy checks.
- **Weapon Gating**: weapons are items; a gun you have not received is taken off you.
- **Weapon Shuffle**: each weapon is replaced by another in the story, everywhere at once, by class, or only
  among a level's own guns. Mines only ever swap with mines. Ammo follows the new gun: a pickup gives the same
  share of the new gun's maximum it would have given the old one.
- **Skip Intro Cutscenes**: on by default; turn it off to watch each mission's intro.
- **Health, Armor and Ammo Packs**: one-time refills from the multiworld, applied as soon as you are in a level.
- **Traps**: each switches on a TimeSplitters 2 cheat (big heads, cloaked enemies, cardboard characters, ...) for
  30 seconds during a story mission, one at a time. Traps received elsewhere wait for your next mission.
- **DeathLink**: every story death is shared. Arcade and Challenge are deathmatches, so there one death goes out
  per `arcade_death_link_threshold` deaths. A death you receive waits until you are on foot (not on a turret or
  camera).
- **Profile preferences**: Inverse Look, Auto Lookahead and Weapon Change are written to your profile once;
  change them in the game afterwards as you like.

## Memory card

**The client overwrites your profile's story and trophy progress** with what Archipelago has unlocked, and the
game saves that to the memory card. On a new seed that means a profile starts out mostly locked. Use a fresh
profile, or back up your memory card first: `%APPDATA%\Dolphin Emulator\GC\USA\Card A`.

## For developers

See [`TESTING.md`](TESTING.md) for building from source and what to verify, and
[`apworld/timesplitters2/README.md`](apworld/timesplitters2/README.md) for the content model.
