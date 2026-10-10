"""Archipelago client for TimeSplitters 2 (GameCube, GTSE4F) running under Dolphin.

A plain Python client that reads and writes emulator memory, in the established
Archipelago GameCube style. Locks and unlocks are memory writes to state the game
already consults; the few things that are hardcoded in the executable (the first
story mission, the first match of each group, the gun precache) are handled by the
patched copy of the disc this client writes for you.

Two ideas carry the whole design:

1. Checks and gates are different structures. Arcade and challenge progress is
   read from the trophy records, while the gate that decides what is playable
   reads a separate medal bitfield. The client can therefore own the bitfield
   outright without disturbing what it reads.

2. Story does not have that luxury -- one bitfield is both the record of what was
   cleared and the gate on what may be played. So story completion is detected
   from the per-level completion counter plus the current-difficulty global
   instead, which the gate never touches.

The GameCube build keeps its own progression system, unlike the PC build where a
mod opens everything up front. The game will keep setting the same bits the client
gates with, so the gate is re-asserted every poll rather than written once.
"""

import asyncio
import os
import subprocess
import sys
import time
import typing
import zlib

import dolphin_memory_engine as dme

import Utils
from CommonClient import (
    ClientCommandProcessor, CommonContext, get_base_parser, gui_enabled, logger, server_loop,
)
from NetUtils import ClientStatus

from .. import data
from .. import memmap as mm
from .. import music
from .. import patcher as patch_iso
from . import gamestate

GAME = "TimeSplitters 2"
SLOT_DATA_VERSION = 12
POLL_INTERVAL = 0.4

# Routine detail (objectives, gating, the shuffle) goes to the log file only: the window
# shows the checks sent, the server's messages, and anything the player must act on.
FILE_ONLY = {"skip_gui": True, "NoStream": True}


def note(msg, *args):
    logger.info(msg, *args, extra=FILE_ONLY)


# Globals the end-of-match recorder uses.
CURRENT_DIFFICULTY = 0x803D0AEC       # 0 Easy, 1 Normal, 2 Hard
UNLOCK_ALL_OVERRIDE = 0x80468450      # [0] story [1] arcade [2] challenge -- must stay 0

STORY_BASE = data.BASE_ID + 0x1400
TROPHY_BASE = data.BASE_ID + 0x1000

# Two different index spaces, and conflating them is the single easiest way to
# make this client silently wrong:
#
#   AP index    position in data.TROPHY_EVENTS -- what location ids are built from
#   game index  position in the game's 66-record table -- what memory is addressed by
#
# They agree at none of the 66 positions: the apworld enumerates arcade first, the
# game enumerates challenges first. Always convert explicitly.
AP_EVENT_NAMES = [name for (_, _, name) in data.TROPHY_EVENTS]
AP_INDEX_OF_NAME = {name: i for i, name in enumerate(AP_EVENT_NAMES)}
GAME_INDEX_OF_NAME = data.TROPHY_EVENT_INDEX                 # name -> game record index
AP_TO_GAME = [GAME_INDEX_OF_NAME[n] for n in AP_EVENT_NAMES]
GAME_TO_AP = [AP_INDEX_OF_NAME[n] for n in data.TROPHY_TABLE_ORDER]
assert sorted(AP_TO_GAME) == list(range(len(AP_EVENT_NAMES))), "event index mapping is not a bijection"

DIFF_BIT = {"Easy": 1, "Normal": 2, "Hard": 4}
LOCATION_NAME = {loc: name for name, loc in data.location_name_to_id.items()}

# One-time items, counted rather than held: how many have arrived against how many
# the client has already applied, the latter kept in the server's data storage.
PACK_IDS = [data.item_name_to_id[name] for name in data.BONUS_ITEMS]     # health, armor, ammo
TRAP_IDS = [data.item_name_to_id[name] for name in data.TRAP_ID_ORDER]
TRAP_SECONDS = 30
# A DeathLink kill takes a frame or two to land. If it never registers as a death,
# stop waiting for it rather than swallow the player's next real one.
KILL_SETTLE_SECONDS = 10
# A story level runs for a while before its player spawns (the load, the intro). Only a
# player still missing after this long is worth telling anyone about.
NO_PAWN_GRACE_SECONDS = 30


def _build_objectives():
    """mission index -> [(location id, difficulty mask)] for objectives a clear implies.

    An objective that is PRIMARY on difficulty D is required to finish the mission on
    D, so clearing at D proves it was done. A handful are instead earned simply by
    finishing (MISSION_COMPLETE_OBJS); for those the secondary difficulties count too.

    Objectives that are only ever SECONDARY are genuinely optional and cannot be
    inferred from a clear -- they are left out here and listed in UNDERIVABLE.
    """
    by_mission: typing.Dict[int, list] = {}
    underivable = []
    for loc_name, (tid, prim, sec, mission) in data.OBJ_LOC_INFO.items():
        loc_id = data.location_name_to_id[loc_name]
        mission_index = data.STORY.index(mission)
        difficulties = prim
        if tid == data.ESCAPE_TEXTID or (mission, tid) in data.MISSION_COMPLETE_OBJS:
            difficulties = prim | sec           # earned by finishing, not by doing
        if not difficulties:
            underivable.append(loc_name)
            continue
        mask = 0
        for d in difficulties:
            mask |= DIFF_BIT[d]
        by_mission.setdefault(mission_index, []).append((loc_id, mask))
    return by_mission, underivable


OBJECTIVES_BY_MISSION, UNDERIVABLE_OBJECTIVES = _build_objectives()


def u32(addr: int) -> int:
    return int.from_bytes(dme.read_bytes(addr, 4), "big")


def w32(addr: int, value: int) -> None:
    dme.write_bytes(addr, (value & 0xFFFFFFFF).to_bytes(4, "big"))


class TS2GCCommandProcessor(ClientCommandProcessor):
    def _cmd_dolphin(self):
        """Report the emulator connection and which profile is being tracked."""
        if self.ctx.hooked:
            logger.info("Hooked to Dolphin, profile slot %s (%r)",
                        self.ctx.profile_slot, self.ctx.profile_name)
        else:
            logger.info("Not hooked. Start Dolphin and boot TimeSplitters 2.")

    def _cmd_gate(self):
        """Show what the client currently has unlocked in game."""
        self.ctx.log_gate()

    def _cmd_launch(self):
        """Start Dolphin on the patched image (the client does this at startup too)."""
        asyncio.create_task(launch(self.ctx), name="Launch")

    def _cmd_weapons(self):
        """Show what each weapon currently behaves as, and which are granted."""
        granted = sorted(self.ctx.granted_weapons)
        logger.info("Weapons granted: %s", ", ".join(
            data.WEAPONS[s] for s in granted if s in data.WEAPONS) or "none")
        remap = self.ctx.weapon_remap
        if not remap:
            logger.info("This seed does not shuffle weapons.")
            return
        for slot, target in sorted(remap.items()):
            if slot == target or slot not in data.WEAPONS or target not in data.WEAPONS:
                continue
            logger.info("  %-28s behaves as  %s", data.WEAPONS[slot], data.WEAPONS[target])

    def _cmd_use_profile(self):
        """Play this seed on the profile loaded now, even though it already has progress
        (which is then sent as checks)."""
        if not self.ctx.hooked or not self.ctx.profile_name:
            logger.info("Load the profile in the game first.")
            return
        self.ctx.force_profile = self.ctx.profile_name
        logger.info("Registering profile %r on the next poll.", self.ctx.profile_name)

    def _cmd_deathlink(self):
        """Toggle DeathLink for this session."""
        on = "DeathLink" not in self.ctx.tags
        asyncio.create_task(self.ctx.update_death_link(on))
        logger.info("DeathLink is now %s.", "ON" if on else "OFF")

    def _cmd_readonly(self):
        """Toggle whether the client is allowed to write to the game."""
        self.ctx.read_only = not self.ctx.read_only
        logger.info("Read-only is now %s.", "ON -- no writes" if self.ctx.read_only else "OFF -- writing")


