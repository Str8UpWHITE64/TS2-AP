"""Reading TimeSplitters 2 (GameCube) progress -- no Archipelago dependency.

Split out so the dry run can watch a live game with nothing but Dolphin and this
repo, and so the index-space conversions live in exactly one place.
"""
import struct
import typing

import dolphin_memory_engine as dme

from .. import data
from .. import memmap as mm

POLL_INTERVAL = 0.4

# Globals the end-of-match recorder uses.
CURRENT_DIFFICULTY = 0x803D0AEC       # 0 Easy, 1 Normal, 2 Hard
MATCH_FLAGS = 0x803D0AF4              # bit 0x8000 = the run was WON
MATCH_WON = 0x8000
UNLOCK_ALL_OVERRIDE = 0x80468450      # [0] story [1] arcade [2] challenge -- must stay 0
GROUP_TABLE = 0x8031C8C8
GROUP_STRIDE = 0x38

STORY_BASE = data.BASE_ID + 0x1400
TROPHY_BASE = data.BASE_ID + 0x1000

# Two different index spaces, and conflating them is the single easiest way to make
# this client silently wrong:
#
#   AP index    position in data.TROPHY_EVENTS -- what location ids are built from
#   game index  position in the game's 66-record table -- what memory is addressed by
#
# They agree at none of the 66 positions: the apworld enumerates arcade first, the
# game enumerates challenges first. Always convert explicitly.
AP_EVENT_NAMES = [name for (_, _, name) in data.TROPHY_EVENTS]
AP_INDEX_OF_NAME = {name: i for i, name in enumerate(AP_EVENT_NAMES)}
GAME_INDEX_OF_NAME = data.TROPHY_EVENT_INDEX
AP_TO_GAME = [GAME_INDEX_OF_NAME[n] for n in AP_EVENT_NAMES]
GAME_TO_AP = [AP_INDEX_OF_NAME[n] for n in data.TROPHY_TABLE_ORDER]
assert sorted(AP_TO_GAME) == list(range(len(AP_EVENT_NAMES))), "event index mapping is not a bijection"

LOCATION_NAME = {loc_id: name for name, loc_id in data.location_name_to_id.items()}
DIFF_BIT = {"Easy": 1, "Normal": 2, "Hard": 4}


def u32(addr: int) -> int:
    return int.from_bytes(dme.read_bytes(addr, 4), "big")


def w32(addr: int, value: int) -> None:
    dme.write_bytes(addr, (value & 0xFFFFFFFF).to_bytes(4, "big"))


def build_objectives():
    """mission index -> [(location id, difficulty mask)] for objectives a clear implies.

    An objective that is PRIMARY on difficulty D is required to finish the mission on
    D, so clearing at D proves it was done. A few are instead earned simply by
    finishing; for those the secondary difficulties count too. Objectives that are
    only ever SECONDARY are genuinely optional and cannot be inferred from a clear.
    """
    by_mission: typing.Dict[int, list] = {}
    underivable = []
    for loc_name, (tid, prim, sec, mission) in data.OBJ_LOC_INFO.items():
        loc_id = data.location_name_to_id[loc_name]
        mission_index = data.STORY.index(mission)
        difficulties = prim
        if tid == data.ESCAPE_TEXTID or (mission, tid) in data.MISSION_COMPLETE_OBJS:
            difficulties = prim | sec
        if not difficulties:
            underivable.append(loc_name)
            continue
        mask = 0
        for d in difficulties:
            mask |= DIFF_BIT[d]
        by_mission.setdefault(mission_index, []).append((loc_id, mask))
    return by_mission, underivable


OBJECTIVES_BY_MISSION, UNDERIVABLE_OBJECTIVES = build_objectives()


def hook(quiet: bool = False) -> bool:
    """Attach to Dolphin and confirm the right game is running."""
    if not dme.is_hooked():
        dme.hook()
    if not dme.is_hooked():
        return False
    try:
        return dme.read_bytes(0x80000000, 6).decode("latin-1") == mm.GAME_ID
    except Exception:
        return False


def profile_base(slot: int = 0) -> typing.Optional[int]:
    base = u32(mm.PROFILE_BASE_PTR)
    if not 0x80000000 <= base < 0x81800000:
        return None
    addr = base + slot * mm.PROFILE_STRIDE
    try:
        name = dme.read_bytes(addr, mm.PROFILE_NAME_LEN).split(bytes(1), 1)[0].decode("latin-1")
    except Exception:
        return None
    return addr if name else None


