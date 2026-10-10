# TimeSplitters 2 (GameCube) — Archipelago world

The `.apworld` for the TimeSplitters 2 (GameCube) Archipelago randomizer. It defines the items, locations and
rules, and carries everything needed to play: the client (`client/`, started from the Launcher as
**TimeSplitters 2 Client**), the disc patcher (`patcher.py`, applied by the client), the mouse driver
(`mouse_driver.py`, started by the client) and the music shuffle (`music.py` with its DSP ADPCM codec
`dspadpcm.py`, applied by the client). `game` name: **`TimeSplitters 2`**; package `timesplitters2`.

## Locations (362)

| Source | Count | Detail |
| --- | --- | --- |
| Arcade & Challenge trophies | 264 | 66 events x 4 tiers (Bronze / Silver / Gold / Platinum) |
| Story difficulties | 30 | 10 missions x 3 difficulties (Easy / Normal / Hard) |
| Objectives | 68 | per-mission objective completions |

The 66 events are 45 Arcade League matches + 21 Challenges, each toggled separately by `game_modes`. A
trophy/story check fires when you reach that tier/difficulty in-game; trophies are cumulative, so a single Gold
fires Bronze + Silver + Gold. Which checks are *active* is option-driven, but every location id is pre-allocated
and stable, so changing options never renumbers existing seeds.

## Items

- **Unit unlocks (76)** — one per Arcade match / Challenge / story mission; receiving it unlocks the unit in the
  menu and enables its checks.
- **Time Crystals (progression)** — gate the final mission.
- **Weapon items (36)** — used when **Weapon Gating** is on; each gates one weapon (the akimbo "(x2)"
  dual-wields are their own pickups, so they are their own items).
- **Health / Armor / Ammo Packs** — one-time refills, placed in place of filler.
- **Traps (8)** — each switches on a TS2 cheat for 30 seconds in a story mission.
- **Filler** — Banana.

## Goal

Follows **`game_modes`**:

- **Story kept** — clear the final story mission (**Space Station**) on your chosen **Max Story Difficulty**.
  Space Station stays locked until `time_crystals_required` **Time Crystals** have arrived from the multiworld.
- **Story removed** — complete **`trophy_goal_percentage`** of the trophy checks you kept. Counted over what the
  seed actually has, so a Challenge-only seed needs that share of its Challenge checks alone. No Time Crystals
  are placed, and the weapon options are forced off.

## Options

See `options.py` for the full text of each. Weapon Gating and Weapon Shuffle apply to story missions only.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `game_modes` | any of Story / Arcade / Challenge | all three | which modes produce checks; at least one (see **Goal**) |
| `trophy_goal_percentage` | 10–100 | 90 | share of the trophy checks needed to win; only when Story is removed |
| `story_difficulty` | easy / normal / hard | normal | the MAX difficulty; checks up to it, and the goal is cleared on it |
| `trophy_tier` | bronze / silver / gold / platinum | gold | the MAX trophy tier; checks up to it |
| `time_crystals_required` | 0–60 | 8 | Time Crystals needed to unlock Space Station (auto-clamped to free slots) |
| `time_crystals_extra` | 0–30 | 4 | extra Time Crystals placed beyond the requirement |
| `starting_unlocks_per_category` | 1–3 | 1 | random starting unlocks per mode, so the seed is enterable from the start |
| `weapon_gating` | toggle | off | you can only hold weapons you've received as items |
| `weapon_shuffle` | toggle | off | swaps what each weapon is (pickups, your loadout, and enemy weapons) |
| `weapon_shuffle_scope` | completely_random / same_class / within_level | completely_random | how far a swap may reach |
| `skip_intro_cutscenes` | toggle | on | story missions start without their intro cutscene; it still unlocks in the gallery |
| `bonus_health_packs` | 0–30 | 3 | one-time full-health refills in the pool |
| `bonus_armor_packs` | 0–30 | 3 | one-time full-armor refills |
| `bonus_ammo_packs` | 0–30 | 3 | one-time refills of every carried weapon's ammo |
| `trap_count` | 0–60 | 8 | traps placed; each turns on a cheat for 30 s in a story mission, one at a time |
| `death_link` | toggle | off | share deaths with other DeathLink players |
| `arcade_death_link_threshold` | 0–50 | 10 | Arcade/Challenge deaths before one is sent out; 0 = never send from there. Story sends every death |
| `inverse_look` | toggle | off | profile preference, written once |
| `auto_lookahead` | toggle | off | profile preference, written once |
| `weapon_change` | always / never / best / if_new / if_new_and_best | never | profile preference, written once; the game's default fights Weapon Gating |

Plus the standard `start_inventory_from_pool`.

**Profile preferences** are written to your profile the first time the client sees it, and never again for that
profile: change them in the game whenever you like.

## Building

    python ../build_apworld.py            -> timesplitters2.apworld
    python ../build_apworld.py --install  -> also copied into the local Archipelago custom_worlds/
