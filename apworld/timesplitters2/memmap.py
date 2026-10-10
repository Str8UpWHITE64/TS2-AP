"""GameCube memory map for TimeSplitters 2 — GTSE4F (NTSC-U, revision 0).

Everything here is *port* knowledge, deliberately kept out of data.py so that the
content catalog stays platform-neutral and can be compared byte-for-byte against
the PC world's copy.

Offsets named `*_OFF` are relative to the start of a profile record unless the
name says otherwise. Absolute addresses are GameCube RAM addresses and are read
big-endian.

Confirmation status is marked per entry:
  [static]  derived from main.dol
  [live]    observed in a running game
"""

GAME_ID = "GTSE4F"
REVISION = 0

# --- profile array ---------------------------------------------------------
# The array is heap-allocated; this is a pointer to it, in .bss.        [static]
PROFILE_BASE_PTR = 0x80468DD0
PROFILE_STRIDE = 0x1F3C
PROFILE_MAX = 16
PROFILE_NAME_OFF = 0x00
PROFILE_NAME_LEN = 32

# Preferences, in the profile header (found by flipping each on the Preferences
# screen and diffing RAM). The game works from a live copy of the active profile
# at ACTIVE_PROFILE_COPY, with the same layout, and writes both when a preference is
# changed on that screen -- so the client writes both too.
PROFILE_PREF_FLAGS = 0x18                 # bit set = on
PREF_INVERSE_LOOK = 1 << 0
PREF_AUTO_LOOKAHEAD = 1 << 3
PROFILE_WEAPON_CHANGE = 0xCC              # 0 always, 1 never, 2 best, 3 if new, 4 if new and best
ACTIVE_PROFILE_COPY = 0x803E6350
ACTIVE_PROFILE_ADDR = 0x803D0784      # int per player: index into the array

# --- game-data sub-struct --------------------------------------------------
# Size is exact: the cross-profile aggregate at 0x803CE188 is memset to it.
GAMEDATA_OFF = 0xD18
GAMEDATA_SIZE = 0x11AC

# --- trophy / medal records ------------------------------------------------
# 66 records. The array ends at exactly the offset where the play counters
# begin, which independently pins the count.                            [static]
TROPHY_OFF = GAMEDATA_OFF + 0x49C     # = profile + 0x11B4
TROPHY_STRIDE = 0x14
TROPHY_COUNT = 66
TROPHY_PLAYS = 0x00
TROPHY_SCORE = 0x04
TROPHY_SCORE2 = 0x08
TROPHY_TIER = 0x0C                    # 0 none, 1 Bronze .. 4 Platinum
TROPHY_DONE = 0x10
# Tiers are cumulative: a Gold clear also sets Bronze and Silver.

# --- story completion ------------------------------------------------------
# One dword per difficulty; bit m = story mission m in data.py's STORY order.
# Sets only on a real clear, and is cumulative across difficulties.
# Siberia/Easy -> bit 0 of the Easy word.                                 [live]
STORY_CLEAR_OFF = {
    "Easy":   GAMEDATA_OFF + 0xBC8,   # = profile + 0x18E0
    "Normal": GAMEDATA_OFF + 0xBCC,   # = profile + 0x18E4
    "Hard":   GAMEDATA_OFF + 0xBD0,   # = profile + 0x18E8
}

# Same shape, arcade side. The end-of-match recorder writes
# `G + 0xBC8 + diff*4 + (0 or 0xC)`, so one loop feeds both arrays.     [static]
ARCADE_LEAGUE_OFF = GAMEDATA_OFF + 0xBD4    # 3 dwords, Easy/Normal/Hard

# Medal bitfields, 4 tiers x 0x10 bytes (128 event bits each).          [static]
MEDAL_BITS_OFF = GAMEDATA_OFF + 0xBE0
MEDAL_BITS_TIER_STRIDE = 0x10

# The seed's claim on a profile: the last word of the Platinum bitfield, event bits
# 96-127, which no event has (there are 66). Saved with the profile like the rest of
# the bitfield. The client never writes its gate into this word.
PROFILE_TAG_OFF = MEDAL_BITS_OFF + 3 * MEDAL_BITS_TIER_STRIDE + 3 * 4    # = G + 0xC1C

# Cinematics gallery: bit n = cutscene n seen (FUN_8013E5EC sets, FUN_8013E64C tests).
CINEMATICS_OFF = GAMEDATA_OFF + 0xBC4
# Each story level's intro cutscene, as FUN_800C0688 numbers it (table 0x802C0C30;
# Space Station is special-cased to 11). 9 and 12 play after Aztec and Space Station.
INTRO_CUTSCENE_OF_LEVEL = {0x23: 1, 0x14: 2, 0x22: 3, 0x28: 4, 0x26: 5, 0x20: 6, 0x1F: 7,
                           0x30: 8, 0x29: 10, 0x27: 11}

