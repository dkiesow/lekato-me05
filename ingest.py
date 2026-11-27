#!/usr/bin/env python3
"""ingest.py - analyse a reference recording (a guitar solo or part) and derive a starting preset for the pedal.

  ingest.py FILE [--start S] [--end S] [--name NAME] [--separate] [--slot N]

FILE is your own copy of the track, any format ffmpeg reads. Trim to the guitar part with --start/--end
(seconds). --separate first pulls the guitar out of the mix with Demucs (model htdemucs_6s, which has a guitar
part); it must be installed (python3 -m pip install demucs, about 2 GB with PyTorch) and takes a minute or two
per minute of audio. Without it the whole mix is analysed, and bass, drums and vocals bias every estimate.

Writes capture/ingest/NAME.json (the measurements) and capture/ingest/NAME_preset.json (lekato.py apply
format; "slot" set if --slot is given). Apply it with: lekato.py apply NAME_preset.json --slot N

What it measures, from the reference alone:
  spectrum    octave-band shape, centroid and tilt (dB per octave), as tone.py match
  drive       how fast held notes and chords decay (drive and compression hold them up), and, on single notes,
              how fast the harmonic series falls off, in dB per octave of harmonic number; odd-over-even balance is
              reported, not used
  modulation  a steady 0.5-12 Hz wobble in the band levels that the pitch does not share (finger vibrato moves the
              pitch; a Uni-Vibe, phaser or tremolo moves the level). Same phase in all bands = tremolo; differing
              phase = vibe or phaser
  delay       a note's own harmonics rising again a fixed 60-1000 ms after its onset (the next note of a line has
              other harmonics); the repeat's level, and the second repeat's level relative to it (feedback)
  reverb      RT60 (seconds to fall 60 dB) from the tails of note endings that fall into a gap

How the preset is derived: fixed rules, each written into the preset's "notes". Model choice follows the drive
class; amp treble and presence follow the tilt; Master is fixed at 30, so loudness is balanced with preset volume; EQ and cab stay off, for the tuning
loop to set. The drive thresholds are provisional until this rig has been measured with them, and three knob
mappings are assumptions, not measurements: Mod Speed (0-100 taken as 0-10 Hz), Delay Time (0-100 taken as
0-1000 ms) and Reverb Decay.
"""
import argparse, json, os, subprocess, sys, tempfile
import numpy as np
from scipy.signal import butter, sosfiltfilt
import tone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "capture", "ingest")
SR = 48000
FPS = 100                                                        # analysis frame rate for envelopes and pitch
HOP = SR // FPS
LEVEL_BANDS = [(200, 500), (500, 1200), (1200, 3000), (3000, 6000)]


def clamp(v, lo=0, hi=100):
    return int(round(min(hi, max(lo, v))))


# ---------------------------------------------------------------- input

def separate(path, start, end):
    """Guitar stem from Demucs, for the trimmed span only. Returns the stem's path."""
    try:
        import demucs  # noqa: F401
    except ImportError:
        sys.exit("--separate needs Demucs: python3 -m pip install demucs (about 2 GB with PyTorch)")
    tmp = tempfile.mkdtemp(prefix="ingest_")
    clip = os.path.join(tmp, "clip.wav")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += (["-ss", str(start)] if start is not None else []) + (["-to", str(end)] if end is not None else [])
    subprocess.run(cmd + ["-i", path, "-ar", "44100", clip], check=True)
    subprocess.run([sys.executable, "-m", "demucs", "-n", "htdemucs_6s", "--two-stems", "guitar", "-o", tmp, clip], check=True)
    stem = os.path.join(tmp, "htdemucs_6s", "clip", "guitar.wav")
    if not os.path.exists(stem):
        sys.exit("Demucs ran but wrote no guitar stem under %s" % tmp)
    return stem


def sounding(x):
    """Per-frame dB level at FPS, and a mask of the frames within 30 dB of the loud end."""
    n = len(x) // HOP
    lv = tone.db(np.mean(x[:n * HOP].reshape(n, HOP) ** 2, axis=1))
    return lv, lv > np.percentile(lv, 95) - 30


# ---------------------------------------------------------------- pitch

