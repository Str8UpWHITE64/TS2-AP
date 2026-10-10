from dataclasses import dataclass

from Options import Choice, DeathLink, DefaultOnToggle, OptionSet, PerGameCommonOptions, Range, StartInventoryPool, Toggle


class GameModes(OptionSet):
    """Which of TimeSplitters 2's three modes produce checks. Remove any you would rather not play.

    Story     - the 10 story missions, their per-difficulty completions, and their objectives.
    Arcade    - the 45 Arcade League matches, one check per trophy tier.
    Challenge - the 21 Challenges, one check per trophy tier.

    At least one must be kept. Dropping a mode removes its checks and its unlock items; what they held becomes
    filler, so a shorter list mainly makes for a smaller, denser seed.

    THE GOAL FOLLOWS THIS LIST. With Story kept, you win by clearing Space Station on your Max Story Difficulty once
    enough Time Crystals have arrived from the multiworld. With Story removed there is no Space Station to clear, so
    you instead win by completing Trophy Goal Percentage of whichever Arcade / Challenge checks you kept, no Time
    Crystals are placed, and Weapon Gating / Weapon Shuffle are forced off (they only ever affect story missions)."""
    display_name = "Game Modes"
    valid_keys = ("Story", "Arcade", "Challenge")
    default = frozenset(valid_keys)


class TrophyGoalPercentage(Range):
    """The share of your Arcade / Challenge checks you must complete to win when Story is NOT in Game Modes.

    Counted over the trophy checks the seed actually has, so it follows Game Modes: keep only Challenge and it is a
    percentage of the Challenge checks alone. Ignored whenever Story is kept, since that wins on Space Station."""
    range_start = 10
    range_end = 100
    display_name = "Trophy Goal Percentage"
    default = 90


class StoryDifficulty(Choice):
    """The MAXIMUM story difficulty you intend to play. Each story mission and its objectives produce checks for every
    difficulty up to and including this (so Normal = Easy + Normal checks; objectives that only exist above it become
    filler-only). The GOAL triggers when you clear Space Station on THIS difficulty. (Story modes only.)"""
    display_name = "Max Story Difficulty"
    option_easy = 0
    option_normal = 1
    option_hard = 2
    default = 1   # Normal


class TrophyTier(Choice):
    """The MAXIMUM trophy tier you intend to earn on Arcade matches + Challenges. Each event produces checks for every
    tier up to and including this (so Gold = Bronze + Silver + Gold checks; a single Gold clear fires all three, since
    trophies are cumulative in the save)."""
    display_name = "Max Trophy Tier"
    option_bronze = 0
    option_silver = 1
    option_gold = 2
    option_platinum = 3
    default = 2   # Gold


class TimeCrystalsRequired(Range):
    """How many Time Crystals you must collect before the final mission (Space Station) unlocks.

    Time Crystals are ITEMS sent to you from the multiworld -- they are scattered among all players' checks, so you
    receive them as other people play. Until this many have arrived, Space Station stays locked. Auto-clamped down if
    the seed has too few free item slots. Ignored when Story is not in Game Modes (no Space Station to unlock)."""
    display_name = "Time Crystals Required to Finish"
    range_start = 0
    range_end = 60
    default = 8


class TimeCrystalsExtra(Range):
    """How many EXTRA Time Crystals are placed beyond the required count (padding, so collecting them isn't all
    mandatory). Total placed = Required + Extra, auto-clamped to the free item slots -- so 'required' can never exceed
    what's actually placed."""
    display_name = "Extra Time Crystals"
    range_start = 0
    range_end = 30
    default = 4


class StartingUnlocksPerCategory(Range):
    """Starting unlock items granted at the top of each active category (story / arcade / challenge) so the seed
    is enterable from the start."""
    display_name = "Starting Unlocks Per Category"
    range_start = 1
    range_end = 3
    default = 1


class BonusHealthPacks(Range):
    """One-time Health Packs placed in the pool. Receiving one fully restores your health -- immediately if you're in
    a mission/challenge, otherwise on your next mission entry. Replaces junk filler (Banana)."""
    display_name = "Health Packs"
    range_start = 0
    range_end = 30
    default = 3


class BonusArmorPacks(Range):
    """One-time Armor Packs. Receiving one fully restores your armor (immediately in a mission, else on next entry).
    Replaces junk filler."""
    display_name = "Armor Packs"
    range_start = 0
    range_end = 30
    default = 3


class BonusAmmoPacks(Range):
    """One-time Ammo Packs. Receiving one fully refills your ammo (immediately in a mission, else on next entry).
    Replaces junk filler."""
    display_name = "Ammo Packs"
    range_start = 0
    range_end = 30
    default = 3


class TrapCount(Range):
    """How many Trap items to place. Each trap, when received during a STORY mission, switches on a disruptive TS2
    cheat (big heads, rotating heads, cardboard characters, ...) for 30 seconds, one trap at a time. Traps received
    outside a story mission wait for the next one. Drawn randomly from the named trap set. Replaces junk filler;
    auto-trimmed if there aren't enough free filler slots."""
    display_name = "Trap Count"
    range_start = 0
    range_end = 60
    default = 8


