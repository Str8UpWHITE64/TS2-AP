"""Nintendo DSP ADPCM, and TS2's .mss music container, in plain Python.

Pure Python because Archipelago's frozen build ships without numpy. It is still quick
enough: decoding runs at several million samples a second, encoding at about 20x real
time per core.

A frame is 8 bytes for 14 samples: a header byte (predictor index << 4 | scale exponent)
and fourteen signed 4-bit residuals. Each sample is

    s = clamp16(((residual << scale) << 11) + 1024 + c1 * hist1 + c2 * hist2) >> 11

where (c1, c2) is one of eight coefficient pairs (11-bit fixed point) stored per channel.
"""
import struct
from array import array
from operator import mul

FRAME_SAMPLES = 14
FRAME_BYTES = 8
_NIB = [n if n < 8 else n - 16 for n in range(16)]


def nibble_count(samples):
    whole, rest = divmod(samples, FRAME_SAMPLES)
    return whole * 16 + (rest + 2 if rest else 0)


def decode(data, coefs, samples, hist1=0, hist2=0):
    """One channel's frames -> array('h') of `samples` samples."""
    out = array("h", bytes(2 * samples))
    k = i = 0
    nt = _NIB
    while k < samples:
        ps = data[i]
        sh = ps & 0xF
        c1 = coefs[(ps >> 4) * 2]
        c2 = coefs[(ps >> 4) * 2 + 1]
        for b in data[i + 1:i + 8]:
            for nib in (nt[b >> 4], nt[b & 15]):
                s = (((nib << sh) << 11) + 1024 + c1 * hist1 + c2 * hist2) >> 11
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                hist2 = hist1
                hist1 = s
                if k < samples:
                    out[k] = s
                k += 1
        i += 8
    return out


# --- coefficients ----------------------------------------------------------------------
def _frame_stats(x, start):
    """Autocorrelation sums (r00, r01, r02, r11, r12, r22) of the frame at `start` against the
    one and two samples before each of its samples."""
    a = x[start:start + FRAME_SAMPLES]
    if start >= 2:
        b = x[start - 1:start + FRAME_SAMPLES - 1]
        c = x[start - 2:start + FRAME_SAMPLES - 2]
    else:
        pre = [0] * (2 - start) + list(x[0:start + FRAME_SAMPLES - 1])
        b = pre[1:1 + FRAME_SAMPLES]
        c = pre[0:FRAME_SAMPLES]
    return (sum(map(mul, a, a)), sum(map(mul, a, b)), sum(map(mul, a, c)),
            sum(map(mul, b, b)), sum(map(mul, b, c)), sum(map(mul, c, c)))


def _energy(st, c1, c2):
    """Squared prediction error of a frame for predictor (c1, c2), from its stats."""
    r00, r01, r02, r11, r12, r22 = st
    return r00 - 2 * (c1 * r01 + c2 * r02) + c1 * c1 * r11 + 2 * c1 * c2 * r12 + c2 * c2 * r22


def _solve(st):
    """Least-squares (c1, c2) for summed stats, kept inside the stable region."""
    r00, r01, r02, r11, r12, r22 = st
    det = r11 * r22 - r12 * r12
    if det <= 1e-9 * max(1.0, r11 * r22):
        c1, c2 = (r01 / r11 if r11 > 0 else 0.0), 0.0
    else:
        c1 = (r01 * r22 - r02 * r12) / det
        c2 = (r02 * r11 - r01 * r12) / det
    c2 = max(-0.999, min(0.999, c2))             # stable: |c2| < 1 and |c1| < 1 - c2
    lim = 1.0 - c2 - 0.001
    return max(-lim, min(lim, c1)), c2


# starting points for the fit: silence, gentle and steep low-pass shapes
_SEEDS = ((0.0, 0.0), (1.0, 0.0), (1.9, -0.92), (1.6, -0.7), (1.2, -0.35), (0.6, 0.0), (1.95, -0.97),
          (0.3, 0.2))


