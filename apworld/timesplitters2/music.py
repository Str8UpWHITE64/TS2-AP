"""Music shuffle and custom songs, written into the patched disc copy before the game boots.

The game opens every track by name: the play call copies a path from its track table
(0x80337e80) and the music streamer at 0x8026141c hands it to the SDK's DVDOpen, which
looks it up in the in-RAM copy of the disc's file table (FST, pointer at 0x80000038). So a
shuffle is just FST entries pointing at other tracks' data, and a custom song is data written
into space a track this launch does not use (an unpicked or replaced game track, or -- on a
plain ISO -- the free space after the last file). Cutscene tracks are timed to their
cutscenes and are never touched.

Every song must be an .mss: Nintendo DSP ADPCM, stereo, 32000 Hz (see dspadpcm). Custom
songs are decoded, resampled and loudness-matched to the game's music with ffmpeg, then
encoded here, once each (cached by content).

Game tracks a launch overwrites are backed up first and restored on the next launch, so the
disc copy can always be put back as it was.
"""
import hashlib
import json
import os
import random
import shutil
import struct
import subprocess
import threading
import zipfile
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import dspadpcm

_M = "music/"
# Level, menu, challenge and mini-game music: shuffled among itself.
BGM = tuple(_M + n + ".mss" for n in (
    "ts1_tomb44fx_redone7july", "chinese44_july3rd_fx", "ts1_cyberden_44_fx2", "ts1_mansion44fx_redone",
    "ts1_village44fx_7july", "ts1_chemical_plant44fx3_7july", "ts1_planet44fx_7july", "ts1_docks44fx2_7july",
    "ts1_spaceways44fxgame_7july", "ts1_warzone44fx_7july", "ts1_buildingsite44fxgame_7july",
    "ts1_castle_redone44fx_7july", "ts1_compound44_july6", "ts2_siberia_boss32_fx", "ts1_spaceship44fx_game_7july",
    "ts1_streets44fx_7july", "ts1_mall44fxgame_7july", "ts1_bank_44_fx", "horror_tile_44", "industrial_tile_44_fx",
    "rocket_launch_site_44_fx3", "pause_44_fx", "ts2_tokyo_tileset44_fx", "ts2_tileset_guitar_44_fx2",
    "ts2_menu44_fx3", "ts2_aztec44game25june", "ts2_aztec_part2_44_fx", "ts2_pyramid_44_fx", "ts2_fecked_44_fx2",
    "ts2_neo_tokyo_44_fx", "ts2_western_44_fx", "ts2_spacestation_44_fx2", "ts2_spacestation_part2_44_fx2",
    "ts2_notre_dame_44_fx", "ts2_notre_dame_part_b_44_fx", "ts2_notredame_boss44_fx", "ts2_ts2trance_44_fx",
    "ts2_mobsters_44_fx", "ts2_mob_nightclub_44_fx", "ts2_military_44_fx", "ts2_atom_smasher_44_fx",
    "ts2_mexican_44_fx", "ts2_robot_world_main_44_fx", "ts2_robot_world_part2_44_fx", "ts2_robot_world_boss_44_fx",
    "ts2_hospital_44_fx", "ts2_martian_44_fx", "ts2_scrapyard_44_fx", "ts2_x2_44_fx3", "ts2_x2_part2_44_fx3",
    "ts2_siberia02_44_fx", "ts2_siberia_boss44_fx", "ts2_siberia02b_44_fx", "ts2_credits44_fx",
    "ts2_bigtop_44_fx2", "ts2_training_ground_game44_fx", "ts2_goteki_remix44_fx", "front_end_story44_fx",
    "ts2_aztec_challenge44", "ts2_mini_snakey44_fx", "ts2_mini_racer44_fx", "ts2_mini_thrust44_fx",
    "ts2_chicago_challenge44fx",
))
# The results-screen music after a win or a loss: its own group, filled from the "events" folder.
EVENTS = (_M + "ts2_gameover_win44_fx2.mss", _M + "ts2_game_over_lose_44_fx3.mss")
# Never changed: the cutscene tracks (cs_*, timed to their scenes), the two silent stubs
# intro/outro, and ts2_jungleambience44_01 (ambience rather than music).