# 1 = skip the story missions' intro cutscenes (patcher SKIP_INTRO_*), from the seed.
# Low RAM, past the mouse block; zero at boot.
SKIP_INTRO_FLAG = 0x80002FF8
SKIP_INTRO_HOOK_ADDR = 0x80187728
SKIP_INTRO_HOOK_APPLIED = 0x4BF837D9   # bl <cave at 0x8010AF00>
TAIL_UNLOCK_OFF = GAMEDATA_OFF + 0x1198     # 5 dwords, ends at GAMEDATA_SIZE

# --- do NOT use for checks -------------------------------------------------
# profile + 0xFA4 + level_id*8 is the per-level entry counter. It bumps when a
# mission is ENTERED, so a quit-out looks like a clear. The PC build has the
# same trap at +0xFC0; it cost real time there. Use STORY_CLEAR_OFF instead.
STORY_ENTRY_COUNTER_BASE = GAMEDATA_OFF + 0x28C   # = profile + 0xFA4
STORY_ENTRY_COUNTER_STRIDE = 8

# --- debug ------------------------------------------------------------------
# Naming a profile this trips the game's unlock-everything routine, which fills
# every field above at once. Useful as a test fixture, useless for deriving the
# trophy record ORDER (it sets all 66 simultaneously).
CHEAT_PROFILE_NAME = "DUFFINS"

# --- cheats (trap items) ----------------------------------------------------
# Bit N is cheat N, numbered as data.TRAP_CHEATS (the PC's catalog order; Fat
# Characters is bit 6 here too). A cheat is in effect only while all three agree,
# and every check site re-reads them per frame:
#   CHEATS_ENABLED  what the Cheats menu switched on (FUN_8026b600 sets a bit)
#   CHEATS_ALLOWED  what this mode permits, rebuilt at every level load from the
#                   cheat table's flags (FUN_8026b450); Story allows all eight traps
#   unlocked        FUN_8026b684: each cheat's medal requirement, or the unlock-all
#                   override byte for category 4 (FUN_8013e420 -> FUN_8026bb08(4))
# The client's own medal writes leave most cheats locked, so a trap raises the
# override for its duration as well.
CHEATS_ENABLED = 0x80469754
CHEATS_ALLOWED = 0x80469750
CHEAT_UNLOCK_OVERRIDE = 0x80468454        # UNLOCK_ALL_OVERRIDE[4]

# --- player state (DeathLink, health / armor packs) -----------------------------
# Player 1's control struct, the same base the mouse driver aims through. Its
# object holds health; the object's +0x124 points back at the pawn above.
PLAYER_CTRL_PTR = 0x804686CC
CTRL_OBJECT = 0x1BC
CTRL_STATE = 0x158                # movement state; jump table at 0x802BE6D4
STATE_FALLING = 2                 # FUN_80050338: on landing, health <= 0 runs the death sequence
STATE_DEAD = 4                    # set by the death handler, FUN_80054a18
OBJ_PAWN = 0x124
OBJ_HEALTH = 0x19C                # float
OBJ_HEALTH_MAX = 0x1A0            # float
PAWN_ARMOR = 0x10                 # float; the death event (FUN_8001ec30) zeroes it with health
PAWN_ARMOR_MAX = 0x14             # float

# --- live objectives --------------------------------------------------------
# The current level's objective table is a (pointer, count) pair at params +0x9C /
# +0xA0, cached into these two globals when the level loads. The table itself lives
# in writable memory, so the game keeps each objective's RUNTIME STATE inside the
# struct: FUN_8014CD1C zeroes +0x1C and +0x20 for every entry at level start.
#
# +0x1C is the status the pause menu shows. It reaches 5 when the objective is
# complete -- the same value the PC client watches for.                    [static]
OBJ_TABLE_PTR = 0x80468DB8        # -> current level's objective table
OBJ_COUNT_ADDR = 0x80468DBC       # entries in that table
CURRENT_LEVEL_ADDR = 0x80468B4C   # level id currently loaded

OBJ_STRIDE = 0x24
OBJ_TEXTID = 0x00
OBJ_REQUIRED_BY_DIFFICULTY = 0x10  # three ints: 0 primary, 1 secondary, -1 absent
OBJ_STATUS = 0x1C
OBJ_STATUS_COMPLETE = 5

# Objective entries the GameCube tables carry that data.py deliberately omits: they
# sit mid-run in otherwise contiguous id blocks, the signature of entries that are
# not real objectives, and PC playtesting dropped the same ones. Recorded because a
# validator comparing table size against data.py's curated list would otherwise
# reject three missions outright.
GC_EXTRA_OBJECTIVE_TEXTIDS = {
    "Chicago": frozenset({1076, 1077}),
    "Notre Dame": frozenset({1089}),
    "Wild West": frozenset({1114}),
}