class WeaponGating(Toggle):
    """Weapons as items: you can only hold weapons you've received as AP items. Anything else you pick up, or start a
    mission with, is taken away again. Every weapon is an item, objective tools (ElectroTool, Camera, Gun Powder)
    included; only your fists, the Temporal Uplink and the Fire Extinguisher are always yours. The logic accounts for
    each mission's objectives, so a mission is only expected of you once you hold what it needs.

    STORY MISSIONS ONLY -- Arcade and Challenge always give their normal fixed loadouts. Forced OFF when Story is not
    in Game Modes (there would be nothing for it to affect)."""
    display_name = "Weapon Gating"


class WeaponShuffle(Toggle):
    """Weapon randomizer: the seed swaps what each weapon is in-game (e.g. every pistol becomes a brick, every sniper a
    rocket launcher). Applies to floor pickups, your starting loadout, AND enemy weapons (an enemy that would carry a
    pistol instead carries whatever pistols map to). Objective tools are never shuffled. Needs the patched disc, which
    the client writes for you.

    A shuffled-in gun draws on the ammo supply of the gun it replaces, so a level's ammo pickups fill it the way they
    would have filled the original.

    STORY MISSIONS ONLY -- Arcade and Challenge keep their normal weapons. Forced OFF when Story is not in Game Modes
    (there would be nothing for it to affect)."""
    display_name = "Weapon Shuffle"


class WeaponShuffleScope(Choice):
    """How far Weapon Shuffle is allowed to reach. Ignored unless Weapon Shuffle is on.

    completely_random - any shuffleable gun can become any other (mines still swap only with mines so a placeable
                        explosive stays placeable). The wildest option: a level can end up holding weapons that are
                        native to some other level.
    same_class        - a gun only becomes one of its own class: regulars with regulars, launchers with launchers,
                        mines with mines. The Flamethrower is the only member of its class, so it never changes.
    within_level      - each level reshuffles only its OWN pickups, so every level still offers exactly the weapons it
                        always did -- just from different pickups. The most conservative option, and it can never
                        strand a mission. (The same pickup slot can be a different gun in a different level.)"""
    display_name = "Weapon Shuffle Scope"
    option_completely_random = 0
    option_same_class = 1
    option_within_level = 2
    default = 0


class SkipIntroCutscenes(DefaultOnToggle):
    """Starting a story mission goes straight into it, without the intro cutscene. The cutscenes still unlock in
    the gallery. The cutscenes after Aztec and Space Station play as normal. Needs the patched disc, which the
    client writes for you."""
    display_name = "Skip Intro Cutscenes"


class DisableWeaponSway(Toggle):
    """Turns off the slow sway of your view and gun, both while standing still (breathing) and while moving.
    Recoil when firing is unchanged. Needs the patched disc, which the client writes for you."""
    display_name = "Disable Weapon Sway"


class InverseLook(Toggle):
    """GameCube preference: Inverse Look. Written to your profile the first time the client sees it, so a new
    profile starts the way you want without a trip through the Preferences screen. Anything you change in the game
    afterwards stays changed -- this is only the starting point. The game's own default is on."""
    display_name = "Inverse Look"
    default = 0


class AutoLookahead(Toggle):
    """GameCube preference: Auto Lookahead (the camera tilts on its own on slopes and stairs). Applied once, like
    Inverse Look. The game's own default is on."""
    display_name = "Auto Lookahead"
    default = 0


class WeaponChange(Choice):
    """GameCube preference: when picking up a weapon switches you to it. Applied once, like Inverse Look.

    The game's own default is if_new_and_best, which fights Weapon Gating: picking up a weapon you have not received
    switches you to it for an instant before the client takes it away again. never is the comfortable choice with
    gating on, and the default here."""
    display_name = "Weapon Change"
    option_always = 0
    option_never = 1
    option_best = 2
    option_if_new = 3
    option_if_new_and_best = 4
    default = 1


class ArcadeDeathLinkThreshold(Range):
    """DeathLink, ARCADE + CHALLENGE only: how many deaths there before one is sent out.

    Arcade matches are deathmatches -- you die constantly -- so sending every one would flood the multiworld. Deaths
    accumulate across matches and challenges for the whole session; each time the count reaches this number, one
    DeathLink is sent and the count resets. Deaths you RECEIVE never count toward it.

    Story missions are unaffected: every story death sends immediately. Incoming deaths always apply everywhere,
    Arcade and Challenge included. 1 = send every arcade/challenge death. 0 = never send from Arcade or Challenge
    (you still receive). Ignored unless DeathLink is on."""
    display_name = "Arcade DeathLink Threshold"
    range_start = 0
    range_end = 50
    default = 10


@dataclass
class TS2Options(PerGameCommonOptions):
    game_modes: GameModes
    trophy_goal_percentage: TrophyGoalPercentage
    story_difficulty: StoryDifficulty
    trophy_tier: TrophyTier
    time_crystals_required: TimeCrystalsRequired
    time_crystals_extra: TimeCrystalsExtra
    starting_unlocks_per_category: StartingUnlocksPerCategory
    bonus_health_packs: BonusHealthPacks
    bonus_armor_packs: BonusArmorPacks
    bonus_ammo_packs: BonusAmmoPacks
    trap_count: TrapCount
    weapon_gating: WeaponGating
    weapon_shuffle: WeaponShuffle
    weapon_shuffle_scope: WeaponShuffleScope
    skip_intro_cutscenes: SkipIntroCutscenes
    disable_weapon_sway: DisableWeaponSway
    inverse_look: InverseLook
    auto_lookahead: AutoLookahead
    weapon_change: WeaponChange
    death_link: DeathLink
    arcade_death_link_threshold: ArcadeDeathLinkThreshold
    start_inventory_from_pool: StartInventoryPool
