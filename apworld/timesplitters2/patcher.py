"""Patch a TimeSplitters 2 (GTSE4F) disc image for Archipelago.

The client can write memory while the game runs, but some things the randomizer
needs are hardcoded in the executable and cannot be reached that way -- the first
story mission is unconditionally selectable, for one. Those need the code changed,
and changing it in the image rather than in RAM means it stays changed across boots
and needs no cooperation from the client.

Patches are byte-for-byte in place: nothing moves, nothing grows, so the disc layout
is untouched and both plain .iso/.gcm and Dolphin's compact .ciso work. The original
file is never modified -- a patched copy is written instead.

The client runs this for you. By hand, from the world folder:

    python patcher.py "TimeSplitters 2 (USA).ciso" -o ts2-ap.ciso
    python patcher.py <image> --verify          report which patches are present
"""
import argparse
import os
import shutil
import struct
import sys

GAME_ID = b"GTSE4F"
DOL_HEADER_OFFSET = 0x420          # disc header field holding the DOL's ISO offset


class Image:
    """Random access to a disc image, whether plain or CISO-compressed.

    CISO stores only the blocks that contain data, so a logical disc offset has to
    be mapped through its block table. Patching in place is still fine: every byte
    we touch lives in a block that necessarily exists, because it holds code.
    """

    def __init__(self, path, writable=False):
        self.path = path
        self.file = open(path, "r+b" if writable else "rb")
        header = self.file.read(8)
        self.ciso = header[:4] == b"CISO"
        if not self.ciso:
            self.file.seek(0)
            return
        self.block_size = struct.unpack("<I", header[4:8])[0]
        flags = self.file.read(0x8000 - 8)
        self.physical = {}
        present = 0
        for index, flag in enumerate(flags):
            if flag:
                self.physical[index] = present
                present += 1

    def _place(self, offset):
        """Physical file offset for a logical disc offset, or None if not stored."""
        if not self.ciso:
            return offset
        block, within = divmod(offset, self.block_size)
        if block not in self.physical:
            return None
        return 0x8000 + self.physical[block] * self.block_size + within

    def read(self, offset, length):
        out = bytearray()
        while length:
            place = self._place(offset)
            span = length if not self.ciso else min(
                length, self.block_size - (offset % self.block_size))
            if place is None:
                out += b"\0" * span
            else:
                self.file.seek(place)
                out += self.file.read(span)
            offset += span
            length -= span
        return bytes(out)

    def write(self, offset, payload):
        place = self._place(offset)
        if place is None:
            raise ValueError("disc offset 0x%x is not stored in this image" % offset)
        if self.ciso and (offset % self.block_size) + len(payload) > self.block_size:
            raise ValueError("write at 0x%x spans a block boundary" % offset)
        self.file.seek(place)
        self.file.write(payload)

    def close(self):
        self.file.close()


# --- patches -----------------------------------------------------------------
# Each entry is (RAM address, expected original words, patched words, description).
# The expected words are checked before anything is written: refusing to patch a
# build that does not match is the whole reason they are here.

def li(register, value):
    """addi rD, 0, value -- the canonical 'load immediate'."""
    return 0x38000000 | (register << 21) | (value & 0xFFFF)


NOP = 0x60000000

# The story gate FUN_8013EDF4 answers "is mission N selectable?" and short-circuits
# mission 0 to always-yes:
#     8013ee30  cmpwi r29, 0        ; mission == 0 ?
#     8013ee34  bne   8013ee40
#     8013ee38  li    r3, 1         ; -> yes, unconditionally
#     8013ee3c  b     8013ee64
#     8013ee40  subi  r0, r29, 1    ; otherwise test story bit (mission - 1)
# Rewriting the two middle instructions makes mission 0 fall through as "mission 11",
# so it is gated by story bit 10 -- free, since missions 1..9 use bits 0..8.
STORY_FIRST_MISSION = (
    0x8013EE38,
    [li(3, 1), 0x48000028],                 # li r3,1 ; b 8013ee64
    [li(29, 11), NOP],
    "Siberia obeys the story bitfield (bit 10) instead of being always selectable",
)