# --- patched-image behaviour ------------------------------------------------
# The stock game hardcodes the first story mission as always selectable. The disc
# patch (patcher/patch_iso.py) makes it fall through as "mission 11" instead, so it
# is gated by this otherwise-unused bit of the story bitfield. Missions 1..9 use
# bits 0..8, so bit 10 is free.
#
# The client sets it for a granted first mission only when the patch is present;
# on a stock image the bit is ignored and that mission is simply always open.
FIRST_MISSION_STORY_BIT = 10

# The two instructions the patch replaces, so the client can tell a patched image
# from a stock one at runtime and adjust rather than silently mis-gating.
STORY_PATCH_ADDR = 0x8013EE38
STORY_PATCH_STOCK = (0x38600001, 0x48000028)      # li r3,1 ; b
STORY_PATCH_APPLIED = (0x3BA0000B, 0x60000000)    # li r29,11 ; nop

# The arcade/challenge patch redirects the "is this match selectable?" answer at
# 0x8013EC84 into a cave that reads these control bits: bit E = match E (a GAME
# record index) is granted. Without the patch the group-leading match of every group
# is always selectable and these bits are ignored.
ARCADE_CONTROL_ADDR = 0x80146A00
ARCADE_CONTROL_WORDS = 4                          # 128 bits, 66 used

ARCADE_PATCH_ADDR = 0x8013EC84
ARCADE_PATCH_STOCK = 0x38600001                   # li r3,1  -- always selectable
ARCADE_PATCH_APPLIED = 0x48007D18                 # b <cave>

# The weapon patch reroutes the level's gun precache (single call site) through a
# cave that rebuilds the gun tables from the block described under "the
# weapon-shuffle block" below. Without it the block is inert and no shuffle can
# be applied.
WEAPON_PATCH_ADDR = 0x800C0208
WEAPON_PATCH_STOCK = 0x480650B9                   # bl <gun precache>
WEAPON_PATCH_APPLIED = 0x48086809                 # bl <cave>
# Its companion makes Story mount gun.pak and sounds.pak, so a gun from another
# level can be found at all; without it a cross-level target hangs the load.
PAK_MOUNT_PATCH_ADDRS = (0x801BFD64, 0x801BFDE4)
PAK_MOUNT_PATCH_STOCK = 0x4182000C                # beq <story-only mounts>
PAK_MOUNT_PATCH_APPLIED = 0x60000000              # nop
# Pickups of a shuffled gun sized for it (patcher AMMO_*): the item spawner's amount
# multiply calls the cave.
AMMO_PATCH_ADDR = 0x800D9110
AMMO_PATCH_STOCK = 0x7EC4B9D6                     # mullw r22, r4, r23
AMMO_PATCH_APPLIED = 0x48031E01                   # bl <cave>

# --- weapons ----------------------------------------------------------------
# The gun table: one entry per weapon identity. Entry N is the weapon the apworld
# calls slot N -- verified by the akimbo pattern, which agrees with data.py's
# WEAPON_AKIMBO at all 38 slots (dual-wield entries hold a gun in both hands,
# single-wield ones read -1 in the left).
#
# +0x08 and +0x0C are the Gun-Stats indices for the left and right hand, and they
# are what decides how a weapon actually behaves. Copying slot Y's pair over slot
# X's makes X fire as Y, which is the whole of the weapon shuffle.
GUN_TABLE = 0x80311E70
GUN_STRIDE = 0x24
GUN_COUNT = 42                    # entry 42 is where the model table begins
GUN_LEFT_STATS = 0x08
GUN_RIGHT_STATS = 0x0C
GUN_NONE = 0xFFFFFFFF             # this hand holds nothing

# The weapon-info / model table, parallel to the gun table: string id, drop and
# inventory graphics, animation, scales. The PC client copies the WHOLE entry when
# it shuffles, which is what makes a remapped weapon look like its target rather
# than merely fire like it. Same 0x48 stride as the PC build.
GUN_MODEL_TABLE = 0x80312458
GUN_MODEL_STRIDE = 0x48
GUN_TABLES_LEN = 0x11B8           # both tables, contiguous: 42 * 0x24 + 42 * 0x48