def profile_name(slot: int = 0) -> str:
    base = u32(mm.PROFILE_BASE_PTR)
    addr = base + slot * mm.PROFILE_STRIDE
    return dme.read_bytes(addr, mm.PROFILE_NAME_LEN).split(bytes(1), 1)[0].decode("latin-1")


def objectives_for(story_cleared: typing.Dict[int, int]) -> typing.Set[int]:
    """Objective location ids implied by the mission clears recorded so far."""
    found = set()
    for mission_index, entries in OBJECTIVES_BY_MISSION.items():
        cleared = 0
        for di in range(3):
            if story_cleared[di] >> mission_index & 1:
                cleared |= 1 << di
        if not cleared:
            continue
        for loc_id, mask in entries:
            if mask & cleared:
                found.add(loc_id)
    return found


# --- live objective status --------------------------------------------------
# Objectives can be completed without finishing the mission, so they are polled
# rather than inferred from a clear. The game keeps each objective's status inside
# its own struct in the level's objective table; status 5 means complete.
MISSION_OF_LEVEL = {level: index for index, level in enumerate(data.STORY_LEVEL_ID)}
OBJ_LOCATION = {}                       # (mission index, textID) -> location id
for _name, (_tid, _prim, _sec, _mission) in data.OBJ_LOC_INFO.items():
    OBJ_LOCATION[(data.STORY.index(_mission), _tid)] = data.location_name_to_id[_name]


def game_mode():
    try:
        return u32(mm.GAME_MODE_ADDR)
    except Exception:
        return None


def in_story() -> bool:
    """A story mission is what is loaded, not an Arcade match or Challenge on a story map."""
    try:
        return game_mode() == mm.GAME_MODE_STORY and not u32(mm.LOAD_FLAGS_ADDR) & mm.LOAD_FLAGS_NOT_STORY
    except Exception:
        return False


def current_mission():
    """Story mission index for the level now loaded, or None if it is not one.

    Story and Arcade share level ids -- Arcade Chicago is level 0x14 just as Story
    Chicago is -- so the level id alone is not enough; the game mode settles it.
    """
    try:
        mission = MISSION_OF_LEVEL.get(u32(mm.CURRENT_LEVEL_ADDR))
    except Exception:
        return None
    if mission is None or not in_story():
        return None
    return mission


def live_objectives():
    """Location ids for objectives currently marked complete in the loaded level.

    Empty outside a story mission, and empty while the cached table still belongs to
    a level we have left -- see live_objective_table().
    """
    validated = live_objective_table()
    if validated is None:
        return set()
    mission, table, count = validated
    found = set()
    for i in range(count):
        entry = table + i * mm.OBJ_STRIDE
        if u32(entry + mm.OBJ_STATUS) != mm.OBJ_STATUS_COMPLETE:
            continue
        loc = OBJ_LOCATION.get((mission, u32(entry + mm.OBJ_TEXTID)))
        if loc is not None:
            found.add(loc)
    return found


# --- validating the live objective table ------------------------------------
# The level-id global updates BEFORE the objective system re-caches its table and
# zeroes the statuses, so for a moment the cached table still belongs to the level
# just left -- and those tables are static data whose statuses stay set until that
# level is loaded again. Reading one while believing we are elsewhere invents
# completions for any textID the two missions share (1062 and 1063 appear in every
# mission), which is exactly how a NeoTokyo entry once fired Planet X's leftovers.
#
# So the table is only trusted when it actually looks like this mission's: the entry
# count must match, and every textID in it must belong to the mission.
MISSION_TEXTIDS = {data.STORY.index(m): frozenset(t[0] for t in objs)
                   for m, objs in data.OBJECTIVES.items()}
# The real tables also carry entries data.py drops, so the test is containment, not
# equality: an exact-size check silently killed objectives in Chicago, Notre Dame and
# Wild West, the three missions where the counts differ.
MISSION_TEXTIDS_FULL = {
    data.STORY.index(m): frozenset(t[0] for t in objs) | mm.GC_EXTRA_OBJECTIVE_TEXTIDS.get(m, frozenset())
    for m, objs in data.OBJECTIVES.items()}