# --- arcade / challenge: make the group-leading match lockable ----------------
# FUN_8013EBA4 walks a group's event list and answers "is this match selectable?".
# It returns yes the moment the walk reaches the match being asked about, so the
# match at position 0 is unconditionally selectable and no bitfield write can lock
# it. Across the 22 groups that is 88 locations reachable outside logic.
#
# Making the answer conditional needs more instructions than the two available at
# the site, so the site becomes a jump into a cave and the cave does the work.
#
# THE CAVE is FUN_8014699C, a 432-byte function that nothing in the image can reach:
# no branch targets it, its address never appears as data, no lis/addi pair builds
# it, and the instruction before it is blr so nothing falls through. Dead code left
# behind by the developers. The arcade cave takes the first 0x30 bytes, the control
# bits sit at +0x64, and the weapon cave below starts at +0x74 -- all inside it.
ARCADE_CAVE = 0x8014699C
ARCADE_CONTROL = 0x80146A00        # 4 words: bit E = "match E is granted"

ARCADE_CAVE_CODE = [
    0x3CA08014,   # lis    r5, 0x8014
    0x38A56A00,   # addi   r5, r5, 0x6a00      -> ARCADE_CONTROL
    0x5780EEFA,   # rlwinm r0, r28, 29, 27, 29 -> (event >> 5) * 4
    0x7C05002E,   # lwzx   r0, r5, r0          -> the control word
    0x578606FE,   # rlwinm r6, r28, 0, 27, 31  -> event & 31
    0x38E00001,   # li     r7, 1
    0x7CE73030,   # slw    r7, r7, r6
    0x7C003839,   # and.   r0, r0, r7
    0x38600000,   # li     r3, 0               -> locked
    0x41820008,   # beq    +8
    0x38600001,   # li     r3, 1               -> selectable
    0x4BFF8348,   # b      0x8013ED10          -> the function's own return path
]
# r28 holds the match being asked about; r0/r3/r5/r6/r7 are volatile and the cave
# returns immediately, so none of the caller's live state is disturbed.

ARCADE_CAVE_ORIGINAL = [
    0x9421FFD0, 0x7C0802A6, 0x2803000B, 0x90010034, 0x4181018C, 0x3CA08032,
    0x5460103A, 0x3865CFD4, 0x7C03002E, 0x7C0903A6, 0x4E800420, 0x806D8F80,
]
ARCADE_CONTROL_ORIGINAL = [0xD8010010, 0x80010014, 0x6C008000, 0x9001001C]

# --- weapon shuffle: rebuild the gun tables as each level starts loading -------
# A level precaches its guns from whatever the gun tables say when FUN_801252C0
# runs: for each slot in the map's weapon list it loads the model entry's files and,
# through FUN_80125484, both Gun-Stats records' sounds, graphics and projectiles.
# So the shuffle has to be in the tables before that call and cannot be applied by
# the client: the loader hangs if the tables change under it, and there is no
# earlier moment at which the level is known. FUN_801252C0 has exactly one caller,
# at 0x800C0208. That call is rerouted through the cave, which rebuilds the tables
# from a block the client writes into low memory (see memmap.py, WPN_BLOCK: magic,
# a pristine copy of both tables, then per-level rows of slot -> slot) and jumps on
# to the real precache. With no block the cave is eight instructions of nothing.
#
# (FUN_800DAB20, the general level precache that runs later, was the first hook
# site tried. It only re-loads the models, so the swapped guns came up untextured
# and silent. FUN_801251CC is the "precache every gun" routine the PC client
# retargets to; it is not used here -- a story level on a 24 MB console is the
# wrong place to load forty guns, and nothing grants a gun the level does not list.)
#
# The tables are always rebuilt from the pristine copy first, so a level without
# a row -- and every Arcade and Challenge map, which the cave detects by game mode
# (Story is 4; the modes share level ids) -- gets the native weapons back.
WEAPON_PRECACHE_CALL = 0x800C0208
WEAPON_CAVE = 0x80146A10