MODES = ("off", "game_music", "game_and_custom", "custom_only")
AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".flac", ".wav", ".m4a", ".aac", ".opus", ".wma", ".aif", ".aiff", ".mss"}
TARGET_LUFS, TRUE_PEAK, LRA = -11.0, -1.0, 11.0     # the game's level music sits around -11 LUFS
CONVERT_VERSION = 1                                  # bump when the conversion changes: re-converts every song
CONVERT_WORKERS = max(1, min(8, (os.cpu_count() or 2) // 2))   # songs converted at once; half the cores
ALIGN = 32
DISC_SIZE = 1459978240                               # a GameCube disc


class Song:
    def __init__(self, name, group, path=None, region=None):
        self.name, self.group = name, group
        self.path = path                # converted custom song, or None for a game track
        self.region = region            # (offset, size) on the disc for a game track
        self.size = os.path.getsize(path) if path else region[1]

    @property
    def custom(self):
        return self.path is not None


# --- the disc ----------------------------------------------------------------------
def read_fst(image):
    off, size = struct.unpack(">II", image.read(0x424, 8))
    return off, bytearray(image.read(off, size))


def fst_entries(raw):
    """{path: entry index} for every file."""
    count = struct.unpack(">I", raw[8:12])[0]
    names = raw[count * 12:]
    out, stack = {}, [(count, "")]
    for i in range(1, count):
        while i >= stack[-1][0]:
            stack.pop()
        o = struct.unpack(">I", raw[i * 12:i * 12 + 4])[0] & 0xFFFFFF
        full = stack[-1][1] + names[o:names.index(b"\0", o)].decode("latin-1")
        if raw[i * 12]:
            stack.append((struct.unpack(">I", raw[i * 12 + 8:i * 12 + 12])[0], full + "/"))
        else:
            out[full] = i
    return out


def entry_region(raw, i):
    return struct.unpack(">II", raw[i * 12 + 4:i * 12 + 12])


def set_entry(raw, i, offset, size):
    raw[i * 12 + 4:i * 12 + 12] = struct.pack(">II", offset, size)


def free_tail(image, raw, entries):
    """[(offset, size)] of writable space after the last file: up to the end of the disc on a plain
    image; on a CISO only what is left of the last stored block (the rest is not in the file)."""
    end = max(sum(entry_region(raw, i)) for i in entries.values())
    if not image.ciso:
        return [(end, DISC_SIZE - end)] if end < DISC_SIZE else []
    block = end // image.block_size
    if end % image.block_size and block in image.physical:
        return [(end, (block + 1) * image.block_size - end)]
    return []


def write_span(image, offset, payload):
    """image.write() across any number of CISO blocks; every block touched must be stored."""
    if not image.ciso:
        image.write(offset, payload)
        return
    done = 0
    while done < len(payload):
        span = min(len(payload) - done, image.block_size - ((offset + done) % image.block_size))
        image.write(offset + done, payload[done:done + span])
        done += span


# --- songs -------------------------------------------------------------------------
def mss_ok(path):
    """True when the file is an .mss the game can stream as it is."""
    try:
        with open(path, "rb") as f:
            head = f.read(dspadpcm.MSS_HEADER)
        return dspadpcm.mss_info(head, os.path.getsize(path)) is not None
    except OSError:
        return False


def find_ffmpeg(configured=""):
    candidates = [configured] if configured else []
    candidates.append(shutil.which("ffmpeg") or "")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += [os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
                   r"C:\ffmpeg\bin\ffmpeg.exe",
                   os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin", "ffmpeg.exe"),
                   "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _ffmpeg(ffmpeg, args, binary=False):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if binary:
        return subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error"] + args,
                              capture_output=True, creationflags=flags)
    return subprocess.run([ffmpeg, "-hide_banner", "-nostats"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=flags)


def _loudnorm_stats(stderr):
    return json.loads(stderr[stderr.rfind("{"):stderr.rfind("}") + 1])


def _pcm_ffmpeg(ffmpeg, src):
    """The song as loudness-matched 32 kHz stereo samples (array('h'), interleaved), or None."""
    measure = _ffmpeg(ffmpeg, ["-i", src, "-vn", "-af", "loudnorm=I=%s:TP=%s:LRA=%s:print_format=json"
                               % (TARGET_LUFS, TRUE_PEAK, LRA), "-f", "null", "-"])
    try:
        m = _loudnorm_stats(measure.stderr)
        af = ("loudnorm=I=%s:TP=%s:LRA=%s:measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s:offset=%s"
              % (TARGET_LUFS, TRUE_PEAK, LRA, m["input_i"], m["input_tp"], m["input_lra"], m["input_thresh"],
                 m["target_offset"]))
    except (ValueError, KeyError):
        af = "loudnorm=I=%s:TP=%s:LRA=%s" % (TARGET_LUFS, TRUE_PEAK, LRA)
    done = _ffmpeg(ffmpeg, ["-i", src, "-vn", "-af", af, "-ac", "2", "-ar", str(dspadpcm.MSS_RATE),
                            "-f", "s16le", "-acodec", "pcm_s16le", "-"], binary=True)
    if done.returncode != 0 or len(done.stdout) < 4:
        return None
    pcm = array("h")
    pcm.frombytes(done.stdout[:len(done.stdout) // 4 * 4])
    if struct.pack("=h", 1) != b"\x01\x00":
        pcm.byteswap()                                   # s16le on a big-endian machine
    return pcm


def _riff_chunks(blob):
    """(channels, rate, bits, sample bytes) of a plain PCM .wav, or None.

    Read by hand: the installed Archipelago leaves the standard library's wave
    module out, so importing it stops the client from starting at all.
    """
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        return None
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(blob):
        tag, size = blob[pos:pos + 4], struct.unpack("<I", blob[pos + 4:pos + 8])[0]
        body = blob[pos + 8:pos + 8 + size]
        if tag == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif tag == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None or fmt[0] != 1:       # 1 = uncompressed PCM
        return None
    return fmt[1], fmt[2], fmt[5], data


def _pcm_wav(src):
    """A 16-bit, 32 kHz .wav read without ffmpeg (no loudness matching), or None."""
    try:
        with open(src, "rb") as f:
            blob = f.read()
    except OSError:
        return None
    fmt = _riff_chunks(blob)
    if fmt is None:
        return None
    channels, rate, bits, raw = fmt
    if bits != 16 or rate != dspadpcm.MSS_RATE or channels not in (1, 2):
        return None
    pcm = array("h")
    pcm.frombytes(raw[:len(raw) // 2 * 2])
    if struct.pack("=h", 1) != b"\x01\x00":
        pcm.byteswap()
    if channels == 1:
        stereo = array("h", bytes(4 * len(pcm)))
        stereo[0::2] = pcm
        stereo[1::2] = pcm
        pcm = stereo
    return pcm


def cache_key(src):
    h = hashlib.sha1()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(b"|v%d" % CONVERT_VERSION)
    return h.hexdigest()[:20]


def converted_path(src, cache_dir):
    return os.path.join(cache_dir, cache_key(src) + ".mss")


def convert(ffmpeg, src, cache_dir):
    """The song as a game-ready .mss in the cache; None if it cannot be made. Without ffmpeg only an
    .mss (used as it is) or a 16-bit 32 kHz .wav can be."""
    if src.lower().endswith(".mss"):
        return src if mss_ok(src) else None
    out = converted_path(src, cache_dir)
    if os.path.isfile(out) and mss_ok(out):
        return out
    pcm = _pcm_ffmpeg(ffmpeg, src) if ffmpeg else (_pcm_wav(src) if src.lower().endswith(".wav") else None)
    if not pcm or len(pcm) < 2 * dspadpcm.FRAME_SAMPLES:
        return None
    blob = dspadpcm.build_mss(pcm[0::2], pcm[1::2])
    tmp = "%s.%d.part" % (out, os.getpid())
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, out)
    return out


def _convert_job(ffmpeg, src, cache_dir):
    """convert() in a worker process; errors come back as None."""
    try:
        return convert(ffmpeg, src, cache_dir)
    except Exception:
        return None


def convert_all(ffmpeg, sources, cache_dir, progress=None, log=print):
    """{source: .mss path or None}. Encoding is Python, so new songs are converted in a pool of
    processes (threads would share one core); songs already in the cache are just looked up."""
    results, todo = {}, []
    for src in sources:
        if src.lower().endswith(".mss"):
            results[src] = src if mss_ok(src) else None
            continue
        try:
            out = converted_path(src, cache_dir)
        except OSError:
            results[src] = None
            continue
        if os.path.isfile(out) and mss_ok(out):
            results[src] = out
        else:
            todo.append(src)
    lock = threading.Lock()
    finished = [len(results)]

    def tick(src):
        if progress:
            with lock:
                finished[0] += 1
                progress(finished[0], len(sources), os.path.splitext(os.path.basename(src))[0])

    if progress and sources:
        progress(len(results), len(sources), "")
    pool = None
    # a worker process starts by loading Archipelago's worlds, which only pays off for a few songs
    if len(todo) >= 3 and CONVERT_WORKERS > 1:
        try:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            pool = ProcessPoolExecutor(max_workers=min(CONVERT_WORKERS, len(todo)),
                                       mp_context=multiprocessing.get_context("spawn"))
        except Exception as exc:
            log("music: no worker processes (%r); converting one song at a time" % (exc,))
    if pool is not None:
        pool_failed = False
        try:
            with pool:
                futures = {pool.submit(_convert_job, ffmpeg, src, cache_dir): src for src in todo}
                for fut in as_completed(futures):
                    try:
                        results[futures[fut]] = fut.result()
                        tick(futures[fut])
                    except Exception as exc:         # the worker died: done again below
                        if not pool_failed:
                            log("music: a worker process failed (%r); converting those songs here" % (exc,))
                        pool_failed = True
        except Exception as exc:
            log("music: the worker processes failed (%r); converting the rest one at a time" % (exc,))
        todo = [s for s in todo if s not in results]
    # no pool, or what it could not do: here, with ffmpeg still running alongside in threads
    with ThreadPoolExecutor(max_workers=max(1, min(CONVERT_WORKERS, len(todo)))) as threads:
        for src, out in zip(todo, threads.map(lambda s: _convert_job(ffmpeg, s, cache_dir), todo)):
            results[src] = out
            tick(src)
    return results


def collect(source, cache_dir):
    """[(group, display name, file)] from a folder or a .zip: files under an "events" folder
    are results-screen music, everything else is level/menu music."""
    if not source or not os.path.exists(source):
        return []
    found = []
    if os.path.isfile(source) and source.lower().endswith(".zip"):
        stamp = hashlib.sha1(("%s|%d|%d" % (os.path.abspath(source), os.path.getsize(source),
                                            int(os.path.getmtime(source)))).encode()).hexdigest()[:12]
        root = os.path.join(cache_dir, "zip_" + stamp)
        if not os.path.isdir(root):
            with zipfile.ZipFile(source) as z:
                z.extractall(root + ".part")
            os.replace(root + ".part", root)
        source = root
    for dirpath, _dirs, files in os.walk(source):
        parts = {p.lower() for p in os.path.relpath(dirpath, source).split(os.sep)}
        group = "events" if "events" in parts else "bgm"
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                found.append((group, os.path.splitext(f)[0], os.path.join(dirpath, f)))
    return sorted(found, key=lambda t: (t[0], t[2].lower()))


# --- the plan ----------------------------------------------------------------------
def plan(mode, rng, slots, game, custom):
    """{slot name: Song} for one group. `game` maps slot name -> its own Song."""
    if mode == "game_music" or not custom:
        picks = [game[n] for n in slots]
    elif mode == "game_and_custom":
        picks = rng.sample([game[n] for n in slots] + list(custom), len(slots))
    else:                                               # custom_only
        chosen = rng.sample(list(custom), min(len(custom), len(slots)))
        keep = rng.sample(slots, len(slots) - len(chosen))
        picks = chosen + [game[n] for n in keep]
    rng.shuffle(picks)
    return dict(zip(slots, picks))


def place(items, extents):
    """First-fit decreasing, ALIGN-aligned: ({item: offset}, [items that did not fit]). Items need
    a .size."""
    free = sorted(([o, s] for o, s in extents if s > 0), key=lambda e: e[0])
    placed, left = {}, []
    for item in sorted(items, key=lambda s: -s.size):
        for ext in free:
            start = (ext[0] + ALIGN - 1) & ~(ALIGN - 1)
            pad = start - ext[0]
            if ext[1] - pad >= item.size:
                placed[item] = start
                ext[0], ext[1] = start + item.size, ext[1] - pad - item.size
                break
        else:
            left.append(item)
    return placed, left


def subtract(extents, used):
    """`extents` with every range in `used` cut out."""
    out = []
    for o, s in extents:
        pieces = [(o, o + s)]
        for uo, us in used:
            nxt = []
            for a, b in pieces:
                if uo + us <= a or b <= uo:
                    nxt.append((a, b))
                else:
                    if a < uo:
                        nxt.append((a, uo))
                    if uo + us < b:
                        nxt.append((uo + us, b))
            pieces = nxt
        out += [(a, b - a) for a, b in pieces if b > a]
    return out


# --- applying it -------------------------------------------------------------------
# State per disc copy (in Archipelago's cache): the copy's original file table, a backup of every
# range a launch overwrote, and manifest.json: {"fst_sha1": the table written last, "fst_prev": the one
# before it (so a crash while writing the table is still recognised), "music": [[offset, size], ...] the
# ranges backed up}.
def state_dir(cache_root, image_path):
    key = hashlib.sha1(os.path.abspath(image_path).lower().encode()).hexdigest()[:16]
    return os.path.join(cache_root, "disc_" + key)


def _backup_file(state, off, size):
    return os.path.join(state, "backup", "%08x_%d.bin" % (off, size))


def back_up(image, state, ranges):
    os.makedirs(os.path.join(state, "backup"), exist_ok=True)
    for off, size in ranges:
        path = _backup_file(state, off, size)
        if os.path.isfile(path) and os.path.getsize(path) == size:
            continue
        with open(path + ".part", "wb") as f:
            done = 0
            while done < size:
                n = min(16 << 20, size - done)
                f.write(image.read(off + done, n))
                done += n
        os.replace(path + ".part", path)


def restore(image, state, ranges, warn):
    lost = 0
    for off, size in ranges:
        path = _backup_file(state, off, size)
        if not (os.path.isfile(path) and os.path.getsize(path) == size):
            lost += 1
            continue
        with open(path, "rb") as f:
            done = 0
            while done < size:
                chunk = f.read(16 << 20)
                write_span(image, off + done, chunk)
                done += len(chunk)
    if lost:
        warn("%d part(s) of the disc copy could not be restored. Delete the \"(AP)\" copy and restart the client "
             "to have it written fresh." % lost)


def apply(image_path, cache_root, mode="off", folder="", seed=0, ffmpeg_path="", log=print, warn=print,
          image_factory=None, progress=None):
    """Bring the disc copy at `image_path` to this launch's music. Returns a short summary.
    progress(done, total, song) is called as each of your songs is ready, as converting them is the
    slow part."""
    if image_factory is None:
        from .patcher import Image as image_factory
    mode = mode if mode in MODES else "off"
    state = state_dir(cache_root, image_path)
    manifest_path = os.path.join(state, "manifest.json")
    if mode == "off" and not os.path.isfile(manifest_path):
        return "music: game soundtrack"             # never changed on this disc copy: nothing to undo
    os.makedirs(os.path.join(state, "backup"), exist_ok=True)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}

    def save():
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    # your songs first: converting is slow and needs nothing from the disc
    custom = {"bgm": [], "events": []}
    if mode in ("game_and_custom", "custom_only"):
        conv_dir = os.path.join(cache_root, "converted")
        os.makedirs(conv_dir, exist_ok=True)
        songs = collect(folder, cache_root)
        ffmpeg = find_ffmpeg(ffmpeg_path)
        if songs and not ffmpeg:
            warn("ffmpeg was not found, so only songs already in the game's format (.mss) and 16-bit 32 kHz .wav "
                 "files are used. Install ffmpeg (Windows: winget install Gyan.FFmpeg; macOS: brew install "
                 "ffmpeg) and restart the Launcher, or set ffmpeg_path under timesplitters2_options in host.yaml.")
        outs = convert_all(ffmpeg, [src for _g, _l, src in songs], conv_dir, progress=progress, log=log)
        bad = 0
        for group, label, src in songs:
            if outs.get(src):
                custom[group].append(Song(label, group, path=outs[src]))
            else:
                bad += 1
        if bad:
            warn("%d song(s) could not be used (unreadable, or not convertible without ffmpeg)." % bad)
        if not songs:
            warn("No songs found in %r; the game's own music is shuffled instead." % folder)

    image = image_factory(image_path, writable=True)
    try:
        fst_off, current = read_fst(image)
        original_path = os.path.join(state, "original_fst.bin")
        current_sha = hashlib.sha1(bytes(current)).hexdigest()
        if current_sha not in (manifest.get("fst_sha1"), manifest.get("fst_prev")) or not os.path.isfile(original_path):
            # never touched, or written fresh from the original disc since: the table on it is the original,
            # and older backups belong to a copy that no longer exists
            shutil.rmtree(os.path.join(state, "backup"), ignore_errors=True)
            os.makedirs(os.path.join(state, "backup"), exist_ok=True)
            with open(original_path, "wb") as f:
                f.write(current)
            manifest = {}
        with open(original_path, "rb") as f:
            original = bytearray(f.read())
        entries = fst_entries(original)
        tail = free_tail(image, original, entries)

        # 1. put back what the last launch overwrote
        restore(image, state, [tuple(r) for r in manifest.get("music", [])], warn)
        manifest["music"] = []
        table = bytearray(original)
        summary = "music: game soundtrack"

        # 2. this launch's music
        if mode != "off":
            rng = random.Random(seed) if seed else random.Random()
            bgm = tuple(n for n in BGM if n in entries)
            events = tuple(n for n in EVENTS if n in entries)
            game = {n: Song(n, g, region=entry_region(original, entries[n]))
                    for g, names in (("bgm", bgm), ("events", events)) for n in names}
            assign = {}
            assign.update(plan(mode, rng, list(bgm), game, custom["bgm"]))
            assign.update(plan(mode, rng, list(events), game, custom["events"]))

            # room for the custom songs: game tracks nobody picked, and the free tail
            picked_game = {s.name for s in assign.values() if not s.custom}
            extents = [game[n].region for n in bgm + events if n not in picked_game] + tail
            customs = list({id(s): s for s in assign.values() if s.custom}.values())
            placed, left = place(customs, extents)
            hit = set()
            for song, off in placed.items():
                for n in bgm + events:
                    o, s = game[n].region
                    if off < o + s and o < off + song.size:
                        hit.add(n)
            if left:
                # a slot whose song did not fit plays a game track whose data is still intact
                intact = [game[n] for n in bgm if n not in hit]
                warn("%d song(s) did not fit on the disc this time and were swapped for game tracks." % len(left))
                for slot, song in list(assign.items()):
                    if song in left:
                        assign[slot] = game[slot] if slot not in hit else rng.choice(intact)
            # back up every range a song lands on (the tail too, so "off" leaves it as it was), then write
            ranges = [(off, song.size) for song, off in placed.items()]
            back_up(image, state, ranges)
            manifest["music"] = ranges
            save()                                     # before writing: a crash can still restore
            for song, off in placed.items():
                with open(song.path, "rb") as f:
                    write_span(image, off, f.read())
            for slot, song in assign.items():
                if song.custom:
                    set_entry(table, entries[slot], placed[song], song.size)
                else:
                    set_entry(table, entries[slot], *song.region)
            summary = "music: %s" % mode + (", %d of your songs" % len(placed) if mode != "game_music" else "")

        manifest["fst_prev"], manifest["fst_sha1"] = current_sha, hashlib.sha1(bytes(table)).hexdigest()
        save()
        write_span(image, fst_off, bytes(table))
        log(summary)
        return summary
    finally:
        image.close()