def live_objective_table():
    """(mission index, table address, count) once the table is provably this level's."""
    mission = current_mission()
    if mission is None:
        return None
    try:
        table = u32(mm.OBJ_TABLE_PTR)
        count = u32(mm.OBJ_COUNT_ADDR)
    except Exception:
        return None
    if not (0x80000000 <= table < 0x81800000) or not 0 < count <= 32:
        return None
    expected = MISSION_TEXTIDS.get(mission, frozenset())
    full = MISSION_TEXTIDS_FULL.get(mission, expected)
    if not expected or count > len(full):
        return None
    try:
        ids = {u32(table + i * mm.OBJ_STRIDE + mm.OBJ_TEXTID) for i in range(count)}
    except Exception:
        return None
    # every objective this mission cares about must be present, and nothing may be
    # present that does not belong to it -- that is what rules out a foreign table
    if not expected.issubset(ids) or not ids.issubset(full):
        return None
    return mission, table, count


# --- story clears -----------------------------------------------------------
# The per-level counter counts ATTEMPTS: its increment in the end-of-match recorder
# is not guarded by the win flag, and a quit-out bumps it. Treat the counter purely
# as an edge trigger and let the game's own win flag decide whether it was a clear.
def story_counter(profile, level):
    return u32(profile + mm.GAMEDATA_OFF + 0x28C + level * 8 + 4)


def match_was_won():
    return bool(u32(MATCH_FLAGS) & MATCH_WON)


def poll_story_clears(profile, previous):
    """Detect mission ends since the last poll.

    `previous` maps level id -> last counter value and is updated in place.
    Returns a list of (mission index, difficulty, won) for every mission that ended,
    including the ones that were quit -- callers decide what to do with those, but
    only `won` entries are clears.
    """
    ended = []
    difficulty = min(u32(CURRENT_DIFFICULTY), 2)
    # the load flags still describe the run that just ended: a story-map Challenge won
    # is not a story clear
    won = match_was_won() and in_story()
    for mission_index, level in enumerate(data.STORY_LEVEL_ID):
        count = story_counter(profile, level)
        was = previous.get(level)
        previous[level] = count
        if was is not None and count > was:
            ended.append((mission_index, difficulty, won))
    return ended


class ObjectiveWatcher:
    """Fires an objective only when it is SEEN to become complete.

    A snapshot cannot be trusted. The objective tables are static data whose status
    fields keep their values after a mission ends, and the level-init routine zeroes
    them a moment AFTER the level id changes -- so on re-entering a mission that was
    completed earlier, the table briefly still reads "everything complete". Reacting
    to that invents checks for work done in a previous run.

    Watching for a transition into status 5 removes the ambiguity: a stale table
    shows 5 immediately, never transitions into it, and is ignored; a real completion
    is always a change from something else.

    The cost is that objectives already complete when the client attaches are not
    seen. Those are picked up by the mission-clear fallback instead, since anything
    primary at the cleared difficulty was necessarily done.
    """

    def __init__(self):
        self.mission = None
        self.status = {}

    def poll(self):
        """(newly completed location ids, [(textID, old status, new status)])."""
        validated = live_objective_table()
        if validated is None:
            return set(), []
        mission, table, count = validated
        if mission != self.mission:
            # new level: take a baseline and deliberately fire nothing from it
            self.mission = mission
            self.status = {}
        newly, transitions = set(), []
        for i in range(count):
            entry = table + i * mm.OBJ_STRIDE
            try:
                tid = u32(entry + mm.OBJ_TEXTID)
                status = u32(entry + mm.OBJ_STATUS)
            except Exception:
                continue
            was = self.status.get(tid)
            self.status[tid] = status
            if was is None or status == was:
                continue
            transitions.append((tid, was, status))
            if status == mm.OBJ_STATUS_COMPLETE:
                loc = OBJ_LOCATION.get((mission, tid))
                if loc is not None:
                    newly.add(loc)
        return newly, transitions


def story_patch_present():
    """True when the disc patch that makes the first mission lockable is in memory.

    Reported rather than assumed: on a stock image the first mission is always
    selectable no matter what the client writes, and pretending otherwise would
    make the lock state quietly disagree with the game.
    """
    try:
        words = (u32(mm.STORY_PATCH_ADDR), u32(mm.STORY_PATCH_ADDR + 4))
    except Exception:
        return False
    return words == mm.STORY_PATCH_APPLIED


def arcade_patch_present():
    """True when the disc patch that makes group-leading matches lockable is live."""
    try:
        return u32(mm.ARCADE_PATCH_ADDR) == mm.ARCADE_PATCH_APPLIED
    except Exception:
        return False