WEAPON_CAVE_CODE = [
    0x3C608000,   # lis     r3, 0x8000
    0x60631800,   # ori     r3, r3, 0x1800         -> the client's block
    0x80030000,   # lwz     r0, 0(r3)
    0x3C805453,   # lis     r4, 0x5453
    0x60843241,   # ori     r4, r4, 0x3241         'TS2A'
    0x7C002000,   # cmpw    r0, r4
    0x408200F4,   # bne     done                   no block: nothing to do
    0x38830010,   # addi    r4, r3, 0x10           r4 = pristine tables
    0x3CA08031,   # lis     r5, 0x8031
    0x60A51E70,   # ori     r5, r5, 0x1e70         r5 = live gun table
    0x3800046E,   # li      r0, 0x46e              0x11b8 bytes: 42 gun + 42 model entries
    0x7C0903A6,   # mtctr   r0
    0x38C4FFFC,   # subi    r6, r4, 4
    0x38E5FFFC,   # subi    r7, r5, 4
    0x84060004,   # restore: lwzu r0, 4(r6)
    0x94070004,   # stwu    r0, 4(r7)
    0x4200FFF8,   # bdnz    restore
    0x3CC0803D,   # lis     r6, 0x803d
    0x80060AE4,   # lwz     r0, 0xae4(r6)          game mode
    0x2C000004,   # cmpwi   r0, 4                  Story?
    0x408200BC,   # bne     done
    0x80060AF0,   # lwz     r0, 0xaf0(r6)          this load's flags: story-map Challenges run in mode 4 too
    0x740A0800,   # andis.  r10, r0, 0x0800        Challenge
    0x408200B0,   # bne     done
    0x700A0040,   # andi.   r10, r0, 0x40          Arcade
    0x408200A8,   # bne     done
    0x80ED8CCC,   # lwz     r7, -0x7334(r13)       current level id (0x80468B4C)
    0x390311C8,   # addi    r8, r3, 0x11c8         first row
    0x38000010,   # li      r0, 16
    0x7C0903A6,   # mtctr   r0
    0x80080000,   # rows:   lwz r0, 0(r8)
    0x7C003800,   # cmpw    r0, r7
    0x41820010,   # beq     found
    0x39080030,   # addi    r8, r8, 0x30
    0x4200FFF0,   # bdnz    rows
    0x48000080,   # b       done                   no row for this level
    0x39080004,   # found:  addi r8, r8, 4         r8[slot] = target, 0 = leave alone
    0x39200001,   # li      r9, 1                  slot 0 is "unarmed", never remapped
    0x7C0848AE,   # slot:   lbzx r0, r8, r9
    0x2C000000,   # cmpwi   r0, 0
    0x41820060,   # beq     next
    0x28000029,   # cmplwi  r0, 41
    0x41810058,   # bgt     next
    0x7C004800,   # cmpw    r0, r9
    0x41820050,   # beq     next
    0x1D690024,   # mulli   r11, r9, 0x24          live gun entry for the slot
    0x7D6B2A14,   # add     r11, r11, r5
    0x1D800024,   # mulli   r12, r0, 0x24          pristine gun entry for the target
    0x7D8C2214,   # add     r12, r12, r4
    0x814C0008,   # lwz     r10, 8(r12)            left-hand Gun-Stats index
    0x914B0008,   # stw     r10, 8(r11)
    0x814C000C,   # lwz     r10, 0xc(r12)          right-hand Gun-Stats index
    0x914B000C,   # stw     r10, 0xc(r11)
    0x1D690048,   # mulli   r11, r9, 0x48          the whole model entry, 18 words
    0x7D6B2A14,   # add     r11, r11, r5
    0x396B05E4,   # addi    r11, r11, 0x5e4        model table is gun table + 0x5e8; -4 for stwu
    0x1D800048,   # mulli   r12, r0, 0x48
    0x7D8C2214,   # add     r12, r12, r4
    0x398C05E4,   # addi    r12, r12, 0x5e4
    0x39400012,   # li      r10, 18
    0x7D4903A6,   # mtctr   r10
    0x840C0004,   # model:  lwzu r0, 4(r12)
    0x940B0004,   # stwu    r0, 4(r11)
    0x4200FFF8,   # bdnz    model
    0x39290001,   # next:   addi r9, r9, 1
    0x2C09002A,   # cmpwi   r9, 42
    0x4180FF90,   # blt     slot
    0x4BFDE7A4,   # done:   b 0x801252C0           the gun precache proper; LR is still the caller's
]
# Only r0 and r3-r12 are used, all volatile across the call being replaced, and the
# cave never calls anything, so LR carries the caller's return address straight
# through to the precache. Every word was checked by disassembling it with Ghidra
# and by executing the cave against a model of memory.

