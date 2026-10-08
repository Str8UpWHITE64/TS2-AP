"""Mouse look for TimeSplitters 2 / Future Perfect (GameCube) under Dolphin.

A Python stand-in for the C drivers in games/. It captures raw mouse movement and
steers the camera two ways, picking between them automatically:

  * writing the camera angle directly -- 1:1 with no speed ceiling, and what it uses
    whenever the game will accept it (on foot, including while scoped);
  * writing the delta block a game-side code cave adds to the C-stick, for the modes
    where the game re-derives the camera every frame and an angle write cannot
    survive: vehicles, turrets, the mech, the sentry cameras.

Raw Input (WM_INPUT with RIDEV_INPUTSINK) is used rather than cursor position, so
deltas keep arriving while Dolphin owns and locks the cursor, and no cursor
recentring is needed.

The angle path works on any image. The C-stick fall-back needs one patched with the
optional mouse cave, which is also what makes the non-first-person modes work:
    python patch_iso.py <image> --mouse -o <image (mouse)>

Usage:
    python mouse_driver.py                 capture on, sensitivity 40
    python mouse_driver.py --sens 60       faster
    python mouse_driver.py --invert        invert pitch
    python mouse_driver.py --flipx         reverse left/right
    python mouse_driver.py --status        report and exit (no capture)
    python mouse_driver.py --debug         print the deltas being written
    python mouse_driver.py --mode cursor   force cursor-position mode (Remote Desktop)
    python mouse_driver.py --unlock-story  hold every story mission unlocked (testing aid)
    python mouse_driver.py --look stick    force the C-stick path (rate-limited)
    python mouse_driver.py --degrees 0.2   more turn per inch of mouse
    python mouse_driver.py --stick-y 2     more vertical gain on the C-stick path (vehicles)

While running:
    F6 / F7   sensitivity down / up
    F8        toggle capture on / off      (starts ON)
    F9        quit

NOTE: the game only converts pad input for a CONNECTED port. If Dolphin's port 1
shows no controller, nothing is injected -- set Dolphin's port 1 to "Standard
Controller" (SIDevice0 = 6) and give it a device that is present.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

import dolphin_memory_engine as dme

MAGIC = b"MOUS"

# Direct-angle ("take over the look") support. The C-stick is a RATE input: past a
# fairly small deflection it saturates at the game's own maximum turn speed, so a fast
# flick and a slow push produce the same rotation and it never feels like a mouse.
# On foot the game keeps an externally written camera angle -- writing yaw/pitch is
# ~1:1 and has no speed ceiling (measured 708 deg/s against 720 requested, 976 against
# 1000). In a vehicle or turret the game re-derives the camera from the vehicle every
# frame, so there we fall back to the C-stick, the only input those modes listen to.
#   base ptr -> yaw/pitch floats; pitch limit float; field of view (fov_ptr + fov_off, or
#   fov_addr) and its unzoomed value; and `mode`, where the game says a remote view has
#   the camera, if one is known.
# `mode` (TS2 only): base+0x158 is a bit field; bit 0x8 is set on a sentry gun and clear
# on foot (every mount and dismount while watched live). It is NOT "non-zero": on foot it
# also flickers 0x60000000, and treating that as a sentry flipped the vertical mid-walk.
# The sentry pointer at pawn+0x1454 (pawn = *0x804013C8) is required too -- it was set
# only ever on a sentry. A sentry keeps
# its own aim and ignores the camera angle entirely, yet nothing overwrites a written
# angle either, so the watcher alone cannot see that the angle path is dead there.
# Future Perfect has no such field in use: its vehicle pointer (pawn+0xC14) is set on
# the gun emplacement, which takes angle writes 1:1, so the watcher decides alone.
# `camera_mode` (FP): (camera pointer, mode offset, remote modes). FUN_8009df90 switches on
# *(0x80611D54)+0x218; mode 3 is a camera attached to another object -- the security /
# turret cameras of Breaking and Entering, Machine Wars' sentry -- which turns from the
# stick and ignores the player's view angles (live: yaw/pitch sat still while the camera
# turned). Mode 2 is Something to Crow About's turret (vehicle pointer set): written
# angles stuck but the turret did not move. In a remote mode the mouse goes to the stick. Other modes are left to the watcher: the gun
# emplacement takes angle writes 1:1.
# `pitch_flip_modes` (FP): camera modes whose pitch runs the other way to on-foot look, so
# a written pitch has to be reversed there. Mode 24 (0x18) is the Machine Wars tank: it
# takes angle writes but aimed down for mouse-up (live, inverse look off).
# `angle_modes` (FP): camera modes that always take the angle path, watcher or not. The
# tank (24) follows angle writes, but its turret stops at limits, and pushing into one
# looked "dead both ways" to the watcher, which handed a second tank to the stick.
# `paused` (FP): a word that is non-zero while the game is paused (0x8061202C, beside the
# frame counter: 0 in play, 1 in the pause menu -- found live by pausing three times).
# While paused the mouse is ignored: written angles turned the player in the pause menu,
# and on Something to Crow About's turret that left them unable to get off.
# `invert`: where the player's "inverse look" preference lives (address, bit), if known.
# The angle path writes pitch itself, so it has to apply that preference; on the stick
# path the game applies it.
# `remote_flip_y`: a TS2 sentry reads the stick's vertical the opposite way to on-foot
# look -- with inverse look OFF, stick up aims it DOWN; with it ON, up aims up (tested
# live, both settings). So on a sentry the vertical is always sent flipped, and the
# player's preference then comes out right either way.
ANGLE = {
    b"G3FE69": dict(base=0x80611D74, yaw=0x100, pitch=0x104, limit=0x80611D7C, mode=None,
                    # player 1's profile (slot 0 of the array at 0x80501608) +0x04, bit 0 =
                    # inverse look; gameplay reads it straight from there (tested live: the
                    # same stick push went up, then down with only this bit flipped)
                    invert=(0x80501608 + 0x04, 1), remote_flip_y=False,
                    camera_mode=(0x80611D54, 0x218, (2, 3)), pitch_flip_modes=(24,), angle_modes=(24,),
                    paused=0x8061202C,
                    fov_ptr=0x80611D5C, fov_off=0x384, fov_addr=None, fov_ref=55.0),
    b"GTSE4F": dict(base=0x804686CC, yaw=0x148, pitch=0x14C, limit=0x804686BC,
                    mode=(0x158, 0x8, 0x804013C8, 0x1454),
                    # live copy of the active profile's preference flags; bit 0 = inverse
                    # look (the AP client's memmap: ACTIVE_PROFILE_COPY + PROFILE_PREF_FLAGS)
                    invert=(0x803E6350 + 0x18, 1), remote_flip_y=True,
                    fov_ptr=None, fov_off=None, fov_addr=0x8046818C, fov_ref=60.0),
}

# game id -> (name, hook site, stock word at that site, record base, C-stick X/Y offsets,
#             delta block address)
# TS2's block is NOT at 0x80001800: the randomizer's weapon block owns
# 0x80001800..0x80002CC8, and sharing it made the two overwrite each other's magic.
# An earlier TS2 cave read its block at 0x80001800 (the randomizer's weapon block). Its
# hook word is the same as the current one, so it is told apart by the one word that
# builds the block address: (address, word in the old cave).
OLD_CAVE = {b"GTSE4F": (0x8010AE30, 0x38A51800)}

GAMES = {
    b"GTSE4F": ("TimeSplitters 2", 0x800F81D0, 0x480000F9, 0x80308400, 0x34, 0x3C, 0x80002F00),
    b"G3FE69": ("TimeSplitters: Future Perfect", 0x8018CA18, 0x4BFFFCB5, 0x804B1140, 0x24, 0x28,
                0x80001800),
}

# private handles: argtypes set below must not leak into other code in the same process
# (inside the Archipelago client they broke Kivy's own GetClientRect calls)
user32 = ctypes.WinDLL("user32", use_last_error=True)
STICKSCALE = 6.0            # stick units per mouse count at sensitivity 40
VK_F6, VK_F7, VK_F8, VK_F9 = 0x75, 0x76, 0x77, 0x78

# --- Raw Input plumbing -----------------------------------------------------
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
MOUSE_MOVE_ABSOLUTE = 0x01
WM_INPUT = 0x00FF
HWND_MESSAGE = -3


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class _RAWMOUSE_BUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wt.USHORT), ("usButtonData", wt.USHORT)]


class _RAWMOUSE_U(ctypes.Union):
    # ulButtons overlaps usButtonFlags/usButtonData -- it is a UNION in the Windows
    # headers. Declaring them sequentially shifts lLastX/lLastY by 4 bytes, which
    # reads them as a constant zero (every delta silently lost).
    _anonymous_ = ("s",)
    _fields_ = [("ulButtons", wt.ULONG), ("s", _RAWMOUSE_BUTTONS)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("usFlags", wt.USHORT), ("u", _RAWMOUSE_U),
                ("ulRawButtons", wt.ULONG), ("lLastX", ctypes.c_long),
                ("lLastY", ctypes.c_long), ("ulExtraInformation", wt.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


LRESULT = ctypes.c_ssize_t          # LONG_PTR: must not be c_long on 64-bit
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)

# Default ctypes restypes are 32-bit ints, which truncates every handle on 64-bit
# Windows -- CreateWindowExW then "succeeds" but returns an unusable HWND.
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.RegisterClassW.restype = wt.ATOM
user32.GetRawInputData.restype = wt.UINT
user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID,
                                   ctypes.POINTER(wt.UINT), wt.UINT]
user32.RegisterRawInputDevices.restype = wt.BOOL


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


user32.GetForegroundWindow.restype = wt.HWND
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ClipCursor.argtypes = [ctypes.c_void_p]


class CursorLock:
    """Keep the Windows cursor inside the game window while mouse look is live.

    Look comes from raw input, which never needed the cursor -- but Dolphin still moves
    it, so a long turn walked it off the window and the next click landed on the
    desktop. Clipping it to the window's client area (inset a little) keeps every click
    in the game. Deliberately the whole window rather than one pixel: if the driver is
    ever killed without cleaning up, a leftover clip then only keeps the cursor inside
    Dolphin, instead of freezing it. Windows drops a clip on some focus changes, so it
    is re-asserted a few times a second, and it is released whenever capture pauses,
    Dolphin loses focus, or the driver exits.
    """
    REASSERT = 0.25
    INSET = 16

    def __init__(self):
        self.locked = False
        self.due = 0.0

    def hold(self):
        now = time.time()
        if self.locked and now < self.due:
            return
        hwnd = user32.GetForegroundWindow()
        rc = RECT()
        if not hwnd or not user32.GetClientRect(hwnd, ctypes.byref(rc)):
            return
        tl = wt.POINT(rc.left, rc.top)
        br = wt.POINT(rc.right, rc.bottom)
        user32.ClientToScreen(hwnd, ctypes.byref(tl))
        user32.ClientToScreen(hwnd, ctypes.byref(br))
        inset = min(self.INSET, (br.x - tl.x) // 4, (br.y - tl.y) // 4)
        box = RECT(tl.x + inset, tl.y + inset, br.x - inset, br.y - inset)
        user32.ClipCursor(ctypes.byref(box))
        self.locked = True
        self.due = now + self.REASSERT

    def release(self):
        if self.locked:
            user32.ClipCursor(None)
            self.locked = False


class CursorMouse:
    """Movement from the cursor's absolute position.

    Fallback for sessions that do not deliver Raw Input -- notably Remote Desktop,
    which forwards the mouse as absolute cursor moves. The cursor is only recentred
    when it nears a screen edge, so it neither fights the remote session's own
    cursor nor gets pinned in a corner.
    """

    EDGE = 80

    def __init__(self):
        self.w = user32.GetSystemMetrics(0)
        self.h = user32.GetSystemMetrics(1)
        self.last = self._pos()

    @staticmethod
    def _pos():
        p = wt.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def pump(self, recentre):
        """Movement since the last call. Only moves the cursor when recentre is set --
        i.e. cursor mode is live and Dolphin is in front -- so the desktop stays usable
        (taskbar, title bars) the rest of the time."""
        x, y = self._pos()
        dx, dy = x - self.last[0], y - self.last[1]
        self.last = (x, y)
        if recentre and (x < self.EDGE or x > self.w - self.EDGE
                         or y < self.EDGE or y > self.h - self.EDGE):
            user32.SetCursorPos(self.w // 2, self.h // 2)
            self.last = self._pos()
        return dx, dy


class RawMouse:
    """Accumulates relative mouse movement from WM_INPUT."""

    def __init__(self):
        self.dx = 0
        self.dy = 0
        self._buf = ctypes.create_string_buffer(1024)
        self._proc = WNDPROC(self._wndproc)      # keep a reference alive
        cls = WNDCLASS()
        cls.lpfnWndProc = self._proc
        cls.lpszClassName = "TSMouseSink"
        cls.hInstance = kernel32.GetModuleHandleW(None)
        if not user32.RegisterClassW(ctypes.byref(cls)):
            err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            if err not in (0, 1410):            # 1410 = class already registered
                raise ctypes.WinError()
        self.hwnd = user32.CreateWindowExW(0, "TSMouseSink", "TSMouseSink", 0, 0, 0, 0, 0,
                                           wt.HWND(HWND_MESSAGE), None, cls.hInstance, None)
        if not self.hwnd:
            raise ctypes.WinError()
        rid = RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, self.hwnd)   # generic desktop / mouse
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
            raise ctypes.WinError()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            size = wt.UINT(ctypes.sizeof(self._buf))
            got = user32.GetRawInputData(wt.HANDLE(lparam), RID_INPUT, self._buf,
                                         ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
            if got > 0:
                ri = ctypes.cast(self._buf, ctypes.POINTER(RAWINPUT)).contents
                # MOUSE_MOVE_ABSOLUTE (tablets, some remote-desktop and VM tools) reports
                # positions on a 0..65535 grid, not movement; adding those as deltas
                # spins the view wildly. Leave them to cursor mode.
                if ri.header.dwType == RIM_TYPEMOUSE and not ri.mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
                    self.dx += ri.mouse.lLastX
                    self.dy += ri.mouse.lLastY
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pump(self):
        """Drain pending messages; returns the movement since the last call."""
        msg = wt.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):   # PM_REMOVE
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        dx, dy = self.dx, self.dy
        self.dx = self.dy = 0
        return dx, dy


# --- game side --------------------------------------------------------------
def u32(addr):
    return int.from_bytes(dme.read_bytes(addr, 4), "big")


def foreground_is_dolphin():
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return "Dolphin" in buf.value


def detect():
    if not dme.is_hooked():
        dme.hook()
    if not dme.is_hooked():
        return None
    try:
        gid = dme.read_bytes(0x80000000, 6)
    except Exception:
        return None
    return GAMES.get(gid)


def write_block(block, dx, dy, port=0):
    # magic last, so the cave never reads a half-written block
    dme.write_bytes(block + 4, struct.pack(">iii", port, int(dx), int(dy)))
    dme.write_bytes(block, MAGIC)


def block_consumed(block):
    """Has the cave taken the last delta?

    The caves zero dx/dy when they take them and leave the magic alone, so "taken"
    is dx and dy both reading zero. (Without the magic the cave ignores the block
    entirely, so that counts as free too.)
    """
    raw = dme.read_bytes(block, 16)
    if raw[:4] != MAGIC:
        return True
    return raw[8:16] == bytes(8)


# The cave consumes the block once per frame; we poll several times faster than that.
# Overwriting it would throw away every delta that lands between two frames -- and
# which ones survive depends on where the tick falls, so the loss is uneven as well as
# large. Accumulate instead, and only hand over once the cave has taken the last lot.
PENDING_LIMIT = 255         # one frame of full deflection; the cave clamps anyway
STALE_AFTER = 0.5           # seconds the cave can ignore the block before we give up


class StickQueue:
    def __init__(self, block):
        self.block = block
        self.x = 0
        self.y = 0
        self.since = None

    def add(self, dx, dy):
        if self.x == 0 and self.y == 0:
            self.since = time.time()
        self.x = max(-PENDING_LIMIT, min(PENDING_LIMIT, self.x + dx))
        self.y = max(-PENDING_LIMIT, min(PENDING_LIMIT, self.y + dy))

    def drop(self):
        self.x = self.y = 0
        self.since = None

    def flush(self, port):
        """Give the cave everything queued, once it has finished with the last delta."""
        if not (self.x or self.y):
            return
        if block_consumed(self.block):
            write_block(self.block, self.x, self.y, port)
            self.drop()
        elif self.since is not None and time.time() - self.since > STALE_AFTER:
            # nobody is reading the block (game paused, pad disconnected). Throw the
            # movement away rather than hoarding it into a lurch when play resumes.
            self.drop()


STORY_UNLOCK = {
    # game id -> (game-data address, tier-1 word holding mission bits 48.., mission count)
    b"G3FE69": (0x80502898, 0x80502898 + 0xBFC + 0x10 + 4, 13),
}


def hold_story_unlock(gid):
    """Keep every story mission's spare gate bit set (patched image only).

    The menus read the verdict when a screen is built, so a mission unlocked while
    its list is on screen appears after backing out and re-entering.
    """
    ent = STORY_UNLOCK.get(gid)
    if ent is None:
        return
    _, word, n = ent
    want = 0
    for m in range(n):
        want |= 1 << ((48 + m) & 31)
    cur = u32(word)
    if cur & want != want:
        dme.write_bytes(word, ((cur | want) & 0xFFFFFFFF).to_bytes(4, "big"))


def angle_state(gid):
    """(base, cfg, remote_view) or None when there is no player yet.

    remote_view is the game's own "a sentry has the camera" flag where one is known;
    it overrides the watcher (see ANGLE)."""
    cfg = ANGLE.get(gid)
    if cfg is None:
        return None
    base = u32(cfg["base"])
    if not 0x80000000 <= base < 0x81800000:
        return None
    remote = False
    if cfg["mode"] is not None:
        off, bit, pawn_ptr, pawn_off = cfg["mode"]
        if u32(base + off) & bit:
            pawn = u32(pawn_ptr)
            remote = 0x80000000 <= pawn < 0x81800000 and u32(pawn + pawn_off) != 0
    if cfg.get("camera_mode") is not None and not remote:
        ptr, off, modes = cfg["camera_mode"]
        cam = u32(ptr)
        remote = 0x80000000 <= cam < 0x81800000 and u32(cam + off) in modes
    return base, cfg, remote


def camera_in(cfg, key):
    """Is the camera in one of the modes listed under `key` (pitch_flip_modes, angle_modes)?"""
    modes = cfg.get(key)
    if not modes or cfg.get("camera_mode") is None:
        return False
    try:
        cam = u32(cfg["camera_mode"][0])
        return 0x80000000 <= cam < 0x81800000 and u32(cam + cfg["camera_mode"][1]) in modes
    except RuntimeError:
        return False


def pitch_flipped(cfg):
    return camera_in(cfg, "pitch_flip_modes")


def game_inverts(cfg):
    """Has the player turned on the game's own inverse-look preference?"""
    if cfg["invert"] is None:
        return False
    addr, bit = cfg["invert"]
    try:
        return bool(u32(addr) & bit)
    except RuntimeError:
        return False