class WeaponBlock:
    """Describe the seed's weapon shuffle to the game, once.

    The shuffle itself is applied by the patched disc: as each level starts
    loading, a cave rebuilds the gun tables from a block in low memory (layout in
    memmap.py under WPN_BLOCK). This class writes that block. Nothing here touches
    the gun tables -- doing so while a level loads hung the game, and after the
    load the assets are already chosen.

    The pristine copy of the tables comes from the game's own memory. That is
    sound because the tables are only ever changed by the cave, and the cave only
    runs once the magic word is present: if the word is absent the tables are
    untouched, and if it is present the block already holds the copy. The word is
    written last, so the cave never sees a half-written block.
    """

    def __init__(self):
        self.written = None

    @staticmethod
    def present():
        try:
            return dme.read_bytes(mm.WPN_BLOCK, 4) == mm.WPN_BLOCK_MAGIC
        except Exception:
            return False

    @staticmethod
    def _pristine():
        """The untouched tables, or None if they do not read plausibly yet."""
        if WeaponBlock.present():
            return dme.read_bytes(mm.WPN_BLOCK + mm.WPN_BLOCK_PRISTINE, mm.GUN_TABLES_LEN)
        tables = dme.read_bytes(mm.GUN_TABLE, mm.GUN_TABLES_LEN)
        for slot in range(mm.GUN_COUNT):
            entry = slot * mm.GUN_STRIDE
            for off in (mm.GUN_LEFT_STATS, mm.GUN_RIGHT_STATS):
                index = int.from_bytes(tables[entry + off:entry + off + 4], "big")
                # a loaded table holds either "no gun" or a small stats index
                if index != mm.GUN_NONE and index >= 0x80:
                    return None
        return tables

    @staticmethod
    def rows(remap, remap_by_level):
        """Per-level slot -> slot maps keyed by level id, from the seed's slot data."""
        out = {}
        for mission, level in enumerate(data.STORY_LEVEL_ID):
            this = remap_by_level.get(mission, remap) if remap_by_level else remap
            out[level] = {slot: target for slot, target in (this or {}).items()
                          if 0 < slot < mm.GUN_COUNT and 0 < target < mm.GUN_COUNT
                          and slot != target}
        return out

    @staticmethod
    def build(rows, pristine):
        if len(rows) > mm.WPN_ROW_COUNT:
            raise ValueError("%d rows, the cave reads %d" % (len(rows), mm.WPN_ROW_COUNT))
        block = bytearray(mm.WPN_BLOCK_LEN)
        block[mm.WPN_BLOCK_PRISTINE:mm.WPN_BLOCK_PRISTINE + mm.GUN_TABLES_LEN] = pristine
        for index, (level, remap) in enumerate(sorted(rows.items())):
            row = mm.WPN_BLOCK_ROWS + index * mm.WPN_ROW_STRIDE
            block[row:row + 4] = level.to_bytes(4, "big")
            for slot, target in remap.items():
                block[row + 4 + slot] = target
        block[:4] = mm.WPN_BLOCK_MAGIC
        return bytes(block)

    def write(self, rows):
        """Put the block in place. Returns True once it is there."""
        if self.written is not None and self.present():
            return True
        pristine = self._pristine()
        if pristine is None:
            return False
        block = self.build(rows, pristine)
        if self.present() and dme.read_bytes(mm.WPN_BLOCK, mm.WPN_BLOCK_LEN) == block:
            self.written = block
            return True
        # everything but the magic first; the cave keys on the magic
        dme.write_bytes(mm.WPN_BLOCK + 4, block[4:])
        dme.write_bytes(mm.WPN_BLOCK, block[:4])
        self.written = block
        return True


def skip_intro_patch_present() -> bool:
    try:
        return u32(mm.SKIP_INTRO_HOOK_ADDR) == mm.SKIP_INTRO_HOOK_APPLIED
    except Exception:
        return False


def set_skip_intro(on: bool) -> None:
    want = b"\x01" if on else b"\x00"
    if dme.read_bytes(mm.SKIP_INTRO_FLAG, 1) != want:
        dme.write_bytes(mm.SKIP_INTRO_FLAG, want)


def mark_intro_seen(profile: int, level: int) -> bool:
    """Unlock a story level's intro in the cinematics gallery, as playing it would have.
    True when the bit was new."""
    bit = mm.INTRO_CUTSCENE_OF_LEVEL.get(level)
    if bit is None:
        return False
    addr = profile + mm.CINEMATICS_OFF
    seen = u32(addr)
    if seen >> bit & 1:
        return False
    w32(addr, seen | 1 << bit)
    return True