# --- weapon shuffle, part two: let Story find the other levels' guns -----------
# Each story level's pak carries only that level's guns (Chicago's has six). Every
# other mode also mounts gun.pak, which holds all 93 gun files, plus sounds.pak,
# chr.pak and demos.pak -- FUN_801BFBA8 decides, twice, with the same test:
#     mode == 4 (Story) && level != 100 && (flags & 0x40) == 0  -> level pak only
# A shuffled-in gun from another level then names a file no mounted pak has, and
# the loader waits for it forever. Each test ends in `beq` to the story-only path;
# turning both into nop makes Story mount what Arcade mounts. The game itself does
# exactly that when flag 0x40 is set, so it is a configuration the game supports.
PAK_MOUNT_TESTS = [0x801BFD64, 0x801BFDE4]     # beq 0x...70 in each copy of the test

WEAPON_CAVE_ORIGINAL = [
    0xC8010018, 0xEC000828, 0xEC030032, 0xEC001024, 0xFC00001E, 0xD8010020,
    0x80610024, 0x48000110, 0x1C041590, 0x806D9358, 0x7C630214, 0x806300BC,
    0x480000FC, 0x1C041590, 0x806D9358, 0x7C630214, 0x806300BC, 0x480000E8,
    0x1C041590, 0x806D9358, 0x7C630214, 0x480A6491, 0x480000D4, 0x1C041590,
    0x806D9358, 0x7C630214, 0x480A62A5, 0x480000C0, 0x1C041590, 0x806D9358,
    0x7C630214, 0x480A6289, 0x480000AC, 0x1C041590, 0x806D9358, 0xC022AD88,
    0x7C630214, 0x806313E0, 0xC003019C, 0xEC010032, 0xFC00001E, 0xD8010020,
    0x80610024, 0x48000080, 0x1C041590, 0x806D9358, 0x7C630214, 0x80630020,
    0x480A5EE9, 0x48000068, 0x5483043E, 0x5484843E, 0x4BFF9AFD, 0xC002AD88,
    0xEC000072, 0xFC00001E, 0xD8010020, 0x80610024, 0x48000044, 0x7C8C2378,
    0x7D8903A6, 0x4E800421, 0x48000034, 0x800D8F90, 0x20000004, 0x7C000034,
    0x5403D97E, 0x48000020,
]
# The first version of the cave checked the game mode alone, and so shuffled story-map
# Challenges, which also run in mode 4. An image patched with it is updated in place.
WEAPON_CAVE_CODE_V1 = [
    0x3C608000, 0x60631800, 0x80030000, 0x3C805453, 0x60843241, 0x7C002000,
    0x408200E0, 0x38830010, 0x3CA08031, 0x60A51E70, 0x3800046E, 0x7C0903A6,
    0x38C4FFFC, 0x38E5FFFC, 0x84060004, 0x94070004, 0x4200FFF8, 0x3CC0803D,
    0x80060AE4, 0x2C000004, 0x408200A8, 0x80ED8CCC, 0x390311C8, 0x38000010,
    0x7C0903A6, 0x80080000, 0x7C003800, 0x41820010, 0x39080030, 0x4200FFF0,
    0x48000080, 0x39080004, 0x39200001, 0x7C0848AE, 0x2C000000, 0x41820060,
    0x28000029, 0x41810058, 0x7C004800, 0x41820050, 0x1D690024, 0x7D6B2A14,
    0x1D800024, 0x7D8C2214, 0x814C0008, 0x914B0008, 0x814C000C, 0x914B000C,
    0x1D690048, 0x7D6B2A14, 0x396B05E4, 0x1D800048, 0x7D8C2214, 0x398C05E4,
    0x39400012, 0x7D4903A6, 0x840C0004, 0x940B0004, 0x4200FFF8, 0x39290001,
    0x2C09002A, 0x4180FF90, 0x4BFDE7B8,
]