def fov_scale(cfg):
    """Slow the turn in proportion to zoom, as the stock drivers do.

    Only ever slows it: clamped to 1.0 so that if the unzoomed field of view is wider
    than the reference, normal on-foot turning is left exactly as it was.
    """
    try:
        if cfg["fov_addr"] is not None:
            addr = cfg["fov_addr"]
        else:
            ptr = u32(cfg["fov_ptr"])
            if not 0x80000000 <= ptr < 0x81800000:
                return 1.0
            addr = ptr + cfg["fov_off"]
        fov = struct.unpack(">f", dme.read_bytes(addr, 4))[0]
    except Exception:
        return 1.0
    if not 3.0 < fov < 170.0:
        return 1.0
    return min(1.0, fov / cfg["fov_ref"])


def write_angles(base, cfg, ddeg_x, ddeg_y):
    """Add degrees straight to the camera yaw/pitch, clamped to the game's pitch limit.

    Returns the yaw we left behind, so the caller can tell next tick whether the game
    accepted it or overwrote it.
    """
    limit = struct.unpack(">f", dme.read_bytes(cfg["limit"], 4))[0]
    if not 5.0 <= limit <= 89.0:
        limit = 60.0
    yaw = struct.unpack(">f", dme.read_bytes(base + cfg["yaw"], 4))[0]
    pit = struct.unpack(">f", dme.read_bytes(base + cfg["pitch"], 4))[0]
    yaw = (yaw - ddeg_x) % 360.0
    pit = max(-limit, min(limit, pit + ddeg_y))
    dme.write_bytes(base + cfg["yaw"], struct.pack(">f", yaw))
    dme.write_bytes(base + cfg["pitch"], struct.pack(">f", pit))
    return yaw