def weapon_patch_present():
    """True when both halves of the weapon patch are in memory: the precache hook
    and the pak mounts that let Story find guns from other levels."""
    try:
        return (u32(mm.WEAPON_PATCH_ADDR) == mm.WEAPON_PATCH_APPLIED
                and u32(mm.WEAPON_CAVE_FLAGS_ADDR) == mm.WEAPON_CAVE_FLAGS_APPLIED
                and all(u32(addr) == mm.PAK_MOUNT_PATCH_APPLIED
                        for addr in mm.PAK_MOUNT_PATCH_ADDRS))
    except Exception:
        return False


# --- weapon gating ----------------------------------------------------------
def player_pawn(index=0):
    """Player pawn address, or None outside a level."""
    try:
        pawn = u32(mm.CHAR_PTR_LIST + index * 4)
    except Exception:
        return None
    if not 0x80000000 <= pawn < 0x81800000:
        return None
    # Sanity-check with something that is TRUE of every live player pawn: fists (slot
    # 0) are always owned. An earlier check assumed the first word was a pointer; it
    # is zero in practice, so gating silently did nothing for a whole play session.
    try:
        if u32(pawn + mm.PAWN_WEAPON_OWNED) != 1:
            return None
    except Exception:
        return None
    return pawn


def clear_ungranted_weapons(pawn, allowed):
    """Take away every weapon the player has not been granted.

    Re-applied continuously rather than once, because the game hands weapons out on
    pickup and at level start; the PC client clears locked slots every frame for the
    same reason.

    `allowed` is a set of weapon slots the player may hold. Slot 0 is fists and is
    never touched -- leaving the player with no way to act would be worse than a
    weapon leaking through.
    """
    cleared = []
    for slot in range(1, mm.PAWN_WEAPON_OWNED_COUNT):
        if slot in allowed:
            continue
        # Entries past the 38 real weapons are not weapons at all -- slot 43's flag
        # lights up on AMMO pickups. Clearing it every poll was cancelling ammo the
        # moment it was collected. Anything AP does not manage is left alone.
        if slot not in data.WEAPONS:
            continue
        addr = pawn + mm.PAWN_WEAPON_OWNED + slot * 4
        try:
            if u32(addr):
                w32(addr, 0)
                cleared.append(slot)
        except Exception:
            break

    # Clearing the owned flag removes a weapon from the selection wheel, but the one
    # IN HAND lives separately and stays usable until the player switches away -- so
    # a freshly picked-up weapon could be fired freely. Force the switch.
    try:
        current = u32(pawn + mm.PAWN_CURRENT_SLOT)
    except Exception:
        current = 0
    switched = None
    if 0 < current < mm.PAWN_WEAPON_OWNED_COUNT and current not in allowed:
        # Prefer a granted combat weapon; the baseline (fists, Temporal Uplink) is a
        # last resort, since the Uplink is a tool rather than something to fight with.
        fallback = 0
        ranked = ([s for s in sorted(allowed, reverse=True) if s not in data.WEAPON_BASELINE and s != 0]
                  + [s for s in sorted(allowed, reverse=True) if s in data.WEAPON_BASELINE and s != 0])
        for slot in ranked:
            if u32(pawn + mm.PAWN_WEAPON_OWNED + slot * 4):
                fallback = slot
                break
        switch_weapon(pawn, fallback)
        switched = (current, fallback)
    return cleared, switched


def switch_weapon(pawn, slot):
    """Ask the game to put `slot` in the player's hands.

    Write the REQUEST field only. +0xC8 is the weapon the player wants and +0xCC the
    one actually held; the game sees them differ and performs the switch itself --
    animation, stats, clip, everything. Three live runs settled this: writing +0xC8
    alone switched the weapon on screen; adding +0xCC (no difference left for the game
    to act on) or the stats fields (bypassing its own setup, hence an empty clip)
    stopped the switch happening at all. The PC client likewise writes only its slot.
    """
    w32(pawn + mm.PAWN_CURRENT_SLOT, slot)


# --- player state: DeathLink and the bonus packs ---------------------------------
def f32(addr: int) -> float:
    return struct.unpack(">f", dme.read_bytes(addr, 4))[0]


def wf32(addr: int, value: float) -> None:
    dme.write_bytes(addr, struct.pack(">f", value))


def _ram(addr: int) -> bool:
    return 0x80000000 <= addr < 0x81800000


class Player(typing.NamedTuple):
    ctrl: int
    obj: int
    pawn: int