# --- skipping a mission's intro cutscene ---------------------------------------
# Starting a story mission (FUN_80187338, case 4) asks FUN_801508FC -- a stub that
# always answers 1 -- whether to play the level's intro cutscene. On 1 it marks the
# cutscene seen in the gallery and sets the load's play-a-cutscene flag (0x20000000);
# on 0 the mission loads straight away. That one call is pointed at a 4-word cave
# answering 1 - the byte at 0x80002FF8, which the client sets from the seed's
# skip_intro_cutscenes option (RAM is zero at boot, so without the client nothing
# changes). The client marks a skipped intro seen itself, so the gallery still fills.
# The cave sits in FUN_8010AE1C, the dead function that also holds the mouse cave,
# past the end of that cave's code.
SKIP_INTRO_CAVE = (
    0x8010AF00,
    [0x90810008, 0x38630004, 0xC842A468, 0x6C638000],
    [0x3C608000, 0x88632FF8, 0x68630001, 0x4E800020],   # lis r3,0x8000 ; lbz r3,0x2ff8(r3) ; xori r3,r3,1 ; blr
    "intro skip: cave that answers from the client's skip flag",
)
SKIP_INTRO_HOOK = (
    0x80187728,
    [0x4BFC91D5],                            # bl FUN_801508fc
    [0x4BF837D9],                            # bl <cave>
    "intro skip: starting a story mission asks the cave whether to play the intro",
)

# --- weapon shuffle, part three: enemies use the gun they hold, not its slot ---
# How an enemy fires (range class, burst length) is looked up by the gun's Gun-Stats
# index, which the shuffle carries along. A few enemy checks instead compare the
# pawn's requested slot (+0xC8) with fixed slot numbers, so a gun shuffled into
# another slot was handled as the gun it replaced: a Flamethrower in the S47 slot was
# fired from rifle range, a shotgun in the Sniper Rifle slot from sniper range. Each
# check now loads the right-hand Gun-Stats index (+0x440) and compares it with the
# stats index of the same gun. In every site the base is the pawn and the register is
# only compared; each site was run, stock and patched, for every gun and takes the
# same branch for the gun's stats as the stock code did for its slot.
#   slot -> stats: 10->5 11->6 13->8 14->9 15/16->10 20->14 21->15 22->16 24..26->19..21
ENEMY_GUN_SITES = [
    (0x801CF294, [0x800300C8, 0x2C000014, 0x41820014, 0x2C000016, 0x4182000C, 0x2C000015],
     [0x80030440, 0x2C00000E, 0x41820014, 0x2C000010, 0x4182000C, 0x2C00000F],
     "no secondary fire in Story for the Homing Launcher, Lasergun and Plasma Autorifle"),
    (0x801D9418, [0x801A00C8, 0x2C00000D], [0x801A0440, 0x2C000008],
     "the Flamethrower fires only within flame reach"),
    (0x801D98A0,
     [0x807A00C8, 0x2C03000D, 0x4082001C, 0xC002BFF8, 0xFC1F0040, 0x40810010, 0x38000004, 0x901A12D8,
      0x48000328, 0x809A12D8, 0x2C040003, 0x41820050, 0x3803FFE8, 0x28000001, 0x4081000C, 0x2C03001A],
     [0x807A0440, 0x2C030008, 0x4082001C, 0xC002BFF8, 0xFC1F0040, 0x40810010, 0x38000004, 0x901A12D8,
      0x48000328, 0x809A12D8, 0x2C040003, 0x41820050, 0x3803FFED, 0x28000001, 0x4081000C, 0x2C030015],
     "Flamethrower and mine attack states"),
    (0x801D9B3C, [0x801A00C8, 0x2C00000D], [0x801A0440, 0x2C000008],
     "Flamethrower attack state"),
    (0x801E5058, [0x801E00C8, 0x2C00000D, 0x41820018, 0x2C00000E],
     [0x801E0440, 0x2C000008, 0x41820018, 0x2C000009],
     "close engage distances for the Flamethrower and Fire Extinguisher"),
    (0x801E621C, [0x801E00C8, 0x2C00000D], [0x801E0440, 0x2C000008],
     "Flamethrower chase distance (1)"),
    (0x801E6304, [0x801E00C8, 0x2C00000D], [0x801E0440, 0x2C000008],
     "Flamethrower chase distance (2)"),
    (0x801E63E4, [0x801E00C8, 0x2C00000D], [0x801E0440, 0x2C000008],
     "Flamethrower chase distance (3)"),
    (0x801C82C8, [0x801F00C8, 0x2C00000B], [0x801F0440, 0x2C000006],
     "only the Sniper Rifle skips the maximum engage range"),
    (0x80120040,
     [0x801D00C8, 0x2C00000F, 0x4182000C, 0x2C000010, 0x40820020, 0xC002A91C, 0xFC040040, 0x40810014,
      0xEC240028, 0xC002A830, 0xEC010024, 0x4800002C, 0x2C000016],
     [0x801D0440, 0x2C00000A, 0x4182000C, 0x2C00000A, 0x40820020, 0xC002A91C, 0xFC040040, 0x40810014,
      0xEC240028, 0xC002A830, 0xEC010024, 0x4800002C, 0x2C000010],
     "aim error for the S47 and Plasma Autorifle"),
    (0x801148F0, [0x800400C8, 0x2C00000A], [0x80040440, 0x2C000005],
     "the Minigun's second fire mode never runs dry"),
]
ENEMY_GUN_PATCHES = [(ram, original, patched, "enemies: " + what)
                     for ram, original, patched, what in ENEMY_GUN_SITES]