def coefficients(x, max_frames=24000, rounds=8):
    """Eight predictor pairs fitted to channel `x`, as 16 int16 values. Lloyd iteration: each
    frame goes to the predictor that predicts it best, each predictor is refitted to its frames."""
    frames = len(x) // FRAME_SAMPLES
    step = max(1, frames // max_frames)
    stats = [st for st in (_frame_stats(x, f * FRAME_SAMPLES) for f in range(0, frames, step)) if st[0] > 0]
    preds = list(_SEEDS)
    for _ in range(rounds if stats else 0):
        sums = [[0.0] * 6 for _ in preds]
        used = [0] * len(preds)
        for st in stats:
            best = min(range(len(preds)), key=lambda k: _energy(st, *preds[k]))
            acc = sums[best]
            for j in range(6):
                acc[j] += st[j]
            used[best] += 1
        preds = [_solve(sums[k]) if used[k] else p for k, p in enumerate(preds)]
    out = []
    for c1, c2 in preds:
        out += [max(-32768, min(32767, int(round(c1 * 2048)))), max(-32768, min(32767, int(round(c2 * 2048))))]
    return out


# --- encoding --------------------------------------------------------------------------
def _quantize(frame, c1, c2, sh, h1, h2):
    """Closed loop, as the decoder will see it: (residuals, squared error, clipped, hist1, hist2)."""
    half = (1 << sh) >> 1
    nibs = []
    err = 0
    clipped = False
    for x in frame:
        p = 1024 + c1 * h1 + c2 * h2
        n = (x - (p >> 11) + half) >> sh
        if n > 7:
            n, clipped = 7, True
        elif n < -8:
            n, clipped = -8, True
        s = (((n << sh) << 11) + p) >> 11
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        err += (x - s) * (x - s)
        nibs.append(n)
        h2 = h1
        h1 = s
    return nibs, err, clipped, h1, h2


def encode(x, coefs):
    """Channel `x` (int16 samples) -> frame bytes. Each frame takes the predictor that predicts it
    best and the smallest scale its residual fits; one scale up is tried when that clips."""
    x = array("h", x)                       # 2 bytes a sample: a list would take ~36
    x.extend(bytes((-len(x)) % FRAME_SAMPLES))
    pairs = [(coefs[2 * k], coefs[2 * k + 1]) for k in range(8)]
    fpairs = [(c1 / 2048.0, c2 / 2048.0) for c1, c2 in pairs]
    out = bytearray()
    h1 = h2 = 0
    for start in range(0, len(x), FRAME_SAMPLES):
        frame = x[start:start + FRAME_SAMPLES]
        st = _frame_stats(x, start)
        if st[0] == 0 and h1 == 0 and h2 == 0:
            out += bytes(FRAME_BYTES)
            continue
        k = min(range(8), key=lambda j: _energy(st, *fpairs[j]))
        c1, c2 = pairs[k]
        p1 = h1 if start == 0 else x[start - 1]
        p2 = h2 if start < 2 else x[start - 2]
        peak = 0
        for v in frame:
            r = v - ((1024 + c1 * p1 + c2 * p2) >> 11)
            if r < 0:
                r = -r
            if r > peak:
                peak = r
            p2 = p1
            p1 = v
        sh = 0
        while sh < 12 and (7 << sh) < peak:
            sh += 1
        nibs, err, clipped, n1, n2 = _quantize(frame, c1, c2, sh, h1, h2)
        if clipped and sh < 12:
            alt = _quantize(frame, c1, c2, sh + 1, h1, h2)
            if alt[1] < err:
                nibs, err, clipped, n1, n2 = alt
                sh += 1
        h1, h2 = n1, n2
        out.append((k << 4) | sh)
        for j in range(0, FRAME_SAMPLES, 2):
            out.append(((nibs[j] & 15) << 4) | (nibs[j + 1] & 15))
    return bytes(out)


# --- TS2's .mss container --------------------------------------------------------------
# Two standard 0x60-byte DSP channel headers, then the two channels interleaved in 0x1000-byte
# blocks (left, right, left, ...), each padded to whole blocks. The game streams 0x2000 bytes
# (a block per channel) at a time and loops by going back to 0xC0, so a file is always
# 0xC0 + n * 0x2000 bytes. It reads the rate from the header; every game track is 32000 Hz.
MSS_RATE = 32000
MSS_BLOCK = 0x1000
MSS_HEADER = 0xC0
# The header padding of every game track carries these bytes; written the same to be safe.
_HEADER_TAIL = bytes.fromhex("530060a9400064ff12") + bytes(13)


def channel_header(samples, coefs, first_ps, rate=MSS_RATE):
    nib = nibble_count(samples)
    h = struct.pack(">IIIHHIII", samples, nib, rate, 0, 0, 2, nib, 2)
    h += struct.pack(">16h", *coefs)
    h += struct.pack(">HHhhHhh", 0, first_ps, 0, 0, 0, 0, 0)
    return h + _HEADER_TAIL


def build_mss(left, right):
    """Two channels of 32 kHz int16 samples -> a complete .mss file."""
    samples = min(len(left), len(right))
    chans = []
    for ch in (left, right):
        ch = ch[:samples]
        co = coefficients(ch)
        chans.append((co, encode(ch, co)))
    blocks = max(1, -(-max(len(d) for _, d in chans) // MSS_BLOCK))
    data = [d + bytes(blocks * MSS_BLOCK - len(d)) for _, d in chans]
    out = bytearray()
    for co, d in chans:
        out += channel_header(samples, co, d[0] if d else 0)
    for b in range(blocks):
        for d in data:
            out += d[b * MSS_BLOCK:(b + 1) * MSS_BLOCK]
    return bytes(out)


def mss_info(head, size):
    """(samples, rate) if `head` (the first 0xC0 bytes) and `size` describe a file the game can
    stream, else None."""
    if len(head) < MSS_HEADER or size < MSS_HEADER + 2 * MSS_BLOCK or (size - MSS_HEADER) % (2 * MSS_BLOCK):
        return None
    a = struct.unpack(">III", head[0:12])
    b = struct.unpack(">III", head[0x60:0x6C])
    if a[0] == 0 or a[2] != MSS_RATE or b[2] != MSS_RATE or a[1] != nibble_count(a[0]):
        return None
    if (a[0] + FRAME_SAMPLES - 1) // FRAME_SAMPLES * FRAME_BYTES > (size - MSS_HEADER) // 2:
        return None
    return a[0], a[2]


def parse_mss(blob):
    """(samples, [(coefs, frame bytes)] for both channels) of an .mss file."""
    samples = struct.unpack(">I", blob[:4])[0]
    body = blob[MSS_HEADER:]
    chans = []
    for c in (0, 1):
        co = list(struct.unpack(">16h", blob[c * 0x60 + 0x1C:c * 0x60 + 0x3C]))
        data = b"".join(body[k + c * MSS_BLOCK:k + (c + 1) * MSS_BLOCK] for k in range(0, len(body), 2 * MSS_BLOCK))
        chans.append((co, data))
    return samples, chans


def decode_mss(blob):
    """[left, right] sample arrays of an .mss file."""
    samples, chans = parse_mss(blob)
    return [decode(d, co, samples) for co, d in chans]