def player() -> typing.Optional[Player]:
    """Player 1 in a level -- control struct, object and pawn -- or None.

    The three are cross-checked (the object points back at the pawn), so a stale
    control pointer left over from the previous level is never written through.
    """
    pawn = player_pawn()
    if pawn is None:
        return None
    try:
        ctrl = u32(mm.PLAYER_CTRL_PTR)
        if not _ram(ctrl):
            return None
        obj = u32(ctrl + mm.CTRL_OBJECT)
        if not _ram(obj) or u32(obj + mm.OBJ_PAWN) != pawn:
            return None
    except Exception:
        return None
    return Player(ctrl, obj, pawn)


def is_dead(p: Player) -> bool:
    return f32(p.obj + mm.OBJ_HEALTH) <= 0.0 or u32(p.ctrl + mm.CTRL_STATE) == mm.STATE_DEAD


# Movement states a kill may interrupt: on foot (0, 1, 3) or already falling. The
# others are turrets, remote views, scripted moves and death itself; a DeathLink
# waits for the player to leave them rather than knock them out of one.
KILLABLE_STATES = frozenset({0, 1, 2, 3})


def kill_player(p: Player) -> bool:
    """Kill the player through the game's own death sequence. False if not now.

    The death handler itself defers a death that happens mid-fall to the landing,
    where the falling state checks health and runs the full sequence -- animation,
    respawn or mission failure, and kill credit. So: no health, and falling. The
    next frame lands, finds health at zero, and dies properly. Writing health alone
    leaves the player standing at zero health, as it does on PC.
    """
    if is_dead(p) or u32(p.ctrl + mm.CTRL_STATE) not in KILLABLE_STATES:
        return False
    wf32(p.obj + mm.OBJ_HEALTH, 0.0)
    w32(p.ctrl + mm.CTRL_STATE, mm.STATE_FALLING)
    return True


def refill_health(p: Player) -> None:
    """Health to this character's own maximum (difficulty and level set it)."""
    wf32(p.obj + mm.OBJ_HEALTH, f32(p.obj + mm.OBJ_HEALTH_MAX))


def refill_armor(p: Player) -> None:
    """Armor to its maximum. The game does not clamp, so always copy the max."""
    wf32(p.pawn + mm.PAWN_ARMOR, f32(p.pawn + mm.PAWN_ARMOR_MAX))


def refill_ammo(p: Player) -> typing.List[int]:
    """Fill every ammo type the player's weapons use, primary and alt-fire.

    Only weapons the player owns, read through the live gun table so a shuffled
    slot fills whatever it actually fires. Returns the ammo types filled.
    """
    filled = []
    for slot in range(1, mm.PAWN_WEAPON_OWNED_COUNT):
        if slot not in data.WEAPONS or not u32(p.pawn + mm.PAWN_WEAPON_OWNED + slot * 4):
            continue
        gun = mm.GUN_TABLE + slot * mm.GUN_STRIDE
        for hand in (mm.GUN_LEFT_STATS, mm.GUN_RIGHT_STATS):
            stats = u32(gun + hand)
            if stats == mm.GUN_NONE or stats >= 0x80:
                continue
            for field in (mm.STATS_AMMO_TYPE, mm.STATS_ALT_AMMO_TYPE):
                kind = u32(mm.STATS_TABLE + stats * mm.STATS_STRIDE + field)
                if not 0 < kind < mm.AMMO_TYPE_COUNT or kind in filled:
                    continue
                most = u32(mm.AMMO_TYPE_TABLE + kind * mm.AMMO_TYPE_STRIDE + mm.AMMO_TYPE_MAX)
                if not 0 < most < 0x100000:
                    most = 100
                w32(p.pawn + mm.PAWN_AMMO + kind * 4, most)
                filled.append(kind)
    return filled


# --- cheats: trap items -------------------------------------------------------------
class CheatState(typing.NamedTuple):
    enabled: bool
    allowed: bool
    override: int


def cheat_state(bit: int) -> CheatState:
    return CheatState(bool(u32(mm.CHEATS_ENABLED) >> bit & 1), bool(u32(mm.CHEATS_ALLOWED) >> bit & 1),
                      dme.read_byte(mm.CHEAT_UNLOCK_OVERRIDE))


def cheat_on(bit: int) -> None:
    """Put one cheat in effect. Re-asserted every poll: a level load rebuilds the
    allowed mask, and the menus would otherwise see the override drop."""
    w32(mm.CHEATS_ENABLED, u32(mm.CHEATS_ENABLED) | 1 << bit)
    w32(mm.CHEATS_ALLOWED, u32(mm.CHEATS_ALLOWED) | 1 << bit)
    if not dme.read_byte(mm.CHEAT_UNLOCK_OVERRIDE):
        dme.write_bytes(mm.CHEAT_UNLOCK_OVERRIDE, b"\1")