PATCHES = [
    STORY_FIRST_MISSION,
    (0x8013EC84, [0x38600001], [0x48007D18],
     "arcade/challenge selectability is asked of the control bits, not assumed"),
    (ARCADE_CAVE, ARCADE_CAVE_ORIGINAL, ARCADE_CAVE_CODE,
     "cave: answer that question from the control bits"),
    (ARCADE_CONTROL, ARCADE_CONTROL_ORIGINAL, [0, 0, 0, 0],
     "control bits start clear, so nothing is selectable until the client says so"),
    (WEAPON_PRECACHE_CALL, [0x480650B9], [0x48086809],
     "the level's gun precache runs through the weapon cave"),
    (WEAPON_CAVE, WEAPON_CAVE_ORIGINAL, WEAPON_CAVE_CODE,
     "cave: rebuild the gun tables from the client's shuffle block",
     [WEAPON_CAVE_CODE_V1 + WEAPON_CAVE_ORIGINAL[len(WEAPON_CAVE_CODE_V1):]]),
    (PAK_MOUNT_TESTS[0], [0x4182000C], [NOP],
     "Story mounts gun.pak and sounds.pak like Arcade does (pak names)"),
    (PAK_MOUNT_TESTS[1], [0x4182000C], [NOP],
     "Story mounts gun.pak and sounds.pak like Arcade does (the mounts)"),
    SKIP_INTRO_CAVE,
    SKIP_INTRO_HOOK,
] + ENEMY_GUN_PATCHES