def read_yaw(base, cfg):
    return struct.unpack(">f", dme.read_bytes(base + cfg["yaw"], 4))[0]


class FightBack:
    """Notice when the game is driving the camera itself.

    The rule is "yaw should be where we left it, plus whatever we turned it since": on
    foot, and on FP's gun emplacement, the game keeps an externally written angle
    exactly, so any other drift means the game is steering the camera (a vehicle
    pulling it toward its own heading) and the angle write is pointless -- fall back
    to the C-stick, which every mode listens to. No per-game vehicle pointer is used:
    the emplacement has one and still takes angle writes 1:1.

    Judged on a fixed ~60 Hz beat rather than per tick, because we poll several times
    per game frame and the game only moves the camera once a frame.

    Two ways the game can own the camera, and one thing that must not be mistaken
    for either (all three seen live in FP):

    * It STEERS it: a vehicle pulls the camera toward its own heading, or turns it
      with the vehicle. That shows while the mouse rests -- on foot and on the gun
      emplacement an untouched camera does not move at all (measured 0.00 deg over
      0.5 s). IDLE_MOVES beats within one rest in which it moved on its own hand it to
      the stick. Counted per rest, not "in a row", because our beat and the game's
      frame drift against each other.
    * It REBUILDS it every frame from its own state (Scotland's vehicle-mounted gun):
      a written angle reads back unchanged, so turning does nothing -- in EITHER
      direction. Dead turning beats in both directions, with no working beat in
      between, hand it to the stick.
    * A LIMIT is neither: a turret pushed into the end of its arc stops dead (it
      snaps back to the limit within one frame, measured), springs off it, stops
      partway through a frame -- but only in one direction; turning back away from
      the limit works, and resets the count. Judging turning beats one-sidedly is
      what used to hand a perfectly good turret to the slow stick path mid-swing.

    Taking it back needs evidence: a still camera proves nothing, since a vehicle sits
    still too once the stick centres. So while the mouse rests, PROBE nudges the yaw
    and requires it to stay exactly there for PROBE_BEATS frames -- long enough for a
    vehicle's gradual pull-back to show. If it holds, the nudge is undone and the angle
    path resumes; if the mouse moves first, the probe is cancelled and undone.
    """
    BEAT = 1.0 / 60.0   # seconds; one game frame
    TOLERANCE = 1.0     # degrees; for the probe, which the game may round or settle
    IDLE_TOLERANCE = 0.25   # degrees; an untouched camera on foot / on the emplacement
                            # moves exactly 0.00, while a vehicle's pull-back fades out
                            # within a frame or two, so idle beats need a fine threshold
    IDLE_MOVES = 3      # beats in one rest in which the camera moved by itself
    IDLE_WINDOW = 0.25  # seconds those have to fall within
    PROBE = 2.0         # degrees; must clear TOLERANCE to be readable
    PROBE_BEATS = 6     # frames the nudge must hold untouched
    PROBE_REST = 0.20   # seconds of no mouse movement before probing
    PROBE_EVERY = 0.30  # seconds between probe attempts

    def __init__(self, log=None):
        self.log = log
        self.reset()

    def _say(self, msg):
        if self.log:
            self.log(msg)

    def reset(self):
        """Nothing to judge -- capture is off, or Dolphin is not in front."""
        self.expect = None
        self.applied = 0.0
        self.tested = False
        self.due = 0.0
        self.probe_due = 0.0
        self.probing = 0.0
        self.probe_good = 0
        self.probe_at = None
        self.rested_since = None
        self.idle_moves = []
        self.dead_dirs = set()
        self.turn_dir = 0
        self.owned = False

    @staticmethod
    def _wrap(a):
        return (a + 180.0) % 360.0 - 180.0

    def _write_yaw(self, base, cfg, yaw):
        dme.write_bytes(base + cfg["yaw"], struct.pack(">f", yaw % 360.0))

    def observe(self, base, cfg):
        """Call once per tick, before deciding. True == the game owns the camera."""
        now = time.time()
        if now < self.due:
            return self.owned
        self.due = now + self.BEAT
        yaw = read_yaw(base, cfg)
        if self.probing:
            if abs(self._wrap(yaw - self.expect)) <= self.TOLERANCE:
                self.probe_good += 1
                if self.probe_good >= self.PROBE_BEATS:
                    yaw = (yaw - self.probing) % 360.0      # it held: take the nudge back
                    self._write_yaw(base, cfg, yaw)
                    self.probing = 0.0
                    self.owned = False
                    self.dead_dirs = set()
                    self.idle_moves = []
                    self._say("watcher: angle holds again -> angle path")
                else:
                    return self.owned                       # keep watching the nudge
            else:
                self.probing = 0.0                          # the game moved it: still owned
            self.expect, self.applied, self.tested = yaw, 0.0, False
            return self.owned
        if self.expect is not None and not self.tested and not self.owned:
            # an idle beat on the angle path: the camera should not have moved at all
            self.turn_dir = 0
            moved = abs(self._wrap(yaw - self.expect))
            if moved > self.IDLE_TOLERANCE:
                self.idle_moves = [t for t in self.idle_moves if now - t <= self.IDLE_WINDOW]
                self.idle_moves.append(now)
                if len(self.idle_moves) >= self.IDLE_MOVES:
                    self.owned = True
                    self.idle_moves = []
                    self._say("watcher: camera kept moving by itself -> stick path")
        elif self.expect is not None and self.tested and not self.owned:
            self.idle_moves = []                            # a new rest starts from zero
            direction = 1 if self.applied > 0 else -1
            same_dir, self.turn_dir = direction == self.turn_dir, direction
            # judged only when the previous beat turned the same way: the first beat after
            # a reversal or a rest also holds the game undoing our last overshoot, which
            # can land in the direction we now turn and pass for a working beat
            if same_dir and abs(self.applied) > self.TOLERANCE:
                went = self._wrap(yaw - self.expect)
                followed = went * self.applied > 0 and abs(went) >= 0.5 * abs(self.applied)
                if followed:
                    self.dead_dirs = set()                  # the angle path works
                else:
                    self.dead_dirs.add(1 if self.applied > 0 else -1)
                    if len(self.dead_dirs) == 2:
                        self.owned = True
                        self.dead_dirs = set()
                        self._say("watcher: turning does nothing either way -> stick path")
        self.expect, self.applied, self.tested = yaw, 0.0, False
        return self.owned

    def applied_turn(self, ddeg_yaw):
        """Degrees of yaw this driver just applied itself; not the game moving."""
        self.applied += ddeg_yaw
        self.tested = True

    def moved(self):
        """The mouse is being used: hold off probing, and cancel one in flight."""
        self.rested_since = None
        if self.probing and self.probe_at is not None:
            base, cfg = self.probe_at
            try:
                self._write_yaw(base, cfg, read_yaw(base, cfg) - self.probing)
            except RuntimeError:
                pass
            self.probing = 0.0
            self.expect = None

    def maybe_probe(self, base, cfg):
        """While the stick has the camera, ask now and then whether it still does."""
        if not self.owned or self.probing:
            return
        now = time.time()
        if self.rested_since is None:
            self.rested_since = now
            return
        if now - self.rested_since < self.PROBE_REST or now < self.probe_due:
            return
        self.probe_due = now + self.PROBE_EVERY
        yaw = (read_yaw(base, cfg) + self.PROBE) % 360.0
        self._write_yaw(base, cfg, yaw)
        self.probing = self.PROBE
        self.probe_good = 0
        self.probe_at = (base, cfg)
        self.expect, self.applied, self.tested = yaw, 0.0, False
        self.due = 0.0