def cheat_off(bit: int, before: CheatState) -> None:
    """Undo cheat_on, leaving whatever was already there before the trap."""
    if not before.enabled:
        w32(mm.CHEATS_ENABLED, u32(mm.CHEATS_ENABLED) & ~(1 << bit))
    if not before.allowed:
        w32(mm.CHEATS_ALLOWED, u32(mm.CHEATS_ALLOWED) & ~(1 << bit))
    dme.write_bytes(mm.CHEAT_UNLOCK_OVERRIDE, bytes([before.override]))


# --- the seed's profile ---------------------------------------------------------------
def profile_tag(profile: int) -> int:
    return u32(profile + mm.PROFILE_TAG_OFF)


def set_profile_tag(profile: int, tag: int) -> None:
    w32(profile + mm.PROFILE_TAG_OFF, tag)


def profile_has_progress(profile: int) -> bool:
    """Anything a check could be read from: a trophy, a story mission played, or story
    locks a client has already written (a profile from an earlier seed)."""
    for index in range(mm.TROPHY_COUNT):
        if u32(profile + mm.TROPHY_OFF + index * mm.TROPHY_STRIDE + mm.TROPHY_TIER):
            return True
    if any(story_counter(profile, level) for level in data.STORY_LEVEL_ID):
        return True
    return any(u32(profile + off) for off in mm.STORY_CLEAR_OFF.values())


# --- ammo under the weapon shuffle -------------------------------------------------------
# A level's weapon pickups carry their amounts from the level's own data, sized for the
# gun the level was built around, while the type and the cap come from the gun table --
# the shuffled-in gun's own. So a Minigun in a pistol's place gets pistol-sized pickups.
# AmmoScaler turns each gain into the same share of the shuffled-in gun's maximum.
def _native_types(tables: bytes, slot: int) -> typing.List[typing.Tuple[int, int]]:
    """(primary, alt-fire) ammo types slot fires in the pristine tables, one pair per hand."""
    out = []
    for hand in (mm.GUN_RIGHT_STATS, mm.GUN_LEFT_STATS):
        at = slot * mm.GUN_STRIDE + hand
        stats = int.from_bytes(tables[at:at + 4], "big")
        if stats == mm.GUN_NONE or stats >= 0x80:
            continue
        base = mm.STATS_TABLE + stats * mm.STATS_STRIDE
        pair = (u32(base + mm.STATS_AMMO_TYPE), u32(base + mm.STATS_ALT_AMMO_TYPE))
        if pair not in out:
            out.append(pair)
    return out


def ammo_max(kind: int) -> int:
    if not 0 < kind < mm.AMMO_TYPE_COUNT:
        return 0
    return u32(mm.AMMO_TYPE_TABLE + kind * mm.AMMO_TYPE_STRIDE + mm.AMMO_TYPE_MAX)


def ammo_scale(remap: typing.Dict[int, int], level_slots) -> typing.Dict[int, typing.Tuple[int, float]]:
    """{ammo type gained: (type to credit, factor)} for one level's shuffle.

    For each shuffled slot X -> Y and each of its (X type, Y type) pairs:
      * Y's type, fired by X alone in this level, gains max(Y)/max(X) per round --
        the pickups that fill it were sized for X;
      * X's type, fired by nothing in this level any more, is turned into Y's at the
        same rate, so X's own ammo pickups still feed the gun in X's place.
    A type two of the level's guns fire is left alone: its gains cannot be told apart.
    Needs the pristine tables from the weapon block; empty without them.
    """
    if not remap or not WeaponBlock.present():
        return {}
    tables = dme.read_bytes(mm.WPN_BLOCK + mm.WPN_BLOCK_PRISTINE, mm.GUN_TABLES_LEN)
    fires: typing.Dict[int, typing.Set[int]] = {}          # ammo type -> slots firing it now
    for slot in level_slots:
        for pair in _native_types(tables, remap.get(slot, slot)):
            for kind in pair:
                if kind:
                    fires.setdefault(kind, set()).add(slot)
    out = {}
    for slot, target in sorted(remap.items()):
        if slot == target or slot not in level_slots:
            continue
        for pair_x, pair_y in zip(_native_types(tables, slot), _native_types(tables, target)):
            for tx, ty in zip(pair_x, pair_y):
                if not tx or not ty or tx == ty or not ammo_max(tx) or not ammo_max(ty):
                    continue
                factor = ammo_max(ty) / ammo_max(tx)
                if fires.get(ty) == {slot}:
                    out.setdefault(ty, (ty, factor))
                if not fires.get(tx):
                    out.setdefault(tx, (ty, factor))
    return out