# --- optional: mouse look (--mouse) ---------------------------------------------
# Not part of the randomizer. Lets an external mouse tool feed the game's C-stick,
# so mouse look works in every mode (first person and the sentry/camera modes the
# angle-writing injectors cannot reach).
#
# FUN_800f7fe8 converts one pad port into the game's input record (r29, port index
# in r31) each frame; the C-stick ints are written by
#     800f81d0  bl FUN_800f82c8            (r6 = &record+0x34, r7 = &record+0x3c)
# That call is rerouted into a cave which runs the original, then adds the deltas
# from a block at 0x80002F00 ('MOUS', port, dx, dy) to the C-stick ints, clamps
# them 0..255 and zeroes the deltas (the magic is left alone, so a writer knows the
# last delta was taken when dx and dy both read zero). Every control mode reads its
# look axes from those ints, so the mouse works everywhere, in the frame, with no
# write race. Without the block the cave does nothing.
#
# The first version of this cave read its block at 0x80001800, which is the start
# of the randomizer's weapon block (WPN_BLOCK in memmap.py). The two magic words
# overwrote each other, so each feature kept switching the other off. An image
# patched with that version is recognised and updated in place.
#
# THE CAVE is FUN_8010AE1C, a 592-byte function nothing in the image can reach (no
# branch, no data word, no lis/addi builds its address; blr precedes it). It is a
# different dead function from the randomizer's weapon/arcade cave, so the two
# never collide. Every word was disassembled back with Ghidra and executed against
# a model of the record and block.
MOUSE_HOOK = (
    0x800F81D0,
    [0x480000F9],                            # bl FUN_800f82c8
    [0x48012C4D],                            # bl <cave>
    "mouse: the C-stick conversion runs through the cave",
)
MOUSE_CAVE = 0x8010AE1C
MOUSE_CAVE_ORIGINAL = [
    0x9421FFD0, 0x7C0802A6, 0x3C60803D, 0x90010034, 0x38630A98, 0x93E1002C,
    0x8003004C, 0x2C000008, 0x4182001C, 0x40800020, 0x2C000003, 0x40800018,
    0x2C000001, 0x40800008, 0x4800000C, 0x38000001, 0x4800005C, 0x3C60803D,
    0x38630A98, 0x80630050, 0x3803FFFB, 0x2800000C, 0x41810040, 0x3C608031,
    0x5400103A, 0x38639778, 0x7C03002E, 0x7C0903A6, 0x4E800420, 0x4BF59BD9,
    0x2C030002, 0x4081000C, 0x38000000, 0x48000018, 0x38000001, 0x48000010,
    0x38000001, 0x48000008, 0x38000000, 0x28000000, 0x41820130,
]
MOUSE_CAVE_CODE = [
    0x7C0802A6, 0x9421FFF0, 0x90010014, 0x4BFED4A1, 0x3CA08000, 0x38A52F00,
    0x80050000, 0x3CC04D4F, 0x60C65553, 0x7C003000, 0x4082006C, 0x80050004,
    0x7C00F800, 0x40820060, 0x807D0034, 0x80850008, 0x7C632214, 0x2C030000,
    0x40800008, 0x38600000, 0x2C0300FF, 0x40810008, 0x386000FF, 0x907D0034,
    0x807D003C, 0x8085000C, 0x7C632214, 0x2C030000, 0x40800008, 0x38600000,
    0x2C0300FF, 0x40810008, 0x386000FF, 0x907D003C, 0x38000000, 0x90050008,
    0x9005000C, 0x80010014, 0x38210010, 0x7C0803A6, 0x4E800020,
]
MOUSE_CAVE_CODE_V1 = [   # block at 0x80001800 -- collided with WPN_BLOCK
    0x7C0802A6, 0x9421FFF0, 0x90010014, 0x4BFED4A1, 0x3CA08000, 0x38A51800,
    0x80050000, 0x3CC04D4F, 0x60C65553, 0x7C003000, 0x4082006C, 0x80050004,
    0x7C00F800, 0x40820060, 0x807D0034, 0x80850008, 0x7C632214, 0x2C030000,
    0x40800008, 0x38600000, 0x2C0300FF, 0x40810008, 0x386000FF, 0x907D0034,
    0x807D003C, 0x8085000C, 0x7C632214, 0x2C030000, 0x40800008, 0x38600000,
    0x2C0300FF, 0x40810008, 0x386000FF, 0x907D003C, 0x38000000, 0x90050008,
    0x9005000C, 0x80010014, 0x38210010, 0x7C0803A6, 0x4E800020,
]
MOUSE_PATCHES = [
    (MOUSE_CAVE, MOUSE_CAVE_ORIGINAL, MOUSE_CAVE_CODE, "mouse: cave that adds the block's deltas to the C-stick",
     [MOUSE_CAVE_CODE_V1]),
    MOUSE_HOOK,
]


def dol_offset(image):
    header = image.read(0, 0x440)
    if header[:6] != GAME_ID:
        raise SystemExit("this is not %s (found %r)" % (GAME_ID.decode(), header[:6]))
    return struct.unpack(">I", header[DOL_HEADER_OFFSET:DOL_HEADER_OFFSET + 4])[0]