def status(game, gid):
    name, hook, stock, rec, cx, cy, block = game
    word = u32(hook)
    present = word != stock
    old = OLD_CAVE.get(gid)
    outdated = present and old is not None and u32(old[0]) == old[1]
    if outdated:
        verdict = "OUTDATED -- re-run the patcher with --mouse to update it"
    elif present:
        verdict = "PRESENT"
    else:
        verdict = "MISSING -- patch the image with --mouse"
    print("game:        %s" % name)
    print("cave:        %s (word at %08x is %08x, stock %08x)" % (verdict, hook, word, stock))
    print("C-stick now: X %d  Y %d   (128 = centred)" % (u32(rec + cx), u32(rec + cy)))
    # an outdated cave reads a different address, so it is as good as absent
    return present and not outdated


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sens", type=float, default=40.0, help="sensitivity (40 = default)")
    ap.add_argument("--invert", action="store_true",
                    help="flip pitch on top of the game's own inverse-look preference, which "
                         "is followed automatically where it is known (TS2)")
    ap.add_argument("--flipx", action="store_true", help="reverse left/right (if the camera turns the wrong way)")
    ap.add_argument("--port", type=int, default=0, help="pad port to inject into (player 1 = 0)")
    ap.add_argument("--status", action="store_true", help="report and exit")
    ap.add_argument("--debug", action="store_true", help="print the deltas being written")
    ap.add_argument("--look", choices=("hybrid", "angle", "stick"), default="hybrid",
                    help="hybrid (default): write the camera angle directly on foot (1:1, no "
                         "speed cap) and fall back to the C-stick when the game owns the camera "
                         "(vehicles, turrets). angle: always write the angle. stick: always feed "
                         "the C-stick -- works everywhere but is rate-limited by the game.")
    ap.add_argument("--stick-y", type=float, default=1.0,
                    help="extra vertical gain on the C-stick path (vehicles): mouse movement "
                         "up/down is usually much smaller than side to side, so vertical stick "
                         "deflection can stay in the slow zone near centre")
    ap.add_argument("--degrees", type=float, default=0.12,
                    help="degrees of turn per mouse count at sensitivity 40 (angle modes)")
    ap.add_argument("--unlock-story", action="store_true",
                    help="hold every story mission unlocked while running (testing aid)")
    ap.add_argument("--mode", choices=("auto", "raw", "cursor"), default="auto",
                    help="how to read the mouse: raw input, cursor position, or auto-detect "
                         "(auto falls back to cursor when raw input never arrives, as over Remote Desktop)")
    ap.add_argument("--hz", type=float, default=250.0, help="polling rate")
    ap.add_argument("--wait", action="store_true",
                    help="wait for Dolphin to be running the game instead of exiting (for launchers)")
    args = ap.parse_args(argv)

    game = detect()
    while game is None and args.wait:
        time.sleep(1.0)
        game = detect()
    gid = dme.read_bytes(0x80000000, 6) if dme.is_hooked() else b""
    if game is None:
        sys.exit("No supported game found. Start Dolphin on TimeSplitters 2 or Future Perfect first.")
    present = status(game, gid)
    if args.status:
        return
    look = args.look
    if not present:
        # the angle path needs no cave; only the C-stick fall-back does
        if look == "stick":
            sys.exit("--look stick needs the mouse cave, and this image has none "
                     "(patch it with --mouse).")
        look = "angle"
        print("no mouse cave: angle look only. On foot works as normal; vehicles, turrets "
              "and the mech will not respond to the mouse (patch the image with --mouse "
              "for those).")

    sens = args.sens
    scale = (sens / 40.0) * STICKSCALE
    period = 1.0 / args.hz
    raw = RawMouse() if args.mode in ("auto", "raw") else None
    cur = CursorMouse() if args.mode in ("auto", "cursor") else None
    mode = args.mode if args.mode != "auto" else "raw"      # auto starts on raw, may switch
    raw_seen = False
    switch_deadline = time.time() + 2.0                      # how long to wait for raw input
    capture = True
    print("\ncapture ON  (F8 toggles, F9 quits).  Click into the Dolphin window and move the mouse.")
    print("sensitivity %.0f%s, port %d\n" % (args.sens, ", inverted" if args.invert else "", args.port))
    f6_was = f7_was = f8_was = f9_was = False
    moved = 0
    fight = FightBack(log=print if args.debug else None)
    stick = StickQueue(game[6])
    lock = CursorLock()
    remote_was = False
    try:
        while True:
            time.sleep(period)
            front = foreground_is_dolphin()
            rdx, rdy = raw.pump() if raw is not None else (0, 0)
            cdx, cdy = cur.pump(recentre=(mode == "cursor" and capture and front))                 if cur is not None else (0, 0)
            if rdx or rdy:
                raw_seen = True
            if args.mode == "auto" and mode == "raw" and not raw_seen and (cdx or cdy)                     and time.time() > switch_deadline:
                mode = "cursor"
                print("no raw input in this session (remote desktop?) -- switching to cursor mode")
            dx, dy = (rdx, rdy) if mode == "raw" else (cdx, cdy)
            f6 = bool(user32.GetAsyncKeyState(VK_F6) & 0x8000)
            f7 = bool(user32.GetAsyncKeyState(VK_F7) & 0x8000)
            if f6 and not f6_was:
                sens = max(5.0, sens - 10.0)
                scale = (sens / 40.0) * STICKSCALE
                print("sensitivity %.0f" % sens)
            f6_was = f6
            if f7 and not f7_was:
                sens = min(400.0, sens + 10.0)
                scale = (sens / 40.0) * STICKSCALE
                print("sensitivity %.0f" % sens)
            f7_was = f7
            f8 = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
            f9 = bool(user32.GetAsyncKeyState(VK_F9) & 0x8000)
            if f9 and not f9_was:
                print("quit")
                return
            f9_was = f9
            if f8 and not f8_was:
                capture = not capture
                print("capture %s" % ("ON" if capture else "OFF"))
            f8_was = f8
            # deltas are drained every pass (above) so they never pile up while paused
            if not capture or not front:
                fight.reset()
                stick.drop()
                lock.release()
                continue
            # cursor mode reads the cursor position, so only pin it in raw mode
            if mode == "raw":
                lock.hold()
            else:
                lock.release()
            # everything below touches emulated memory, which vanishes if Dolphin is
            # closed or emulation stops -- including the idle branch
            try:
                if args.unlock_story:
                    hold_story_unlock(gid)
                pcfg = ANGLE.get(gid)
                if pcfg is not None and pcfg.get("paused") is not None and u32(pcfg["paused"]):
                    fight.reset()               # the game is paused: ignore the mouse entirely
                    stick.drop()
                    continue
                if dx == 0 and dy == 0:
                    # still watch the camera while the mouse rests: that is when the
                    # probe can ask whether the game has let go of the camera
                    if look == "hybrid":
                        st = angle_state(gid)
                        if st is not None:
                            fight.observe(st[0], st[1])
                            fight.maybe_probe(st[0], st[1])
                    if present:
                        stick.flush(args.port)
                    continue
                used = "stick"
                flip_y = False
                if look != "stick":
                    st = angle_state(gid)
                    if st is not None:
                        base, cfg, remote = st
                        # Only the watcher decides. The vehicle pointer is NOT a reason to
                        # leave the angle path: FP's gun emplacement has one, yet follows a
                        # written camera angle 1:1 with no cap (measured 480 deg/s sweeps,
                        # exact). Vehicles that really re-derive the camera pull it back,
                        # and the watcher hands those to the stick within ~50 ms.
                        watched = look == "hybrid" and fight.observe(base, cfg)
                        if remote != remote_was and args.debug:
                            print("view mode: %s" % ("remote (sentry) -> stick path" if remote
                                                     else "normal -> watcher decides"))
                        remote_was = remote
                        owned = look == "hybrid" and (remote or watched) and not camera_in(cfg, "angle_modes")
                        flip_y = remote and cfg["remote_flip_y"]
                        if look == "angle" or not owned:
                            degs = (sens / 40.0) * args.degrees * fov_scale(cfg)
                            ddeg_x = (-dx if args.flipx else dx) * degs
                            # the angle path writes pitch itself, so it applies the
                            # game's inverse-look preference (--invert flips on top)
                            inv = (args.invert != game_inverts(cfg)) != pitch_flipped(cfg)
                            write_angles(base, cfg, ddeg_x, (dy if inv else -dy) * degs)
                            fight.applied_turn(-ddeg_x)   # write_angles subtracts it
                            used = "angle"
                        fight.moved()
                if used == "stick":
                    sdx = round((-dx if args.flipx else dx) * scale)
                    # raw Y grows downward; the C-stick's Y grows upward
                    sdy = round((dy if args.invert else -dy) * scale * args.stick_y)
                    if flip_y:
                        sdy = -sdy          # a TS2 sentry reads the vertical reversed
                    stick.add(sdx, sdy)
                elif stick.x or stick.y:
                    stick.drop()            # angle path took over; drop stale deltas
                if present:
                    stick.flush(args.port)
            except Exception as exc:
                print("lost the game (%s) -- is Dolphin still running?" % exc)
                return
            if args.debug:
                moved += 1
                if moved % 10 == 0:
                    print("raw (%+5d,%+5d) -> %s" % (dx, dy, used))
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