class TS2GCContext(CommonContext):
    game = GAME
    command_processor = TS2GCCommandProcessor
    # receive our own items, other players' items, and the starting inventory: the
    # client applies every unlock from ReceivedItems, so it needs all three.
    items_handling = 0b111

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        self.hooked = False
        self.ever_hooked = False
        self.lost_reported = False
        self.dolphin_exit_reported = False
        self.mouse_process = None
        self.profile_slot = 0
        self.profile_name = ""
        self.slot_data: dict = {}
        # in-game names the player has been granted
        self.granted_units: typing.Set[str] = set()
        self.time_crystals = 0
        # what the client believes has genuinely been achieved
        self.story_cleared: typing.Dict[int, int] = {0: 0, 1: 0, 2: 0}   # diff -> mission bitmask
        self.level_completions: typing.Dict[int, int] = {}               # level id -> story count
        self.sent_locations: typing.Set[int] = set()
        self.read_only = False
        self.dolphin_process = None
        self.launching = False             # a launch is preparing the disc
        self.story_patched = False
        self.arcade_patched = False
        self.weapon_patched = False
        self.skip_patched = False
        self.reported_patch_state = False
        self.warned_about_writing = False
        self.objectives = gamestate.ObjectiveWatcher()
        self.weapons = gamestate.WeaponBlock()
        self.weapon_remap = {}
        self.granted_weapons = set()
        self.warned_no_pawn = False
        self.no_pawn_since = None
        self.reported_gating = False
        self.weapon_remap_by_level = {}
        self.reported_weapons = False
        self.reported_mode = None
        self.prefs_done = None            # profile names already set up, from data storage
        self.objective_hits: typing.Set[int] = set()
        self.goal_reached = False
        self.primed = False
        # DeathLink
        self.arcade_death_threshold = 10
        self.arcade_deaths = 0
        self.was_alive = False
        self.death_pending = None          # the incoming DeathLink waiting to be applied
        self.kill_sent_at = None           # when our own kill was written (its death is not echoed)
        self.reported_kill_wait = False
        # bonus packs and traps: arrived vs applied
        self.packs_received = [0, 0, 0]
        self.traps_received = [0] * len(TRAP_IDS)
        self.trap = None                   # (index, bit, ends at, cheat state before)
        # the seed's profile
        self.room_seed_name = None
        self.pending_profile = None        # tagged on the last poll, confirmed on this one
        self.force_profile = None          # /use_profile
        self.profile_said = None
        # ammo under the weapon shuffle
        self.ammo_scaler = gamestate.AmmoScaler()
        self.ammo_scale_for = None         # (mission, scale) computed for the level loaded

    def run_gui(self):
        """The standard Archipelago client window (connection box, log, command bar), plus a
        status line and bar for the launch's disc preparation."""
        import logging
        import threading
        # kvui first: it sets kivy up before kivy loads (DPI awareness off; kivy's own makes the window
        # re-lay itself out endlessly), and that can only happen before the first kivy import
        from kvui import GameManager, LogtoUI
        from kivy.clock import Clock
        from kivy.metrics import dp
        from kivymd.uix.boxlayout import MDBoxLayout
        from kivymd.uix.label import MDLabel
        from kivymd.uix.progressindicator import MDLinearProgressIndicator

        def on_main_thread(fn):
            """The window's log is not thread-safe; lines from the disc preparation thread are
            handed to the main thread."""
            def call(*args):
                if threading.current_thread() is threading.main_thread():
                    fn(*args)
                else:
                    Clock.schedule_once(lambda _dt: fn(*args))
            return call

        class TS2Manager(GameManager):
            logging_pairs = [("Client", "Archipelago")]
            base_title = "Archipelago TimeSplitters 2 Client"

            def build(self):
                layout = super().build()
                # a status line and bar under the server bar while a launch prepares the disc
                self.prep_box = MDBoxLayout(orientation="vertical", size_hint_y=None, height=0, opacity=0,
                                            padding=(dp(8), 0))
                self.prep_label = MDLabel(text="", size_hint_y=None, height=dp(24))
                self.prep_bar = MDLinearProgressIndicator(size_hint_y=None, height=dp(4), max=1, value=0)
                self.prep_box.add_widget(self.prep_label)
                self.prep_box.add_widget(self.prep_bar)
                # kivy lists children last-first: this index puts it just below Archipelago's own bar
                self.grid.add_widget(self.prep_box, index=self.grid.children.index(self.progressbar))
                for name in [None] + list(logging.Logger.manager.loggerDict):
                    for handler in logging.getLogger(name).handlers:
                        if isinstance(handler, LogtoUI):
                            handler.on_log = on_main_thread(handler.on_log)
                return layout

            def show_prep(self, text, done, total):
                self.prep_box.height, self.prep_box.opacity = dp(32), 1
                self.prep_label.text = text
                self.prep_bar.max = max(1, total or 0)
                self.prep_bar.value = done or 0

            def hide_prep(self):
                self.prep_box.height, self.prep_box.opacity = 0, 0

        self.ui = TS2Manager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")

    def show_prep(self, text, done=None, total=None):
        """Launch progress in the window (any thread), and in the log. done/total fill the bar;
        without them it stays empty and the line alone says what is happening."""
        note(text)
        if self.ui is not None and hasattr(self.ui, "prep_box"):
            from kivy.clock import Clock
            Clock.schedule_once(lambda _dt: self.ui.show_prep(text, done, total))

    def hide_prep(self):
        if self.ui is not None and hasattr(self.ui, "prep_box"):
            from kivy.clock import Clock
            Clock.schedule_once(lambda _dt: self.ui.hide_prep())

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        if not self.auth:                      # --name skips the interactive prompt
            await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict):
        if cmd == "RoomInfo":
            self.room_seed_name = args.get("seed_name")     # this CommonClient keeps it nowhere
        elif cmd == "Connected":
            self.slot_data = args.get("slot_data", {}) or {}
            version = self.slot_data.get("version")
            if version != SLOT_DATA_VERSION:
                logger.warning("slot_data version %s, client expects %s -- continuing, but "
                               "some options may not behave as intended.", version, SLOT_DATA_VERSION)
            for name in self.slot_data.get("starting_units", []) or []:
                self.granted_units.add(name)
            # which profiles have had the yaml preferences written, so it happens once;
            # and how many packs and traps have already been applied
            self.set_notify(self.prefs_key(), self.grant_key(), self.profile_key())
            self.arcade_death_threshold = int(self.slot_data.get("arcade_death_link_threshold", 10))
            self.arcade_deaths = 0
            if self.slot_data.get("death_link"):
                asyncio.create_task(self.update_death_link(True))
            # slot_data comes back through JSON, so every key is a string
            slot_of_item = {name: slot for slot, name in data.WEAPON_ITEM_OF.items()}
            for name in self.slot_data.get("starting_weapons") or []:
                if name in slot_of_item:
                    self.granted_weapons.add(slot_of_item[name])
            self.weapon_remap = {int(k): int(v)
                                 for k, v in (self.slot_data.get("weapon_remap") or {}).items()}
            self.weapon_remap_by_level = {
                int(mi): {int(k): int(v) for k, v in mapping.items()}
                for mi, mapping in (self.slot_data.get("weapon_remap_by_level") or {}).items()}
            if gamestate.UNDERIVABLE_OBJECTIVES:
                note(
                    "%d optional objective(s) cannot be detected from a mission clear and "
                    "will not send automatically: %s",
                    len(gamestate.UNDERIVABLE_OBJECTIVES),
                    ", ".join(sorted(gamestate.UNDERIVABLE_OBJECTIVES)))
            self.primed = False          # re-seed true progress from the server's checks
        elif cmd == "ReceivedItems":
            before = len(self.granted_units)
            for item in args["items"]:
                self.handle_item(item.item)
            self.count_items()
            gained = len(self.granted_units) - before
            if gained or self.time_crystals:
                note("Received %d item(s): %d unlock(s) now held, %d Time Crystal(s).",
                            len(args["items"]), len(self.granted_units), self.time_crystals)
                self.log_gate()

    # ---- items -----------------------------------------------------------
    def count_items(self):
        """Counted items, from the full list the server has sent this session.

        Counted afresh rather than incremented: a reconnect resends every item, which
        used to add each Time Crystal a second time.
        """
        ids = [item.item for item in self.items_received]
        self.time_crystals = ids.count(data.item_name_to_id[data.TIME_CRYSTAL_ITEM])
        self.packs_received = [ids.count(i) for i in PACK_IDS]
        self.traps_received = [ids.count(i) for i in TRAP_IDS]

    def handle_item(self, item_id: int):
        if item_id == data.item_name_to_id.get(data.TIME_CRYSTAL_ITEM):
            return                              # counted in count_items
        index = item_id - data.BASE_ID
        weapon_slot = index - data.WEAPON_ITEM_ID_BASE
        if 0 <= weapon_slot < 64 and weapon_slot in data.WEAPONS:
            self.granted_weapons.add(weapon_slot)
            return
        if 0 <= index < len(data.UNIT_NAMES):
            self.granted_units.add(data.UNIT_NAMES[index])

    def granted_events(self) -> typing.Set[int]:
        """Granted trophy events, as GAME record indices (ready to address memory)."""
        return {GAME_INDEX_OF_NAME[name] for name in self.granted_units
                if name in GAME_INDEX_OF_NAME}

    def granted_missions(self) -> typing.Set[int]:
        """Missions to open. The final one also waits for the Time Crystals: clearing it early would not
        count for the goal, and the player would only have to play it again."""
        need = self.slot_data.get("time_crystals_required", 0)
        return {mi for mi, m in enumerate(data.STORY) if m in self.granted_units
                and (m != data.FINAL_STORY_MISSION or self.time_crystals >= need)}

    # ---- emulator --------------------------------------------------------
    def _hook(self) -> bool:
        if dme.is_hooked():
            return True
        dme.hook()
        if not dme.is_hooked():
            return False
        try:
            return dme.read_bytes(0x80000000, 6).decode("latin-1") == mm.GAME_ID
        except Exception:
            return False

    def ensure_hooked(self) -> bool:
        was = self.hooked
        self.hooked = self._hook()
        if self.hooked and not was:
            note("Hooked to Dolphin running %s.", mm.GAME_ID)
            self.ever_hooked = True
            self.lost_reported = False
        if (self.dolphin_process is not None and self.dolphin_process.poll() is not None
                and not self.dolphin_exit_reported):
            self.dolphin_exit_reported = True
            logger.warning("Dolphin has closed. Restart this client to play again -- it launches the game fresh.")
        elif not self.hooked and self.ever_hooked and not self.lost_reported:
            self.lost_reported = True
            logger.warning("Lost the game. If it crashed or was stopped, close Dolphin and restart this client: "
                           "it launches the game fresh. Restart both together whenever either one crashes.")
        return self.hooked

    def profile_base(self) -> typing.Optional[int]:
        active = u32(mm.ACTIVE_PROFILE_ADDR)        # player 1's index into the array
        self.profile_slot = active if 0 <= active < mm.PROFILE_MAX else 0
        base = u32(mm.PROFILE_BASE_PTR)
        if not 0x80000000 <= base < 0x81800000:
            return None
        addr = base + self.profile_slot * mm.PROFILE_STRIDE
        try:
            self.profile_name = dme.read_bytes(addr, mm.PROFILE_NAME_LEN).split(b"\0", 1)[0].decode("latin-1")
        except Exception:
            return None
        return addr if self.profile_name else None

    # ---- which profile is this seed's ----------------------------------------
    # A seed is played on one profile, claimed by a tag in its save data. Only a profile
    # with no progress is claimed on its own, so a profile played outside this seed never
    # has its trophies and clears sent as this seed's checks.
    def profile_key(self):
        return "ts2gc_profile_%s_%s" % (self.team, self.slot)

    def seed_tag(self) -> typing.Optional[int]:
        seed = self.room_seed_name or getattr(self, "server_seed_name", None) or getattr(self, "seed_name", None)
        if not seed or self.slot is None:
            return None
        return zlib.crc32(("%s|%s|%s" % (seed, self.team, self.slot)).encode()) or 1

    def say_profile(self, key, msg, *args, warn=False):
        if key != self.profile_said:
            self.profile_said = key
            (logger.warning if warn else logger.info)(msg, *args)

    def seed_profile_hint(self) -> str:
        name = self.stored_data.get(self.profile_key())
        return " This seed's profile is %r." % name if name else ""

    async def registered(self, name: str):
        await self.send_msgs([{"cmd": "Set", "key": self.profile_key(), "default": "", "want_reply": False,
                               "operations": [{"operation": "replace", "value": name}]}])

    async def check_profile(self, profile: int) -> bool:
        """True when the profile loaded is this seed's (claiming a fresh one)."""
        tag = self.seed_tag()
        if tag is None:
            self.say_profile(("no seed",), "Waiting for the server's seed name before checking the profile.")
            return False
        name = self.profile_name
        have = gamestate.profile_tag(profile)
        if self.force_profile is not None:
            forced, self.force_profile = self.force_profile, None
            if forced != name:
                logger.info("Not registered: load your own profile and try /use_profile again.")
                return False
            if not self.read_only:
                gamestate.set_profile_tag(profile, tag)
            self.pending_profile = None
            self.say_profile(("ours", name), "Profile %r registered to this seed; its existing progress is sent "
                             "as checks.", name)
            await self.registered(name)
            return True
        used = gamestate.profile_has_progress(profile)
        if have == tag:
            if self.pending_profile == name:
                self.pending_profile = None
                if used:
                    # a card profile loaded over the one just claimed: let it go
                    if not self.read_only:
                        gamestate.set_profile_tag(profile, 0)
                    return False
                self.say_profile(("ours", name), "Profile %r registered to this seed. Play this seed on it; "
                                 "other profiles are ignored.", name)
                await self.registered(name)
            self.say_profile(("ours", name), "Playing on profile %r (this seed's).", name)
            return True
        was_pending, self.pending_profile = self.pending_profile == name, None
        if have == 0 and not used:
            if was_pending:
                self.say_profile(("lost", name), "Could not register profile %r: the game did not keep the "
                                 "client's mark. Please report this.", name, warn=True)
            if self.read_only:
                self.say_profile(("ours", name), "Playing on profile %r (read-only: not registered).", name)
                return True
            gamestate.set_profile_tag(profile, tag)
            self.pending_profile = name
        elif have:
            self.say_profile(("other", name), "Profile %r belongs to a different seed, so it is ignored. Create a "
                             "new profile for this seed.%s", name, self.seed_profile_hint(), warn=True)
        else:
            self.say_profile(("used", name), "Profile %r already has progress, so it is ignored: nothing is sent "
                             "or unlocked on it. Create a new profile for this seed.%s To play this seed on %r "
                             "anyway and send its progress as checks, type /use_profile.",
                             name, self.seed_profile_hint(), name, warn=True)
        return False

    # ---- reading progress -> checks --------------------------------------
    def seed_from_server(self):
        """Rebuild true progress from the server's checked locations.

        The server is the durable record. Rebuilding from it rather than from the
        game matters because the client overwrites the story bitfield for gating,
        so the game's own copy is not trustworthy after the first write.
        """
        for loc in self.checked_locations:
            offset = loc - STORY_BASE
            # objective ids live above the story block; only story ids rebuild progress
            if 0 <= offset < len(data.STORY) * 3:
                mi, di = divmod(offset, 3)
                self.story_cleared[di] |= 1 << mi
        self.sent_locations |= set(self.checked_locations)
        self.primed = True
        note("Restored progress from the server: %d checks already recorded.",
                    len(self.checked_locations))

    def collect_trophy_checks(self, profile: int) -> typing.Set[int]:
        """Arcade and challenge checks, read from the records the gate never touches."""
        found = set()
        for game_index in range(mm.TROPHY_COUNT):
            rec = profile + mm.TROPHY_OFF + game_index * mm.TROPHY_STRIDE
            tier = u32(rec + mm.TROPHY_TIER)
            if not tier:
                continue
            ap_index = GAME_TO_AP[game_index]          # memory order -> location-id order
            # tiers are cumulative: a Gold also earns Bronze and Silver
            for ti in range(min(tier, len(data.TROPHY_TIERS))):
                found.add(TROPHY_BASE + ap_index * 4 + ti)
        return found

    def collect_story_checks(self, profile: int) -> typing.Set[int]:
        """Story checks. Detection lives in gamestate so the dry run cannot drift."""
        for mission_index, difficulty, won in gamestate.poll_story_clears(
                profile, self.level_completions):
            if not won:
                note("%s ended without the win flag -- treated as a quit, no check sent.",
                            data.STORY[mission_index])
                continue
            for di in range(difficulty + 1):        # difficulties are cumulative
                self.story_cleared[di] |= 1 << mission_index
        found = set()
        for di in range(3):
            for mi in range(len(data.STORY)):
                if self.story_cleared[di] >> mi & 1:
                    found.add(STORY_BASE + mi * 3 + di)
        return found

    def collect_objective_checks(self) -> typing.Set[int]:
        """Objectives seen to complete in the loaded level, plus a clear-derived floor.

        Polled rather than inferred from a mission clear: the logic lets an objective
        be completed without finishing the level. gamestate.ObjectiveWatcher only fires
        on a watched transition into complete, so a stale table cannot invent checks.

        The clear-derived part is a floor for anything completed while the client was
        not watching -- an objective primary at a cleared difficulty was necessarily done.
        """
        if not self.slot_data.get("objective_checks", True):
            return set()
        newly, _ = self.objectives.poll()
        self.objective_hits |= newly
        found = set(self.objective_hits)
        for mission_index, entries in gamestate.OBJECTIVES_BY_MISSION.items():
            cleared = 0
            for di in range(3):
                if self.story_cleared[di] >> mission_index & 1:
                    cleared |= 1 << di
            if cleared:
                for loc_id, mask in entries:
                    if mask & cleared:
                        found.add(loc_id)
        return found

    # ---- writing the gate ------------------------------------------------
    def apply_gate(self, profile: int):
        """Re-assert AP's lock state over the game's own progression.

        This OVERWRITES the profile's story and medal state to match what Archipelago
        has granted, which is the point -- but it means any progress the profile had
        outside this seed is replaced, and the profile is what gets written to the
        memory card. --read-only skips it.
        """
        if self.read_only:
            return
        if not self.warned_about_writing:
            self.warned_about_writing = True
            note("Applying Archipelago lock state to profile %r.", self.profile_name)
        g = profile + mm.GAMEDATA_OFF

        # The blanket overrides bypass the per-item gate entirely; AP needs them off.
        for i in range(3):
            if dme.read_byte(UNLOCK_ALL_OVERRIDE + i):
                dme.write_bytes(UNLOCK_ALL_OVERRIDE + i, b"\0")

        # Story: mission N is playable when story bit (N-1) is set for that
        # difficulty. Mission 0 is always open, so it needs no bit.
        granted = self.granted_missions()
        story_mask = 0
        for mi in granted:
            if mi > 0:
                story_mask |= 1 << (mi - 1)
            elif self.story_patched:
                # a patched image gates the first mission on this spare bit; a stock
                # image ignores it and leaves that mission permanently selectable
                story_mask |= 1 << mm.FIRST_MISSION_STORY_BIT
        # A difficulty above the seed's chosen maximum is deliberately left greyed
        # out: it awards no checks, so offering it would only invite a player who
        # picked Normal to wonder why Hard exists. The game requires the previous
        # mission cleared at the SAME difficulty, so an empty word does exactly that.
        #
        # Excluded difficulties are zeroed rather than skipped -- a profile carrying
        # progress from before the seed would otherwise leave them selectable, and
        # which difficulties are offered should depend on the seed, not the save.
        for di in range(3):
            w32(g + 0xBC8 + di * 4, story_mask if self.difficulty_enabled(di) else 0)

        # Arcade and challenge: an event is playable when every event before it in
        # its group carries a medal. Granting event E means marking everything
        # ahead of it in E's group. Bronze alone satisfies the gate.
        open_events = set()                       # GAME indices; the group table uses them
        for game_index in self.granted_events():
            for other in self.group_of(game_index):
                if other == game_index:
                    break
                open_events.add(other)
        words = [0, 0, 0, 0]
        for ei in open_events:
            words[ei >> 5] |= 1 << (ei & 31)
        for tier in range(1, 5):
            for w in range(4):
                address = g + 0xBD0 + tier * 0x10 + w * 4
                if address != profile + mm.PROFILE_TAG_OFF:      # the seed's claim on this profile
                    w32(address, words[w] if tier == 1 else 0)

        self.apply_intro_skip(profile)
        self.apply_weapons()
        self.apply_weapon_gating()
        self.scale_ammo()
        self.apply_preferences(profile)

        # On a patched image each match additionally has to be granted outright, which
        # is what makes a group's FIRST match lockable -- it has no predecessors, so the
        # medal marks above can never gate it.
        if self.arcade_patched:
            control = [0, 0, 0, 0]
            for game_index in self.granted_events():
                control[game_index >> 5] |= 1 << (game_index & 31)
            for w in range(mm.ARCADE_CONTROL_WORDS):
                w32(mm.ARCADE_CONTROL_ADDR + w * 4, control[w])

    def apply_intro_skip(self, profile: int):
        """Tell the patched disc whether to skip story intros, from the seed. A skipped
        intro is marked seen, so it still unlocks in the cinematics gallery."""
        skip = bool(self.slot_data.get("skip_intro_cutscenes"))
        gamestate.set_skip_intro(skip)
        if skip and self.skip_patched:
            mission = gamestate.current_mission()
            if mission is not None:
                gamestate.mark_intro_seen(profile, data.STORY_LEVEL_ID[mission])

    def apply_weapons(self):
        """Hand the seed's weapon shuffle to the game.

        The client only describes the shuffle -- a block in low memory, written once
        -- and the patched disc applies it as each level starts loading, which is the
        one moment that works: a level precaches the assets for whatever the tables
        say when the loader starts, changing them under the loader hangs it, and by
        the time a level is up the assets are already chosen. The same cave puts the
        native weapons back for Arcade and Challenge, which are scored content.
        """
        if not self.slot_data.get("weapon_shuffle"):
            return
        if not self.weapon_patched:
            if not self.reported_weapons:
                self.reported_weapons = True
                logger.warning("This image lacks the weapon patch, so the seed's weapon "
                               "shuffle cannot be applied. Close Dolphin and restart this "
                               "client: it patches a copy of the disc for you.")
            return
        rows = gamestate.WeaponBlock.rows(self.weapon_remap, self.weapon_remap_by_level)
        if self.weapons.write(rows) and not self.reported_weapons:
            self.reported_weapons = True
            changed = sum(len(remap) for remap in rows.values())
            note("Weapon shuffle handed to the game: %d remapped slot(s) across %d "
                        "missions; it takes effect as each mission loads.", changed, len(rows))

    def scale_ammo(self):
        """Under the shuffle, turn ammo gains into the shuffled-in gun's share (gamestate.ammo_scale)."""
        mission = gamestate.current_mission()
        pawn = gamestate.player_pawn() if mission is not None else None
        if (pawn is None or self.read_only or not self.slot_data.get("weapon_shuffle")
                or not self.weapon_patched):
            self.ammo_scaler.reset()
            self.ammo_scale_for = None
            return
        if self.ammo_scale_for is None or self.ammo_scale_for[0] != mission:
            level = data.STORY_LEVEL_ID[mission]
            remap = gamestate.WeaponBlock.rows(self.weapon_remap, self.weapon_remap_by_level).get(level, {})
            scale = gamestate.ammo_scale(remap, data.LEVEL_WEAPON_SLOTS.get(data.STORY[mission], ()))
            self.ammo_scale_for = (mission, scale)
            if scale:
                note("Ammo for %s: %s.", data.STORY[mission], ", ".join(
                    "type %#x x%.2f" % (kind, factor) if credit == kind else
                    "type %#x -> %#x x%.2f" % (kind, credit, factor)
                    for kind, (credit, factor) in sorted(scale.items())))
        if self.ammo_scale_for[1]:
            self.ammo_scaler.poll(pawn, self.ammo_scale_for[1])

    def allowed_weapon_slots(self) -> typing.Set[int]:
        """Slots the player may currently hold.

        A slot yields whatever the shuffle maps it to, so gating asks whether that
        TARGET has been granted, not the slot itself -- the PC client keys on the same
        thing. Baseline weapons (fists and the Temporal Uplink) are never gated: every
        mission starts with them and some objectives need the Uplink.
        """
        remap = self.current_remap()
        allowed = set(data.WEAPON_BASELINE)
        for slot in data.WEAPONS:
            yields = remap.get(slot, slot)
            if yields in self.granted_weapons or yields in data.WEAPON_BASELINE:
                allowed.add(slot)
        return allowed

    def apply_weapon_gating(self):
        """Strip weapons the seed has not handed out yet -- in story missions only, like the
        shuffle: Arcade and Challenge keep their fixed loadouts."""
        if not self.slot_data.get("weapon_gating"):
            return
        if gamestate.current_mission() is None:
            self.no_pawn_since = None
            return
        pawn = gamestate.player_pawn()
        if pawn is None:
            # Normal while the level loads and the player spawns. Still missing well after
            # that means the pawn guard is rejecting a real pawn -- which is exactly how
            # gating was once silently dead for a whole session -- so say so, once.
            now = time.monotonic()
            if self.no_pawn_since is None:
                self.no_pawn_since = now
            elif now - self.no_pawn_since > NO_PAWN_GRACE_SECONDS and not self.warned_no_pawn:
                self.warned_no_pawn = True
                logger.warning("Weapon gating is waiting: no player found in this mission after %d seconds. "
                               "If you are already playing, restart the client and report this.",
                               NO_PAWN_GRACE_SECONDS)
                note("No player pawn accepted; char list word %08x.", u32(mm.CHAR_PTR_LIST))
            return
        if self.warned_no_pawn:
            logger.info("Weapon gating is running again.")
        self.warned_no_pawn = False
        self.no_pawn_since = None
        allowed = self.allowed_weapon_slots()
        cleared, switched = gamestate.clear_ungranted_weapons(pawn, allowed)
        if cleared:
            # name every slot, every time: if a weapon the player SHOULD have vanishes,
            # this line is how we tell whether the client did it or the game did
            note("Weapon gating removed: %s   (allowed: %s)",
                        ", ".join(self.describe_slot(s) for s in cleared),
                        ", ".join(self.describe_slot(s) for s in sorted(allowed)))
        if switched:
            note("Weapon gating: was holding %s, switched to %s",
                        self.describe_slot(switched[0]), self.describe_slot(switched[1]))
        gamestate.clear_spent_ammo(pawn, allowed)

    def describe_slot(self, slot):
        """A slot as the player sees it: under a shuffle, the weapon it appears as."""
        name = data.WEAPONS.get(slot, "fists" if slot == 0 else "?")
        target = self.current_remap().get(slot, slot)
        if target != slot:
            return "%s (in the %s's place)" % (data.WEAPONS.get(target, "?"), name)
        return "%d %s" % (slot, name)

    def current_remap(self):
        """The slot -> slot map in force for the mission now loaded."""
        remap = self.weapon_remap
        if self.weapon_remap_by_level:
            remap = self.weapon_remap_by_level.get(gamestate.current_mission(), remap)
        return remap or {}

    def prefs_key(self):
        return "ts2gc_prefs_%s_%s" % (self.team, self.slot)

    def apply_preferences(self, profile: int):
        """Write the yaml's profile preferences, once per profile.

        A fresh profile defaults to Inverse Look on, Auto Lookahead on and Weapon
        Change "if new and best" -- the last of which fights weapon gating, since a
        pickup switches you to a weapon the client is about to take away. The seed
        says what a new profile should start with; anything the player changes
        afterwards is theirs, which is why the profile is remembered in the server's
        data storage once written, not re-asserted every session.
        """
        prefs = self.slot_data.get("preferences")
        if not prefs or self.read_only:
            return
        if self.prefs_key() not in self.stored_data:
            return                              # not heard back from the server yet
        done = self.stored_data[self.prefs_key()] or {}   # an unset key comes back as None
        if self.profile_name in done:
            return
        flags = u32(profile + mm.PROFILE_PREF_FLAGS)
        for bit, key in ((mm.PREF_INVERSE_LOOK, "inverse_look"),
                         (mm.PREF_AUTO_LOOKAHEAD, "auto_lookahead")):
            flags = (flags | bit) if prefs.get(key) else (flags & ~bit)
        change = int(prefs.get("weapon_change", 1))
        for base in (profile, mm.ACTIVE_PROFILE_COPY):
            w32(base + mm.PROFILE_PREF_FLAGS, flags)
            w32(base + mm.PROFILE_WEAPON_CHANGE, change)
        self.stored_data[self.prefs_key()] = dict(done, **{self.profile_name: True})
        asyncio.create_task(self.send_msgs([{
            "cmd": "Set", "key": self.prefs_key(), "default": {}, "want_reply": True,
            "operations": [{"operation": "update", "value": {self.profile_name: True}}]}]))
        names = {0: "always", 1: "never", 2: "best", 3: "if new", 4: "if new and best"}
        logger.info("Preferences written to profile %r: Inverse Look %s, Auto Lookahead %s, "
                    "Weapon Change %s. Change them in the game whenever you like; they will "
                    "not be written again.", self.profile_name,
                    "on" if prefs.get("inverse_look") else "off",
                    "on" if prefs.get("auto_lookahead") else "off", names.get(change, change))

    def difficulty_enabled(self, di: int) -> bool:
        """Whether this seed uses this story difficulty at all.

        slot_data's story_difficulty_checks is a maximum rather than a set of choices,
        so this is "at or below the seed's cap". Difficulties above it award no checks
        and are deliberately not offered in game.
        """
        allowed = self.slot_data.get("story_difficulty_checks")
        if not allowed:
            return True
        return data.STORY_DIFFICULTIES[di] in allowed

    _group_cache: typing.Optional[typing.List[typing.List[int]]] = None

    def group_of(self, event: int) -> typing.List[int]:
        """The arcade/challenge group an event belongs to, read from the game.

        Only cached once it reads back plausibly. A poll landing before the game has
        loaded sees zeros, and caching that would leave every event group-less for the
        rest of the session -- silently, since an empty group still returns something.
        """
        if TS2GCContext._group_cache is None:
            groups = []
            for gi in range(32):
                p = gamestate.GROUP_TABLE + gi * gamestate.GROUP_STRIDE
                count = u32(p + 4)
                if not 0 < count <= 8:
                    break
                groups.append([u32(p + 8 + k * 4) for k in range(count)])
            if len(groups) < 16:                    # 22 real groups; anything short is junk
                return [event]
            TS2GCContext._group_cache = groups
        for grp in TS2GCContext._group_cache:
            if event in grp:
                return grp
        return [event]

    async def shutdown(self):
        self.end_trap()
        # Only ever close a Dolphin this client started; one the player opened is theirs.
        if self.dolphin_process is not None and self.dolphin_process.poll() is None:
            logger.info("Closing the Dolphin instance this client started.")
            try:
                self.dolphin_process.terminate()
            except Exception:
                pass
        await super().shutdown()

    def log_gate(self):
        missions = sorted(self.granted_missions())
        logger.info("Story open: %s", ", ".join(data.STORY[mi] for mi in missions) or "none")
        need = self.slot_data.get("time_crystals_required", 0)
        if (data.FINAL_STORY_MISSION in self.granted_units and self.time_crystals < need
                and self.slot_data.get("story_content", True)):
            logger.info("%s stays locked until %d Time Crystal(s) arrive (%d so far).",
                        data.FINAL_STORY_MISSION, need, self.time_crystals)
        events = sorted(self.granted_events())
        if events:
            logger.info("Arcade/Challenge granted: %s",
                        ", ".join(data.TROPHY_TABLE_ORDER[e] for e in events))
        else:
            logger.info("Arcade/Challenge granted: none")
        if self.read_only:
            logger.info("(read-only: this state is NOT being written to the game)")

    # ---- DeathLink ---------------------------------------------------------
    def on_deathlink(self, data_: dict):
        """An incoming death. Several before the next chance to apply collapse into one."""
        super().on_deathlink(data_)
        self.death_pending = data_
        self.reported_kill_wait = False

    def watch_deaths(self, player: typing.Optional[gamestate.Player]):
        """Send our deaths, and apply incoming ones, on the alive -> dead edge.

        Leaving a level is not a death (no player, so not alive), and a respawn
        re-arms the edge. Story sends every death; Arcade and Challenge are
        deathmatches, so they send one per arcade_death_link_threshold deaths.
        """
        if "DeathLink" not in self.tags:
            self.was_alive = False
            return
        dead = player is not None and gamestate.is_dead(player)
        alive = player is not None and not dead
        if self.kill_sent_at is not None and time.monotonic() - self.kill_sent_at > KILL_SETTLE_SECONDS:
            self.kill_sent_at = None
        if self.was_alive and dead:
            if self.kill_sent_at is not None:
                self.kill_sent_at = None        # the death we were sent, not one to pass on
            elif gamestate.in_story():
                asyncio.create_task(self.send_death("%s bit the dust." % self.player_names.get(self.slot, "")))
            elif self.arcade_death_threshold > 0:
                self.arcade_deaths += 1
                if self.arcade_deaths >= self.arcade_death_threshold:
                    self.arcade_deaths = 0
                    asyncio.create_task(self.send_death(
                        "%s bit the dust one time too many." % self.player_names.get(self.slot, "")))
                else:
                    note("Arcade death %d of %d before a DeathLink goes out.",
                                self.arcade_deaths, self.arcade_death_threshold)
        self.was_alive = alive
        if self.death_pending is not None and alive and not self.read_only:
            if gamestate.kill_player(player):
                logger.info("DeathLink applied.")
                self.death_pending = None
                self.kill_sent_at = time.monotonic()
            elif not self.reported_kill_wait:
                self.reported_kill_wait = True
                logger.info("DeathLink waiting until you are on foot (movement state %d).",
                            u32(player.ctrl + mm.CTRL_STATE))

    # ---- bonus packs and traps --------------------------------------------
    def grant_key(self):
        return "ts2gc_grant_%s_%s" % (self.team, self.slot)

    def granted_counts(self) -> typing.Optional[dict]:
        """How many packs and traps were already applied, or None until the server answers."""
        if self.grant_key() not in self.stored_data:
            return None
        stored = self.stored_data[self.grant_key()] or {}
        packs = list(stored.get("packs") or [])[:3]
        traps = list(stored.get("traps") or [])[:len(TRAP_IDS)]
        return {"packs": packs + [0] * (3 - len(packs)), "traps": traps + [0] * (len(TRAP_IDS) - len(traps))}

    def save_granted(self, counts: dict):
        self.stored_data[self.grant_key()] = counts
        asyncio.create_task(self.send_msgs([{
            "cmd": "Set", "key": self.grant_key(), "default": {}, "want_reply": True,
            "operations": [{"operation": "replace", "value": counts}]}]))

    def apply_packs(self, player: typing.Optional[gamestate.Player], counts: dict) -> bool:
        """Refill on arrival in any level, or on the next level entry. Several of a kind
        waiting collapse into one refill, as on PC."""
        if player is None or gamestate.is_dead(player):
            return False
        changed = False
        for kind, (name, refill) in enumerate((("Health", gamestate.refill_health),
                                               ("Armor", gamestate.refill_armor),
                                               ("Ammo", gamestate.refill_ammo))):
            if counts["packs"][kind] < self.packs_received[kind]:
                refill(player)
                if refill is gamestate.refill_ammo:
                    self.ammo_scaler.rebase(player.pawn)
                counts["packs"][kind] = self.packs_received[kind]
                changed = True
                logger.info("%s Pack applied.", name)
        return changed

    def apply_traps(self, player: typing.Optional[gamestate.Player], counts: dict) -> bool:
        """One trap at a time, TRAP_SECONDS each, in Story missions only.

        Serial on purpose: Big Head and Small Head together crash the game. A trap
        received elsewhere waits for the next story mission; leaving the mission ends
        the running one early, and it still counts as delivered.
        """
        in_story = player is not None and gamestate.current_mission() is not None
        changed = self.skip_retired_traps(counts)
        if self.trap is not None:
            index, bit, ends, before = self.trap
            if time.monotonic() >= ends or not in_story:
                gamestate.cheat_off(bit, before)
                self.trap = None
                counts["traps"][index] += 1
                logger.info("%s is over.", data.TRAP_ID_ORDER[index])
                return True
            gamestate.cheat_on(bit)
            return changed
        if not in_story:
            return changed
        for index, name in enumerate(data.TRAP_ID_ORDER):
            if name not in data.RETIRED_TRAPS and counts["traps"][index] < self.traps_received[index]:
                bit = data.TRAP_CHEATS[name]
                self.trap = (index, bit, time.monotonic() + TRAP_SECONDS, gamestate.cheat_state(bit))
                gamestate.cheat_on(bit)
                logger.info("%s! (%d seconds)", name, TRAP_SECONDS)
                break
        return changed

    def skip_retired_traps(self, counts: dict) -> bool:
        """A seed made before a trap was dropped may still send it: count it delivered, once, and do nothing."""
        changed = False
        for index, name in enumerate(data.TRAP_ID_ORDER):
            if name in data.RETIRED_TRAPS and counts["traps"][index] < self.traps_received[index]:
                counts["traps"][index] = self.traps_received[index]
                logger.info("%s no longer exists in this version and is skipped.", name)
                changed = True
        return changed

    def apply_player_items(self):
        """DeathLink, packs and traps: everything that acts on the player in a level."""
        player = gamestate.player()
        self.watch_deaths(player)
        if self.read_only:
            return
        counts = self.granted_counts()
        if counts is None:
            return                              # not heard back from the server yet
        if self.apply_packs(player, counts) | self.apply_traps(player, counts):
            self.save_granted(counts)

    def end_trap(self):
        """Take a running trap's cheat away, e.g. when the client closes."""
        if self.trap is not None and self.hooked:
            try:
                gamestate.cheat_off(self.trap[1], self.trap[3])
            except Exception:
                pass
        self.trap = None

    # ---- goal ------------------------------------------------------------
    def goal_met(self) -> bool:
        if not self.slot_data.get("story_content", True):
            # No Story: win on a share of the kept Arcade / Challenge trophy checks. goal_trophy_events
            # are AP indices (location-id order), which is exactly what the location id encodes.
            need = self.slot_data.get("trophy_goal_checks", 0)
            eis = set(self.slot_data.get("goal_trophy_events") or [])
            done = sum(1 for loc in self.sent_locations
                       if TROPHY_BASE <= loc < TROPHY_BASE + len(AP_EVENT_NAMES) * 4
                       and (loc - TROPHY_BASE) // 4 in eis)
            return need > 0 and done >= need
        required = self.slot_data.get("time_crystals_required", 0)
        if self.time_crystals < required:
            return False
        final = len(data.STORY) - 1
        goal_difficulty = self.slot_data.get("goal_difficulty", "Easy")
        di = data.STORY_DIFFICULTIES.index(goal_difficulty) if goal_difficulty in data.STORY_DIFFICULTIES else 0
        return bool(self.story_cleared[di] >> final & 1)


async def game_loop(ctx: TS2GCContext):
    logger.info("Waiting for Dolphin running %s...", mm.GAME_ID)
    reported_errors: typing.Set[str] = set()
    while not ctx.exit_event.is_set():
        await asyncio.sleep(POLL_INTERVAL)
        try:
            if not ctx.ensure_hooked():
                continue
            if ctx.server is None or ctx.slot is None:
                continue
            if not ctx.primed:
                ctx.seed_from_server()
            profile = ctx.profile_base()
            if profile is None:
                continue
            if not await ctx.check_profile(profile):
                continue
            ctx.story_patched = gamestate.story_patch_present()
            ctx.arcade_patched = gamestate.arcade_patch_present()
            ctx.weapon_patched = gamestate.weapon_patch_present()
            ctx.skip_patched = gamestate.skip_intro_patch_present()
            # A story map outside Story mode is Arcade (they share level ids). Said
            # once per level so a wrong mode value would show up in the first test.
            level = gamestate.MISSION_OF_LEVEL.get(u32(mm.CURRENT_LEVEL_ADDR))
            mode = (gamestate.game_mode(), u32(mm.LOAD_FLAGS_ADDR))
            if (level, mode) != ctx.reported_mode:
                ctx.reported_mode = (level, mode)
                if level is not None:
                    note("%s loaded in game mode %s, flags %08x%s.", data.STORY[level], mode[0], mode[1],
                         " (Story)" if gamestate.in_story() else " -- not Story, so "
                         "no story checks, gating or shuffle here")
            if not ctx.reported_patch_state:
                ctx.reported_patch_state = True
                if ctx.story_patched and ctx.arcade_patched and ctx.weapon_patched and ctx.skip_patched:
                    note("Fully patched image: every mission and match can be locked.")
                else:
                    missing = []
                    if not ctx.story_patched:
                        missing.append("%s is always selectable" % data.STORY[0])
                    if not ctx.arcade_patched:
                        missing.append("the first match of each arcade/challenge group is "
                                       "always selectable")
                    if not ctx.weapon_patched:
                        missing.append("weapons cannot be shuffled")
                    if not ctx.skip_patched:
                        missing.append("intro cutscenes cannot be skipped")
                    logger.warning("Partly stock image -- %s. Close Dolphin and restart this client: "
                                   "it patches a copy of the disc for you.", "; and ".join(missing))

            # Order matters: read what the game recorded BEFORE overwriting the
            # arrays the gate shares with it.
            found = (ctx.collect_trophy_checks(profile)
                     | ctx.collect_story_checks(profile)
                     | ctx.collect_objective_checks())
            new = found - ctx.sent_locations
            if new:
                ctx.sent_locations |= new
                await ctx.send_msgs([{"cmd": "LocationChecks", "locations": sorted(new)}])
                logger.info("Sent %d new check(s): %s", len(new),
                            ", ".join(LOCATION_NAME.get(loc, str(loc)) for loc in sorted(new)))

            ctx.apply_gate(profile)
            ctx.apply_player_items()

            if not ctx.goal_reached and ctx.goal_met():
                ctx.goal_reached = True
                await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                logger.info("Goal complete!")
        except Exception as exc:
            # A crashed emulator must not kill the client, but a real bug must not hide
            # either: report each distinct failure once, loudly. An earlier NameError in
            # this loop silently stopped every write and looked like "the gate does not
            # work".
            key = repr(exc)
            if key not in reported_errors:
                reported_errors.add(key)
                logger.exception("Poll failed (further identical failures will be quiet): %s", exc)
            ctx.hooked = False


# ---------------------------------------------------------------------------
# Launching the game
#
# The client owns the Dolphin process: pick the executable and the original disc
# once, and every later session just starts. The patch carries no seed data -- it
# is the same code changes every time -- so one patched copy, "<name> (AP)", is
# written beside the original and reused for every seed.
# ---------------------------------------------------------------------------

def _settings():
    try:
        from settings import get_settings
        return get_settings().timesplitters2_options
    except Exception:
        return None


def _save_settings():
    """Write host.yaml now. Archipelago only adds the keys a new version brings when it saves on
    exit, so the client saves at start too: an upgrade's new options show up straight away."""
    try:
        from settings import get_settings
        get_settings().save()
    except Exception as exc:
        note("could not save settings: %r", exc)


# Every patched image carries the optional mouse-look hook too: it does nothing unless the
# mouse driver is running, so one image serves everyone (mouse_look in host.yaml decides).
ALL_PATCHES = patch_iso.PATCHES + [p for p in patch_iso.MOUSE_PATCHES if p not in patch_iso.PATCHES]


def ensure_patched(image_path):
    """Return a fully patched image to run, patching a copy if need be.

    An unpatched image still runs and still plays, it just quietly leaves 101
    locations reachable that the seed's logic assumes are not, and cannot shuffle
    weapons. A patched copy from an earlier version is updated too, since the set
    of patches has grown over time.
    """

    def missing_patches(path):
        image = patch_iso.Image(path)
        try:
            dol = patch_iso.dol_offset(image)
            # the mouse cave entry carries a fifth element (earlier versions of the site)
            return [description for ram, original, patched, description, *_ in ALL_PATCHES
                    if patch_iso.words_at(image, patch_iso.ram_to_disc(image, dol, ram), len(original)) != patched]
        finally:
            image.close()

    try:
        missing = missing_patches(image_path)
    except SystemExit as exc:
        logger.error("%s", exc)
        return None
    if not missing:
        note("Image is fully patched.")
        return image_path
    stem, ext = os.path.splitext(image_path)
    # the image chosen may itself be a copy this client wrote earlier ("... (AP)"): update it
    # rather than writing "(AP) (AP)" beside it
    patched_path = image_path if stem.endswith(" (AP)") else stem + " (AP)" + ext
    if os.path.exists(patched_path):
        try:
            if not missing_patches(patched_path):
                note("Using the patched image beside it: %s", os.path.basename(patched_path))
                return patched_path
            logger.info("%s is from an earlier version; updating it.", os.path.basename(patched_path))
            patch_iso.apply(patched_path, patched_path, patches=ALL_PATCHES, in_place=True)
            return patched_path
        except (SystemExit, OSError) as exc:
            if patched_path == image_path:
                logger.error("Patching failed: %s -- if Dolphin is still running on it, close it and retry.", exc)
                return None
            logger.info("Could not update it (%s); writing a fresh copy.", exc)
    logger.info("Writing a patched copy to %s (%d patch(es))", os.path.basename(patched_path), len(ALL_PATCHES))
    try:
        patch_iso.apply(image_path, patched_path, patches=ALL_PATCHES)
    except (SystemExit, OSError) as exc:
        logger.error("Patching failed: %s -- if Dolphin is still running on the old copy, close it and retry.", exc)
        return None
    return patched_path


def _ask_path(title, filetypes):
    """A file picker, for the first run (the choice is saved to host.yaml)."""
    try:
        return Utils.open_filename(title, filetypes) or None
    except Exception as exc:
        logger.debug("no file dialog: %r", exc)
        return None


async def launch(ctx, dolphin=None, game=None):
    """Ask for anything missing, prepare the disc copy off the main thread (the window stays up and
    shows the progress), then start Dolphin on it."""
    if ctx.launching:
        logger.info("Already launching; wait for Dolphin to start.")
        return
    if ctx.exit_event.is_set():
        return
    chosen = choose_launch(dolphin, game)
    if chosen is None:
        return
    settings, dolphin, game = chosen
    ctx.launching = True
    try:
        patched = await asyncio.to_thread(prepare_disc, ctx, settings, dolphin, game)
    except Exception as exc:
        logger.exception("Preparing the disc failed: %s", exc)
        patched = None
    finally:
        ctx.launching = False
        ctx.hide_prep()
    # the window may have been closed while the disc was prepared: then there is no one to play
    if patched is not None and not ctx.exit_event.is_set():
        proc = start_game(ctx, settings, dolphin, patched)
        if proc is not None:
            ctx.dolphin_process = proc
            ctx.dolphin_exit_reported = False


def choose_launch(dolphin=None, game=None):
    """Dolphin and the disc, asked for on the main thread (the file dialogs need it) before any slow
    work. Returns (settings, dolphin, game), or None if something was not chosen.

    The paths come from the command line, else host.yaml, else a file picker on the
    first run. host.yaml keeps the ORIGINAL disc; the patched copy lives beside it.
    """
    settings = _settings()
    dolphin = dolphin or (getattr(settings, "dolphin_path", None) if settings else None)
    game = game or (getattr(settings, "game_path", None) if settings else None)
    if not dolphin or not os.path.isfile(str(dolphin)):
        dolphin = _ask_path("Select Dolphin (Dolphin.exe)", [("Dolphin", [".exe"]), ("Any file", ["*"])])
    if not game or not os.path.isfile(str(game)):
        game = _ask_path("Select your TimeSplitters 2 (USA) disc image",
                         [("GameCube disc image", [".iso", ".gcm", ".ciso", ".rvz"]), ("Any file", ["*"])])
    missing = []
    if not dolphin or not os.path.isfile(str(dolphin)):
        missing.append("Dolphin")
    if not game or not os.path.isfile(str(game)):
        missing.append("the disc image")
    if missing:
        logger.error("Not launching: %s not chosen. Restart the client to pick it, or set dolphin_path and "
                     "game_path under timesplitters2_options in host.yaml. To use a Dolphin you started "
                     "yourself, run the client with --no-launch.", " and ".join(missing))
        return None
    if str(game).lower().endswith(".rvz"):
        logger.error("RVZ images cannot be patched. Convert it to ISO in Dolphin (right-click the game, "
                     "Convert File...) and choose that instead.")
        return None
    if settings is not None:
        try:
            settings.dolphin_path = str(dolphin)
            settings.game_path = str(game)          # the original: the patched copy is kept beside it
        except Exception as exc:
            note("could not set the paths: %r", exc)
        ask_music_folder(settings)
        _save_settings()
    return settings, str(dolphin), str(game)


def music_mode(settings):
    mode = str(getattr(settings, "music_shuffle", "off") or "off") if settings is not None else "off"
    if mode not in music.MODES:
        logger.warning("Unknown music_shuffle %r in host.yaml; using off. Choices: %s", mode, ", ".join(music.MODES))
        mode = "off"
    return mode


def ask_music_folder(settings):
    """Your songs' folder, asked for the first time a mode needs one, and remembered as music_folder."""
    if music_mode(settings) not in ("game_and_custom", "custom_only"):
        return
    if os.path.exists(str(getattr(settings, "music_folder", "") or "")):
        return
    try:
        folder = Utils.open_directory("Select the folder with your songs for the music shuffle") or ""
    except Exception as exc:
        note("no folder dialog: %r", exc)
        folder = ""
    if folder:
        try:
            settings.music_folder = folder
        except Exception as exc:
            note("could not set the music folder: %r", exc)


def prepare_disc(ctx, settings, dolphin, game):
    """The slow part of a launch, run off the main thread so the window stays up: patch the copy.
    Returns the image to boot, or None. Progress goes through ctx.show_prep."""
    ctx.show_prep("Patching your disc copy...")
    patched = ensure_patched(game)
    if patched is None:
        logger.error("Not launching without a fully patched image.")
        return None
    apply_music(ctx, settings, str(patched))
    return str(patched)


def apply_music(ctx, settings, image):
    """This launch's music (host.yaml music_shuffle), written into the patched copy before boot."""
    mode = music_mode(settings)
    if mode in ("game_and_custom", "custom_only"):
        logger.info("Preparing your music (new songs are converted once; this can take a moment)...")

    def progress(done, total, song):
        if done < total:
            ctx.show_prep("Converting your songs: %d of %d done%s" % (done, total, " (%s)" % song if song else ""),
                          done, total)
        else:
            ctx.show_prep("Writing the music into your disc copy...", total, total)

    if mode != "off":
        ctx.show_prep("Writing the music into your disc copy...")
    try:
        music.apply(image, Utils.cache_path("timesplitters2", "music"), mode=mode,
                    folder=str(getattr(settings, "music_folder", "") or ""),
                    seed=int(getattr(settings, "music_seed", 0) or 0),
                    ffmpeg_path=str(getattr(settings, "ffmpeg_path", "") or ""),
                    log=logger.info, warn=logger.warning, image_factory=patch_iso.Image, progress=progress)
    except Exception as exc:
        logger.warning("Music skipped: %s", exc)


def start_game(ctx, settings, dolphin, patched):
    logger.info("Launching %s", os.path.basename(patched))
    try:
        proc = subprocess.Popen([dolphin, "--exec=%s" % patched, "--batch"])
    except Exception as exc:
        logger.error("Could not start Dolphin: %s", exc)
        return None
    if ctx.mouse_process is None and (settings is None or bool(getattr(settings, "mouse_look", True))):
        ctx.mouse_process = start_mouse()
    return proc


def start_mouse():
    """The mouse driver, in its own process: it waits for the game, then feeds mouse look
    into it (F8 toggles capture, F6/F7 sensitivity). Windows only."""
    if sys.platform != "win32":
        logger.info("Mouse look needs Windows; skipped.")
        return None
    try:
        import multiprocessing
        from .. import mouse_driver
        proc = multiprocessing.Process(target=mouse_driver.main, args=(["--wait", "--stick-y", "2"],),
                                       name="TS2GCMouse", daemon=True)
        proc.start()
        logger.info("Mouse look on: click into the Dolphin window. F8 turns capture on/off, F6/F7 "
                    "lower/raise the sensitivity. (Turn it off with mouse_look in host.yaml.)")
        return proc
    except Exception as exc:
        logger.warning("Mouse look could not start: %s", exc)
        return None


def main(*launch_args: str):
    Utils.init_logging("TS2Client")

    async def run():
        parser = get_base_parser()
        parser.add_argument("--name", default=None,
                            help="slot name to connect as, so the prompt can be skipped")
        parser.add_argument("--no-launch", action="store_true",
                            help="do not start Dolphin; attach to one that is already running")
        parser.add_argument("--read-only", action="store_true",
                            help="send checks but never write to the game, so a live session can "
                                 "be verified without altering the profile")
        parser.add_argument("--dolphin", default=None,
                            help="Dolphin executable (remembered in host.yaml)")
        parser.add_argument("--game", default=None,
                            help="the original TimeSplitters 2 disc image; a patched copy is written "
                                 "beside it (remembered in host.yaml)")
        args = parser.parse_args(launch_args)
        _save_settings()
        ctx = TS2GCContext(args.connect, args.password)
        if args.name:
            ctx.auth = args.name
        ctx.read_only = args.read_only
        if ctx.read_only:
            logger.info("Read-only: checks will be sent, but nothing will be written to the game.")
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")
        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()
        loop = asyncio.create_task(game_loop(ctx), name="GameLoop")
        if not args.no_launch:
            await asyncio.sleep(0.5)            # let the window draw before the dialogs and the slow work
            await launch(ctx, args.dolphin, args.game)
        await ctx.exit_event.wait()
        loop.cancel()
        if ctx.mouse_process is not None and ctx.mouse_process.is_alive():
            ctx.mouse_process.terminate()
        await ctx.shutdown()

    import colorama
    colorama.init()
    asyncio.run(run())
    colorama.deinit()