def ram_to_disc(image, dol_start, ram):
    """Translate a RAM address into a disc offset via the DOL's section table."""
    head = image.read(dol_start, 0x100)
    offsets = struct.unpack(">18I", head[0x00:0x48])
    addresses = struct.unpack(">18I", head[0x48:0x90])
    sizes = struct.unpack(">18I", head[0x90:0xD8])
    for file_offset, address, size in zip(offsets, addresses, sizes):
        if size and address <= ram < address + size:
            return dol_start + file_offset + (ram - address)
    raise SystemExit("0x%08x is not inside any DOL section" % ram)


def words_at(image, disc, count):
    raw = image.read(disc, count * 4)
    return list(struct.unpack(">%dI" % count, raw))


def apply(path, out_path, verify_only=False, patches=None, title="patches", header=True, in_place=False):
    """Apply (or with verify_only, report) a list of patches.

    Each patch is (ram, original words, patched words, description), optionally with
    a fifth element: earlier patched versions of the same site, which are replaced.

    Patches a copy of `path` into `out_path` -- or, with in_place, `path` itself (only
    for an image this patcher already wrote, e.g. upgrading "(AP)" after an update).
    """
    patches = PATCHES if patches is None else patches
    if verify_only or in_place:
        target = path
    else:
        if os.path.abspath(path) == os.path.abspath(out_path):
            raise SystemExit("refusing to write over the original image")
        print("copying %s -> %s" % (os.path.basename(path), os.path.basename(out_path)))
        shutil.copyfile(path, out_path)
        target = out_path

    image = Image(target, writable=not verify_only)
    try:
        dol_start = dol_offset(image)
        if header:
            print("%s, DOL at disc offset 0x%x%s"
                  % (GAME_ID.decode(), dol_start, " (CISO)" if image.ciso else ""))
        applied = already = 0
        for ram, original, patched, description, *rest in patches:
            previous = rest[0] if rest else []
            disc = ram_to_disc(image, dol_start, ram)
            current = words_at(image, disc, len(original))
            if current == patched:
                print("  already patched  %08x  %s" % (ram, description))
                already += 1
                continue
            if current in previous:
                if verify_only:
                    print("  OLD version      %08x  %s (re-run the patcher to update)" % (ram, description))
                    continue
                image.write(disc, struct.pack(">%dI" % len(patched), *patched))
                print("  updated          %08x  %s" % (ram, description))
                applied += 1
                continue
            if current != original:
                raise SystemExit(
                    "unexpected code at %08x: found %s, expected %s.\n"
                    "This image is not the build these patches were written for."
                    % (ram, " ".join("%08x" % w for w in current),
                       " ".join("%08x" % w for w in original)))
            if verify_only:
                print("  NOT patched      %08x  %s" % (ram, description))
                continue
            image.write(disc, struct.pack(">%dI" % len(patched), *patched))
            print("  patched          %08x  %s" % (ram, description))
            applied += 1
        if verify_only:
            print("%s: %d of %d present" % (title, already, len(patches)))
        else:
            print("done: %d applied, %d already present" % (applied, already))
    finally:
        image.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="TimeSplitters 2 disc image (.iso, .gcm or .ciso)")
    parser.add_argument("-o", "--out", help="where to write the patched copy")
    parser.add_argument("--verify", action="store_true",
                        help="report which patches are present, changing nothing")
    parser.add_argument("--mouse", action="store_true",
                        help="also apply the optional mouse-look hook (see MOUSE_PATCHES)")
    parser.add_argument("--mouse-only", action="store_true",
                        help="apply ONLY the mouse-look hook: the stock game with mouse look, "
                             "no randomizer changes")
    args = parser.parse_args()
    if args.mouse_only:
        patches = MOUSE_PATCHES
    else:
        patches = PATCHES + (MOUSE_PATCHES if args.mouse else [])
    if args.verify:
        # reported separately: the mouse hook is optional, so its absence is not a fault
        apply(args.image, None, verify_only=True, patches=PATCHES, title="randomizer")
        print()
        apply(args.image, None, verify_only=True, patches=MOUSE_PATCHES,
              title="optional mouse hook (--mouse)", header=False)
        return
    out = args.out
    if not out:
        stem, ext = os.path.splitext(args.image)
        out = stem + (" (mouse)" if args.mouse_only else " (AP)") + ext
    apply(args.image, out, patches=patches)
    print()
    print("Run this patched image in Dolphin. The original is untouched.")


if __name__ == "__main__":
    main()