class AmmoScaler:
    """Apply ammo_scale to the player's gains, poll by poll. Losses (firing, reloading)
    pass as they are; fractions carry over, so ten gains worth 0.4 each still come to four.

    A pawn first seen right after a load is scaled from zero, since what it holds was
    handed out by the level. One already there when the client started is taken as it
    is: an earlier client scaled it.
    """

    def __init__(self):
        self.pawn = None
        self.watched_load = False
        self.last: typing.List[int] = []
        self.carry: typing.Dict[int, float] = {}

    def reset(self):
        """No player (a load, or the front end): the next pawn is a fresh one."""
        self.pawn = None
        self.watched_load = True

    def rebase(self, pawn: int):
        """Take the pawn's ammo as it is now -- after a change that is not a pickup (an
        Ammo Pack), so the next poll does not scale it."""
        if pawn == self.pawn:
            raw = dme.read_bytes(pawn + mm.PAWN_AMMO, mm.AMMO_TYPE_COUNT * 4)
            self.last = [int.from_bytes(raw[i * 4:i * 4 + 4], "big", signed=True) for i in range(mm.AMMO_TYPE_COUNT)]

    def poll(self, pawn: int, scale: typing.Dict[int, typing.Tuple[int, float]]) -> int:
        raw = dme.read_bytes(pawn + mm.PAWN_AMMO, mm.AMMO_TYPE_COUNT * 4)
        now = [int.from_bytes(raw[i * 4:i * 4 + 4], "big", signed=True) for i in range(mm.AMMO_TYPE_COUNT)]
        if pawn != self.pawn:
            fresh = self.watched_load
            self.pawn, self.carry, self.watched_load = pawn, {}, False
            self.last = [0] * mm.AMMO_TYPE_COUNT if fresh else list(now)
        want = list(now)
        for kind, (credit, factor) in scale.items():
            gain = now[kind] - self.last[kind]
            if gain <= 0:
                continue
            if credit != kind:
                want[kind] = self.last[kind]                # nothing fires it here: move it over
            share = gain * factor + self.carry.get(credit, 0.0)
            whole = int(share)
            self.carry[credit] = share - whole
            base = want[credit] - (gain if credit == kind else 0)
            want[credit] = max(0, min(base + whole, ammo_max(credit)))
        changed = 0
        for kind in range(mm.AMMO_TYPE_COUNT):
            if want[kind] != now[kind]:
                w32(pawn + mm.PAWN_AMMO + kind * 4, want[kind])
                changed += 1
        self.last = want
        return changed


# Weapons used straight from their ammo -- placed mines, TNT and the Brick. Taking the
# owned flag away is the gate for a gun; for these the ammo is emptied as well, so one
# can never be used in the moment between a pickup and the next poll.
SPENT_FROM_AMMO = data.WEAPON_MINE | {34}


def clear_spent_ammo(pawn: int, allowed: typing.Set[int]) -> typing.List[int]:
    """Empty the ammo of ungranted placed/thrown weapons, unless a granted weapon fires
    the same type. Read through the live gun table, so it follows the shuffle."""
    def types(slot):
        out = set()
        for hand in (mm.GUN_LEFT_STATS, mm.GUN_RIGHT_STATS):
            stats = u32(mm.GUN_TABLE + slot * mm.GUN_STRIDE + hand)
            if stats != mm.GUN_NONE and stats < 0x80:
                base = mm.STATS_TABLE + stats * mm.STATS_STRIDE
                out |= {t for t in (u32(base + mm.STATS_AMMO_TYPE), u32(base + mm.STATS_ALT_AMMO_TYPE))
                        if 0 < t < mm.AMMO_TYPE_COUNT}
        return out
    keep = set()
    for slot in allowed:
        if 0 < slot < mm.GUN_COUNT:
            keep |= types(slot)
    emptied = []
    for slot in sorted(SPENT_FROM_AMMO - set(allowed)):
        for kind in sorted(types(slot) - keep):
            if u32(pawn + mm.PAWN_AMMO + kind * 4):
                w32(pawn + mm.PAWN_AMMO + kind * 4, 0)
                emptied.append(kind)
    return emptied