# --- the weapon-shuffle block ---------------------------------------------
# The shuffle cannot be applied from outside the game at the right moment: a level
# precaches the assets for whatever the tables say when it starts loading, and
# rewriting them under the loader hangs it. So a disc patch (patcher/patch_iso.py)
# reroutes the precache through a cave that rebuilds the tables itself, and the
# client's only job is to describe the shuffle in this block, once.
#
# The block lives in low memory below the OS globals at 0x80003000, a region the
# game never uses. The cave does nothing at all until the magic word is present,
# so the client writes that word LAST.
#
#   +0x0000  magic 'TS2A'
#   +0x0010  pristine copy of both tables (GUN_TABLES_LEN bytes, as in the game)
#   +0x11C8  WPN_ROW_COUNT rows of WPN_ROW_STRIDE: u32 level id, then one byte per
#            slot = the slot it should become (0 = leave alone). Level id 0 = unused.
#
# On every level load the cave restores the pristine copy, and then -- in Story
# only, for a level that has a row -- copies the target's Gun-Stats pair and whole
# model entry over each remapped slot. Arcade and Challenge always get native guns.
# The cave's Story test reads the load flags here; an image with the first version of
# the cave (mode only) lacks this word and shuffles story-map Challenges.
WEAPON_CAVE_FLAGS_ADDR = 0x80146A64
WEAPON_CAVE_FLAGS_APPLIED = 0x80060AF0      # lwz r0, 0xaf0(r6)
WPN_BLOCK = 0x80001800
WPN_BLOCK_MAGIC = b"TS2A"
WPN_BLOCK_PRISTINE = 0x10
WPN_BLOCK_ROWS = 0x11C8
WPN_ROW_COUNT = 16
WPN_ROW_STRIDE = 0x30
WPN_BLOCK_LEN = WPN_BLOCK_ROWS + WPN_ROW_COUNT * WPN_ROW_STRIDE     # 0x14C8

# Game mode, read by the precache to pick the Story or Arcade parameter set for a
# level (the two modes share level ids). 4 = Story.
GAME_MODE_ADDR = 0x803D0AE4
GAME_MODE_STORY = 4
# The load's flags, set with the mode by the front end's launcher (FUN_80187338): a story
# start sets neither of these, an Arcade match 0x40 and a Challenge 0x8000000. Mode alone
# is not enough -- the story-map Challenges run in mode 4 too. The game makes the same
# test: cheats are disallowed on 0x8800040, a starting gun is handed out on 0x40/0x8000000.
LOAD_FLAGS_ADDR = 0x803D0AF0
LOAD_FLAGS_NOT_STORY = 0x08000040

# Gun-Stats records, indexed by the gun table's +0x08 / +0x0C. Recorded for the
# ammo fields, which the Ammo Pack refill reads. A shuffled-in gun fires its own
# ammo type up to its own max; the patched disc sizes its pickups (patcher AMMO_*).
STATS_TABLE = 0x8030B6F0
STATS_STRIDE = 0x240
STATS_AMMO_TYPE = 0x10
STATS_ALT_AMMO_TYPE = 0x18        # the Plasma Autorifle's grenades, for one
STATS_AMMO_ON_PICKUP = 0x20

# Ammo types: +0x00 is the maximum a player can carry of that type.
AMMO_TYPE_TABLE = 0x80309900
AMMO_TYPE_STRIDE = 0xD0
AMMO_TYPE_COUNT = 0x1C
AMMO_TYPE_MAX = 0x00

# --- the player pawn --------------------------------------------------------
# Char pointer list; entry 0 is player 1, holding the pawn address directly.
CHAR_PTR_LIST = 0x804013C8
PAWN_CURRENT_SLOT = 0x0C8         # weapon slot in hand
PAWN_CURRENT_STATS = 0x440        # that weapon's Gun-Stats index

# Per-slot "the player owns this weapon" flags, one dword each. Found by diffing a
# pawn before and after a pickup, then confirmed three ways: picking up the Sniper
# Rifle flips exactly slot 11; the only flags set beforehand are slot 0 (fists) and
# slot 36 (Temporal Uplink), which is precisely the loadout every mission starts
# with; and every entry reads 0 or 1. The PC build keeps the same array at +0x1000.
# 42 entries, one per gun table slot: the game saves and restores exactly 0xA8 bytes
# here, and the ammo array starts right after it.
PAWN_WEAPON_OWNED = 0x994
PAWN_WEAPON_OWNED_COUNT = 42
# Reserve ammo, one dword per ammo type (AMMO_TYPE_COUNT of them). Rounds in the
# magazines are kept apart, per hand: +0x444 (right) / +0x104 (left) + type * 4.
PAWN_AMMO = 0xA3C

# A real weapon switch sets all three of these together. +0x100/+0x440 are the pawn's
# copies of the gun table's per-hand Gun-Stats indices (they read exactly gun[slot]
# +0x08 / +0x0C after a genuine switch). Writing only the slot leaves the pawn with
# one weapon's identity and another's stats: no ammo, and the game drops the weapon on
# the next switch when it notices.
PAWN_LEFT_STATS = 0x100
PAWN_RIGHT_STATS = 0x440

# The original pawn diff showed TWO slot fields change identically on a pickup
# (36 -> 11 at both +0xC8 and +0xCC). One alone did not move the rendered weapon.
PAWN_CURRENT_SLOT_2 = 0x0CC
