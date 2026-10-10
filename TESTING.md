# Building and testing

## Building

    python apworld/build_apworld.py                 -> apworld/timesplitters2.apworld
    python apworld/build_apworld.py --install DIR   -> also copied into DIR (an Archipelago custom_worlds)

The `.apworld` is self-contained: the world, the client (`client/`), the disc patcher (`patcher.py`), the mouse
driver (`mouse_driver.py`) and the music shuffle (`music.py`, `dspadpcm.py`). The build verifies every member against its source and refuses a stray `__pycache__`.

## Running from a source checkout

With the `.apworld` installed into the checkout's `custom_worlds`:

    python Launcher.py "TimeSplitters 2 Client" -- --connect localhost:38281 --name Player1

Client options (after `--`): `--dolphin <Dolphin.exe>` and `--game <disc image>` (remembered in `host.yaml`),
`--no-launch` to attach to a Dolphin that is already running the patched game, `--read-only` to send checks but
write nothing. The installed Archipelago already ships `dolphin-memory-engine`; a source checkout needs
`pip install dolphin-memory-engine`.

## Patching the disc

The client writes a patched copy beside the original, `<name> (AP).<ext>`, and refreshes it when a newer build
adds or changes patches. By hand:

    python apworld/timesplitters2/patcher.py "<original image>" --mouse
    python apworld/timesplitters2/patcher.py "<image>" --verify

The randomizer patches: the story gate (the first mission can be locked like the rest), the arcade/challenge
gate (the first match of each group can be locked), the gun precache (a story level rebuilds its gun tables from
the seed's shuffle as it starts loading, and Arcade and Challenge loads get the native tables back), the pak
mounts (Story can load any gun), the intro skip (starting a story mission asks a flag the client sets from
the seed whether to play the intro; zero, as at boot, plays it), and the enemy weapon checks (the few enemy
checks that named a gun by its slot, such as the Flamethrower's reach and the Sniper Rifle's range, name it by
its Gun-Stats instead, so they follow a shuffled gun). The optional mouse hook is added to every image
the client writes; it does nothing without the mouse driver.

The patches use low RAM at `0x80001800`..`0x80003000` (weapon block, mouse block, intro-skip flag). Dolphin puts
its Gecko cheat handler there when a game has active cheat codes: keep them off.

## Behaviour worth knowing

**If either the game or the client crashes, restart both**: close Dolphin and start the client again, and it
launches the game fresh. The client says so when it loses the game or the Dolphin it started closes.

**Story, Arcade and Challenge.** The Challenges set on story maps run in Story's game mode, so the client and the
gun precache tell Story apart by the load's flags as well (a Story start sets neither the Arcade nor the Challenge
flag). Weapon gating, the weapon shuffle, traps and the ammo scaling apply in story missions only; DeathLink and
the bonus packs apply everywhere. Story sends every death; Arcade and Challenge send one per
`arcade_death_link_threshold` deaths. A death received waits until the player is on foot, and plays out through
the game's own death sequence.

**Weapon shuffle.** The client writes the seed's shuffle once, with a pristine copy of the gun tables, into the
weapon block; the patched disc applies it as each story level loads. Restarting only the client is safe: a later
client takes the pristine tables from the block, not from the live ones. A level's weapon pickups take their
amounts from the level's own data, sized for the gun the level was built around, so the client scales the
player's ammo gains to the shuffled-in gun: a gain of g rounds is worth g * max(new) / max(old), fractions
carried. Losses, Ammo Packs and a restarted client are never scaled.

**One profile per seed.** The client follows the profile that is loaded, and only the one that belongs to the
seed: a tag in the last word of the Platinum medal bitfield (bits no event uses), a hash of the seed name, team
and slot, saved with the profile. A profile with no tag and no progress (no trophy, no story mission played, no
story locks) is claimed on its own; the claim is confirmed on the next poll, and dropped if progress appeared
under it (a card profile loading into the slot). Any other profile is ignored until `/use_profile`. The name of
the seed's profile is kept in data storage, key `ts2gc_profile_<team>_<slot>`. Locks are applied every poll; the
menus pick them up when a screen is built, so an unlock received while a list is on screen shows after backing out
and back in.

**Beside the PC world.** This world is the package `timesplitters2` with the game `TimeSplitters 2`. The PC world
(TS2Redux-AP 1.1.0) is the package `ts2redux` with the game `TimeSplitters 2 Redux`, and the two install side by
side. The PC's 1.0.x apworld had this file name and game name, so installing this one replaces it.