def yin(x, n=2048, fmin=80, fmax=1200, thresh=0.15):
    """f0 per frame at FPS (0 where unpitched), by the YIN difference function."""
    tmin, tmax = int(SR / fmax), int(SR / fmin)
    out = np.zeros(len(x) // HOP)
    size = 1 << int(np.ceil(np.log2(2 * n + tmax)))
    for k in range(len(out)):
        i = k * HOP
        fr = x[i:i + n + tmax]
        if len(fr) < n + tmax or not fr.any():
            continue
        a = fr[:n]
        r = np.fft.irfft(np.conj(np.fft.rfft(a, size)) * np.fft.rfft(fr, size), size)[:tmax + 1]
        c = np.concatenate([[0], np.cumsum(fr ** 2)])
        e0 = c[n]
        et = c[np.arange(tmax + 1) + n] - c[np.arange(tmax + 1)]
        d = e0 + et - 2 * r
        d[0] = 0
        cm = d[1:] * np.arange(1, tmax + 1) / np.maximum(np.cumsum(d[1:]), 1e-20)
        cm = np.concatenate([[1], cm])
        cand = np.where(cm[tmin:tmax] < thresh)[0]
        if not len(cand):
            continue
        t = tmin + cand[0]
        while t + 1 < tmax and cm[t + 1] < cm[t]:
            t += 1
        out[k] = SR / t
    return out


# ---------------------------------------------------------------- drive

def held_runs(f0, mask, min_s=0.3):
    """(start, end) frame pairs where the pitch holds within 50 cents for min_s seconds or longer."""
    runs, k = [], 0
    while k < len(f0):
        if f0[k] > 0 and mask[k]:
            j = k
            while j + 1 < len(f0) and f0[j + 1] > 0 and mask[j + 1] and abs(1200 * np.log2(f0[j + 1] / f0[k])) < 50:
                j += 1
            if (j - k + 1) / FPS >= min_s:
                runs.append((k, j + 1))
            k = j + 1
        else:
            k += 1
    return runs


def drive(x, f0, mask):
    """Over the pitched, sounding frames: how fast the harmonics fall off (dB per octave of harmonic number,
    using the louder of each odd/even pair, so a square-ish wave's missing even harmonics do not count against
    it), the odd-over-even balance, and the level slope of held notes (drive and compression hold notes up)."""
    idx = np.where((f0 > 0) & mask)[0]
    if len(idx) < 20:                                            # chords only: no harmonic series to read; sustain still works
        lv, _ = sounding(x)
        held = [float(np.polyfit(np.arange(b - a) / FPS, lv[a:b], 1)[0]) for a, b in stretches(x, mask, 0.3)]
        return {"harmonic_slope_db_per_oct": None, "odd_minus_even_db": None,
                "held_decay_db_per_s": round(float(np.median(held)), 1) if held else None, "pitched_frames": int(len(idx)), "held_notes": len(held)}
    idx = idx[np.linspace(0, len(idx) - 1, min(400, len(idx))).astype(int)]
    n = 8192
    freqs = np.fft.rfftfreq(n, 1 / SR)
    slopes, oddeven = [], []
    for k in idx:
        c = k * HOP + 1024
        seg = x[max(0, c - n // 2):c + n // 2]
        if len(seg) < n:
            continue
        spec = np.abs(np.fft.rfft(seg * np.hanning(n))) ** 2
        f = f0[k]
        hs = [h for h in range(1, 21) if f * h < 8000]
        if len(hs) < 6:
            continue
        e = []
        for h in hs:
            w = max(f * h * 0.03, 2 * SR / n)
            m = (freqs > f * h - w) & (freqs < f * h + w)
            e.append(spec[m].max())
        e = tone.db(np.array(e))
        pairs = [(2 * p + 1, max(e[2 * p], e[2 * p + 1])) for p in range(len(e) // 2)]   # (h, level) for h=1-2, 3-4, ...
        hh = np.array([p[0] + 0.5 for p in pairs]); ll = np.array([p[1] for p in pairs])
        if len(hh) >= 3:
            slopes.append(float(np.polyfit(np.log2(hh), ll, 1)[0]))
        odd = [e[h - 1] for h in (3, 5, 7, 9) if h <= len(hs)]
        even = [e[h - 1] for h in (2, 4, 6, 8) if h <= len(hs)]
        if odd and even:
            oddeven.append(float(np.mean(odd) - np.mean(even)))
    lv, _ = sounding(x)
    held = [float(np.polyfit(np.arange(b - a) / FPS, lv[a:b], 1)[0]) for a, b in stretches(x, mask, 0.3)]
    return {"harmonic_slope_db_per_oct": round(float(np.median(slopes)), 1) if slopes else None,
            "odd_minus_even_db": round(float(np.median(oddeven)), 1) if oddeven else None,
            "held_decay_db_per_s": round(float(np.median(held)), 1) if held else None,
            "pitched_frames": int(len(idx)), "held_notes": len(held)}


def drive_class(d):
    """Sustain first (a clean note dies away; drive holds it up), then how flat the harmonic series is."""
    if not d or d["held_decay_db_per_s"] is None:
        return "unknown"
    s, g = d["held_decay_db_per_s"], d["harmonic_slope_db_per_oct"]
    if g is None:                                                # chords only: sustain alone
        g = -20 if s < -2 else -10
    if s < -15:
        return "clean"
    if s < -8:
        return "edge"
    if s < -2:
        return "high" if g > -15 else "crunch"
    return "high"


# ---------------------------------------------------------------- modulation

def band_levels(x):
    n = 2048
    frames = np.array([x[i:i + n] for i in range(0, len(x) - n, HOP)])
    spec = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / SR)
    return np.array([tone.db(spec[:, (freqs >= a) & (freqs < b)].sum(axis=1)) for a, b in LEVEL_BANDS])


RATES = np.arange(1.0, 12.01, 0.1)


def fit_sines(sig):
    """Least-squares sinusoid at each candidate rate on a detrended stretch: complex amplitude (dB peak) per rate."""
    t = np.arange(len(sig)) / FPS
    r = sig - np.polyval(np.polyfit(t, sig, 2), t)             # quadratic: a note's decay is not a straight line in dB
    return np.array([2 * np.mean(r * np.exp(-2j * np.pi * f * t)) for f in RATES])


def onsets(x):
    """Frames (at FPS) where a note or chord starts: the level above 200 Hz rises 4 dB or more within 20 ms. A tremolo,
    vibe or Leslie rises far more slowly (a 6 Hz, 7 dB wobble: about 1.3 dB in 20 ms), so it does not count."""
    n = len(x) // HOP
    y = sosfiltfilt(butter(4, 200, "hp", fs=SR, output="sos"), x)
    lv = tone.db(np.mean(y[:n * HOP].reshape(n, HOP) ** 2, axis=1))
    rise = np.concatenate([[0, 0], lv[2:] - lv[:-2]])
    return [t for t in range(1, n - 1) if rise[t] >= 4 and rise[t] >= rise[t - 1] and rise[t] > rise[t + 1]]


def stretches(x, mask, min_s=0.5):
    """Sounding stretches with no new note or chord starting, 0.5 s or longer, each starting 50 ms after its onset
    (past the attack). These are held notes and held chords alike; the pitch tracker is not needed."""
    on = [t for t in onsets(x) if t < len(mask)] + [len(mask)]
    runs = []
    for a, b in zip(on[:-1], on[1:]):
        a += 5
        while a < b and not mask[a]:
            a += 1
        e = a
        while e < b and mask[e]:
            e += 1
        if (e - a) / FPS >= min_s:
            runs.append((a, e))
    return runs


def modulation(x, f0, mask):
    """Inside held notes and chords only, so note changes do not count: a level wobble at one rate, which the pitch
    does not share. A stretch must last three cycles to count for a rate, so the slowest rate seen depends on the
    longest held stretches (1.5 s: 2 Hz). Finger vibrato wobbles the level too; it is told apart by its pitch wobble,
    so the level wobble must also be there on stretches whose pitch is steady. A chord has no single pitch; it counts
    as steady (finger vibrato on a whole chord is rare)."""
    bl = band_levels(x)
    n = min(bl.shape[1], len(f0), len(mask))
    runs = [(a, min(b, n)) for a, b in stretches(x, mask[:n]) if min(b, n) - a >= 0.5 * FPS]
    if len(runs) < 3:
        return {"detected": None, "held_notes": len(runs)}
    lvl, pit, w, chords = [], [], [], 0
    for a, b in runs:
        ok = RATES * (b - a) / FPS >= 3
        lvl.append([fit_sines(band[a:b]) for band in bl])
        fr = f0[a:b]
        voiced = fr > 0
        if voiced.mean() >= 0.7 and np.all(np.abs(1200 * np.log2(fr[voiced] / np.median(fr[voiced]))) < 100):
            cents = np.where(voiced, 1200 * np.log2(np.maximum(fr, 1) / np.median(fr[voiced])), 0)
            pit.append(fit_sines(cents))
        else:
            pit.append(np.zeros(len(RATES), complex))
            chords += 1
        w.append(ok)
    w = np.array(w, float)
    cnt = np.maximum(w.sum(axis=0), 1)
    lvl = np.array(lvl)                                          # stretches x bands x rates (complex)
    lev_amp = (np.abs(lvl).mean(axis=1) * w).sum(axis=0) / cnt   # dB, averaged over bands and stretches
    pit_amp = (np.abs(np.array(pit)) * w).sum(axis=0) / cnt      # cents
    k = int(np.argmax(np.where(cnt >= 3, lev_amp, 0)))
    rate, depth = float(RATES[k]), float(2 * lev_amp[k])          # peak-to-peak dB
    prom = float(lev_amp[k] / np.median(lev_amp))
    # tremolo moves every band together; a vibe, phaser or Leslie moves them at different phases
    ph = np.angle(np.sum(lvl[:, -1, k] * np.conj(lvl[:, 0, k]) * w[:, k]))
    # an effect wobbles every stretch at its rate; beating between the notes of a chord wobbles each chord at its own
    # rate. Share = the stretches whose own wobble at this rate is at least twice their typical wobble.
    per = np.abs(lvl).mean(axis=1)
    elig = w[:, k] > 0
    share = float(np.mean(per[elig, k] >= 2 * np.median(per[elig], axis=1))) if elig.any() else 0.0
    note_pitch = np.abs(np.array(pit))[:, k]
    note_level = np.abs(lvl).mean(axis=1)[:, k]
    steady = (note_pitch < 4) & (w[:, k] > 0)
    on_steady = float(note_level[steady].mean() / lev_amp[k]) if steady.sum() >= 3 else None
    detected = None
    if depth >= 2 and (prom >= 3.5 or share >= 0.5) and (on_steady >= 0.6 if on_steady is not None else pit_amp[k] < 3):
        detected = "tremolo" if abs(ph) < 0.6 else "vibe"
    return {"rate_hz": round(rate, 1), "depth_db": round(depth, 1), "prominence": round(prom, 1),
            "pitch_cents": round(float(pit_amp[k]), 1), "band_phase_rad": round(float(ph), 2),
            "steady_pitch_notes": int(steady.sum()), "on_steady_notes": round(on_steady, 2) if on_steady is not None else None,
            "share": round(share, 2), "held_notes": len(runs), "chords": chords, "detected": detected}


# ---------------------------------------------------------------- delay

def delay(x, mod_rate=None):
    """An echo repeats an onset's own spectrum a fixed time later; the next note or chord of a line has another
    spectrum. For the strongest onsets, the rise in energy tau later is projected onto the onset's own rise (80 Hz -
    5 kHz) and taken relative to it, then averaged for each tau from 60 to 1000 ms. A peak is the repeat; its height
    is the echo level. Works for single notes and chords; a part that repeats one chord or note on a steady beat
    can look like an echo at the beat."""
    n, fps = 2048, 200
    hop = SR // fps
    frames = np.array([x[i:i + n] for i in range(0, len(x) - n, hop)])
    freqs = np.fft.rfftfreq(n, 1 / SR)
    band = (freqs >= 80) & (freqs <= 5000)
    mag = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1))[:, band]
    flux = np.vstack([np.zeros(mag.shape[1]), np.maximum(np.diff(mag, axis=0), 0)])
    tot = flux.sum(axis=1)
    lags = np.arange(int(0.06 * fps), int(1.0 * fps))
    if mod_rate:                                                 # a tremolo or vibe also makes the spectrum rise once per cycle
        per = fps / mod_rate
        lags = np.array([L for L in lags if min(abs(L - m * per) for m in range(1, 8)) > 0.2 * per])
    peaks = [t for t in range(1, len(tot) - 1) if tot[t] >= tot[t - 1] and tot[t] > tot[t + 1]]
    peaks = [t for t in sorted(peaks, key=lambda t: -tot[t])[:60] if t + 2 * lags[-1] + 1 < len(tot)]
    rows = []
    for t in peaks:
        v = flux[t]
        vv = float(v @ v)
        if vv <= 0:
            continue
        rows.append([max(float(flux[t + L + d] @ v) for d in (-1, 0, 1)) / vv for L in np.concatenate([lags, 2 * lags])])
    if len(rows) < 5:
        return {"detected": False, "onsets": len(rows)}
    g = np.median(np.array(rows), axis=0)
    g1, g2 = g[:len(lags)], g[len(lags):]
    # A repeat is a narrow peak standing above its neighbours. Comparing with the neighbours (5-15 steps either
    # side) rather than the whole curve matters for chords, whose spectra overlap and raise every lag, and it
    # rejects the shortest lags, where an onset's own attack is still rising (a peak needs neighbours on both sides).
    best = None
    for k in range(15, len(g1) - 15):
        if g1[k] < 0.1 or g1[k] < g1[k - 1] or g1[k] < g1[k + 1]:
            continue
        nb = np.concatenate([g1[k - 15:k - 4], g1[k + 5:k + 16]])
        p = float(g1[k] / max(np.mean(nb), 1e-9))
        if best is None or p > best[0]:
            best = (p, k)
    if best is None:
        return {"detected": False, "onsets": len(rows), "time_ms": 0, "level": 0.0, "feedback": 0.0, "prominence": 0.0}
    prom, k = best
    level = float(g1[k])
    overall = float(level / max(np.median(g1), 1e-9))
    fb = float(g2[k] / level) if level > 0 else 0
    return {"time_ms": int(round(lags[k] / fps * 1000)), "level": round(level, 2), "feedback": round(min(fb, 1.0), 2),
            "prominence": round(prom, 1), "onsets": len(rows),
            "overall": round(overall, 1),
            # a narrow peak, of a real size (an echo 12 dB down or louder), standing out from the whole curve as well;
            # a tremolo or vibe raises every lag a little, so ask more of the peak then
            "detected": bool(level >= 0.25 and overall >= 2 and prom >= (2.5 if mod_rate else 1.8))}


# ---------------------------------------------------------------- reverb

def reverb(x):
    """RT60 from note endings that fall into a gap: runs of falling level that drop 20 dB or more. The first 10 dB of
    each is skipped (the note itself stopping); the slope of the rest is the tail. Median over those tails."""
    lv, mask = sounding(x)
    lv = np.convolve(lv, np.ones(5) / 5, mode="same")             # 50 ms: a reverb tail is noise-like and flickers
    tails, k = [], 1
    while k < len(lv) - 1:
        if lv[k] < lv[k - 1] and mask[k - 1]:
            st = k - 1
            j = k
            while j + 1 < len(lv) and lv[j + 1] <= lv[j] + 1.5:
                j += 1
            if lv[st] - lv[j] >= 20:
                tail = np.arange(st, j + 1)[lv[st:j + 1] <= lv[st] - 10]
                tail = tail[lv[tail] >= lv[j] + 3]                    # stop short of the floor
                if len(tail) >= 3:
                    tails.append(float(np.polyfit(tail / FPS, lv[tail], 1)[0]))
                else:
                    tails.append(-600.0)                             # fell to the floor within 30 ms: dry
            k = j + 1
        else:
            k += 1
    if len(tails) < 3:
        return {"rt60_s": None, "tails": len(tails)}
    return {"rt60_s": round(min(60 / abs(float(np.median(tails))), 10), 2), "tails": len(tails)}


# ---------------------------------------------------------------- preset

def derive(feat, name):
    notes, blocks = [], {}
    sp, d, md, dl, rv = feat["spectrum"], feat["drive"], feat["modulation"], feat["delay"], feat["reverb"]
    cls = drive_class(d)
    tilt = sp["tilt"]
    treble = clamp(50 + 4 * (tilt + 6), 30, 70)
    presence = clamp(treble - 10, 25, 60)
    amp = {"clean": ("Black Twin", 15), "edge": ("Voxy 30HW", 40), "crunch": ("Mar Plexi100", 50),
           "high": ("Mar Plexi100", 45), "unknown": ("Mar Plexi100", 40)}[cls]
    blocks["Amp"] = {"on": True, "model": amp[0], "knobs": {"Gain": amp[1], "Bass": 50, "Middle": 55, "Treble": treble,
                                                         "Presence": presence, "Master": 30}}
    notes.append("drive class %s (held notes %s dB/s, harmonics %s dB/octave); amp %s at gain %d" % (
        cls, (d or {}).get("held_decay_db_per_s"), (d or {}).get("harmonic_slope_db_per_oct"), amp[0], amp[1]))
    notes.append("tilt %+.1f dB/octave -> treble %d, presence %d (presence kept 10 below treble: cab off is fizzy)" % (tilt, treble, presence))
    g = (d or {}).get("harmonic_slope_db_per_oct") or -20
    pick = feat.get("drive_model")
    if cls == "high" and (pick or g > -11):
        pick = pick or "Face Fuzz"
        defaults = {"Face Fuzz": {"Gain": 45, "Tone": 50, "Output": 50}, "Muff": {"Volume": 50, "Tone": 50, "Sustain": 45},
                    "TS9": {"Drive": 35, "Tone": 50, "Level": 65}}
        blocks["OD-FX"] = {"on": True, "model": pick, "knobs": defaults.get(pick, {})}
        notes.append("harmonics fall only %.1f dB/octave: %s into the amp%s" % (g, pick, " (chosen with --drive-model)" if feat.get("drive_model") else ""))
    elif cls == "high":
        blocks["OD-FX"] = {"on": True, "model": "TS9", "knobs": {"Drive": 35, "Tone": 50, "Level": 65}}
        notes.append("high drive, harmonics %.1f dB/octave: overdrive (TS9) into the amp" % g)
    else:
        blocks["OD-FX"] = {"on": False}
    blocks["Cab"] = {"on": False}
    blocks["EQ"] = {"on": False}
    blocks["Gate"] = {"on": True, "model": "Noise gate", "knobs": {"Threshold": {"clean": 26, "edge": 26, "crunch": 30}.get(cls, 32), "Decay": 40}}
    if md["detected"] == "vibe":
        blocks["Mod"] = {"on": True, "model": "Uni Vibe", "knobs": {"Speed": clamp(md["rate_hz"] * 10), "Volume": 78,
                                                                   "Intensity": clamp(md["depth_db"] * 10, 20, 80), "Mode": 0}}
        notes.append("level wobble %.2f Hz, %.1f dB, bands out of phase: Uni Vibe; Speed from 0-100 = 0-10 Hz (assumed)" % (md["rate_hz"], md["depth_db"]))
    elif md["detected"] == "tremolo":
        blocks["Mod"] = {"on": True, "model": "Tremolo", "knobs": {"Depth": clamp(md["depth_db"] * 8, 10, 90), "Rate": clamp(md["rate_hz"] * 10)}}
        notes.append("level wobble %.2f Hz, %.1f dB, bands in phase: Tremolo; Rate from 0-100 = 0-10 Hz (assumed)" % (md["rate_hz"], md["depth_db"]))
    else:
        blocks["Mod"] = {"on": False}
    if dl["detected"]:
        blocks["Delay"] = {"on": True, "model": "Tape Delay", "knobs": {"Time": clamp(dl["time_ms"] / 10), "Feedback": clamp((dl["feedback"] or 0.2) * 100, 0, 60),
                                                                       "Level": clamp((dl["level"] or 0.3) * 100, 10, 50)}}
        notes.append("repeat at %d ms: Tape Delay; Time from 0-100 = 0-1000 ms (assumed)" % dl["time_ms"])
    else:
        blocks["Delay"] = {"on": False}
    rt = rv["rt60_s"]
    if rt is None or rt < 0.4:
        blocks["Reverb"] = {"on": True, "model": "Spring", "knobs": {"Mix": 10, "Decay": 20}}
    elif rt < 1.0:
        blocks["Reverb"] = {"on": True, "model": "Spring", "knobs": {"Mix": 20, "Decay": clamp(20 + rt * 30)}}
    else:
        blocks["Reverb"] = {"on": True, "model": "Hall", "knobs": {"Decay": clamp(rt * 30, 30, 80), "Tone": 50, "Pre Delay (ms)": 20, "Level": 25}}
    notes.append("RT60 %s s -> %s" % (rt, blocks["Reverb"]["model"]))
    return {"name": name[:16], "target": "derived by ingest.py from %s" % feat["file"], "notes": notes, "volume": 80,
            "order": ["Gate", "OD-FX", "Amp", "Cab", "EQ", "Mod", "Delay", "Reverb"], "blocks": blocks}


# ---------------------------------------------------------------- main

def analyse(path, start=None, end=None):
    x, _ = tone.load_audio(path, start, end, SR)
    x = sosfiltfilt(butter(4, 60, "hp", fs=SR, output="sos"), x)
    _, mask = sounding(x)
    f0 = yin(x)
    freqs, spec, secs = tone.ltas(x, SR)
    md = modulation(x, f0, mask)
    sh = tone.shape(freqs, spec)
    return {"seconds_of_sound": round(secs, 1), "pitched_share": round(float(np.mean((f0[:len(mask)] > 0)[mask[:len(f0)]])), 2),
            "spectrum": {"octaves": {str(k): round(v, 1) for k, v in sh["octaves"].items()}, "centroid": sh["centroid"], "tilt": sh["tilt"]},
            "drive": drive(x, f0, mask), "modulation": md, "delay": delay(x, md.get("rate_hz") if md.get("detected") else None), "reverb": reverb(x)}


def show(feat, preset):
    sp, d, md, dl, rv = feat["spectrum"], feat["drive"] or {}, feat["modulation"], feat["delay"], feat["reverb"]
    print("%s%s: %.0f s of sound, %d%% of it pitched" % (feat["file"], " (guitar stem)" if feat["separated"] else " (not separated)",
                                                         feat["seconds_of_sound"], 100 * feat["pitched_share"]))
    print("  spectrum    centroid %d Hz, tilt %+.1f dB/octave; octaves %s" % (sp["centroid"], sp["tilt"], " ".join("%s:%+.0f" % kv for kv in sp["octaves"].items())))
    print("  drive       %s: held notes %s dB/s (%s notes), harmonics %s dB/octave, odd-even %s dB" % (
        drive_class(feat["drive"]), d.get("held_decay_db_per_s"), d.get("held_notes"), d.get("harmonic_slope_db_per_oct"), d.get("odd_minus_even_db")))
    if "rate_hz" in md:
        print("  modulation  %s: %.1f Hz, %.1f dB peak-to-peak, prominence %.1f, share %.2f, pitch %.1f cents, band phase %.2f rad (%d held stretches, %d of them chords)" % (
            md["detected"] or "none", md["rate_hz"], md["depth_db"], md["prominence"], md["share"], md["pitch_cents"], md["band_phase_rad"], md["held_notes"], md["chords"]))
    else:
        print("  modulation  not measured: %d held notes or chords of 0.5 s or longer" % md["held_notes"])
    if "time_ms" in dl:
        print("  delay       %s: %d ms, level %.2f, feedback %.2f, prominence %.1f (%d onsets)" % (
            "yes" if dl["detected"] else "none", dl["time_ms"], dl["level"], dl["feedback"], dl["prominence"], dl["onsets"]))
    else:
        print("  delay       not measured: %d usable onsets" % dl["onsets"])  # noqa
    print("  reverb      RT60 %s s from %d note endings" % (rv["rt60_s"], rv["tails"]))
    print("preset %r:" % preset["name"])
    for b, s in preset["blocks"].items():
        print("  %-7s %s" % (b, ("%s  %s" % (s["model"], ", ".join("%s %s" % kv for kv in s["knobs"].items()))) if s.get("on") else "off"))
    for n in preset["notes"]:
        print("  - " + n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--start", type=float)
    ap.add_argument("--end", type=float)
    ap.add_argument("--name", help="preset name (16 characters) and output file name; default from the file")
    ap.add_argument("--separate", action="store_true", help="isolate the guitar with Demucs first")
    ap.add_argument("--slot", type=int, help="slot to put in the preset file")
    ap.add_argument("--drive-model", help="OD/FX model to use when the drive is high, e.g. \"Face Fuzz\", Muff, TS9")
    a = ap.parse_args()
    name = a.name or os.path.splitext(os.path.basename(a.file))[0]
    if a.separate:
        feat = analyse(separate(a.file, a.start, a.end))
    else:
        feat = analyse(a.file, a.start, a.end)
    feat.update({"file": os.path.basename(a.file), "start": a.start, "end": a.end, "separated": a.separate, "drive_model": a.drive_model})
    preset = derive(feat, name)
    if a.slot is not None:
        preset["slot"] = a.slot
    os.makedirs(OUT, exist_ok=True)
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    json.dump(feat, open(os.path.join(OUT, stem + ".json"), "w"), indent=1)
    json.dump(preset, open(os.path.join(OUT, stem + "_preset.json"), "w"), indent=1)
    show(feat, preset)


if __name__ == "__main__":
    main()
