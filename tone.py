#!/usr/bin/env python3
"""tone.py - record open strings through a microphone and measure the tone of each note.

  tone.py record NAME [--plucks 3] [--seconds S] [--mic DEVICE] [--cal FILE]                     record, then analyse
  tone.py analyse FILE.wav [--plucks 3] [--cal FILE]                                             analyse an existing recording
  tone.py compare NAME_A NAME_B                                                                  per-string differences between two takes
  tone.py part NAME --ref REF [--seconds 30] [--mic ...] [--cal FILE] [--ref-start S] [--ref-end S]   record a played part, then match
  tone.py match TAKE REF [--cal FILE] [--take-start S] [--take-end S] [--ref-start S] [--ref-end S]   compare a take with a reference

Play the six open strings low E to high E, each string --plucks times in a row (default 3): pluck, let it
ring two seconds, damp it, half a second of silence, pluck again. The plucks of each string are averaged,
because single plucks at the same settings differ by several dB per band. Takes made before averaging
existed are one pluck per string: analyse them with --plucks 1. The recording length defaults to
10 s + 3.5 s per pluck (73 s for three plucks).
Needs ffmpeg, numpy and scipy. Recordings and results go to capture/audio/ (not committed). Recording uses
AVFoundation on macOS and ALSA on Linux; --mic (or the TONE_MIC environment variable) names the input device.

--cal FILE corrects the spectrum for the microphone's own response, using the calibration file that comes
with a measurement microphone (Dayton UMM-6, miniDSP UMIK-1: text lines of "frequency dB [phase]"; header
and comment lines are skipped). Use the 0-degree file with the microphone pointed at the speaker. The
correction applies to bands, centroid and upper; level stays raw dBFS. Compare takes only when both used
the same calibration, or neither did.

Reference matching (part, match). REF is a recording of the sound being aimed for: any file ffmpeg reads
(wav, mp3, m4a, flac). Best is an isolated guitar track; from a full mix, pick a stretch where the guitar is
alone or loudest with --ref-start/--ref-end, or separate the guitar first (Demucs, on the Mac). For the take,
play the same part as the reference, not open strings. Both recordings are reduced to their long-term average
spectrum over the sounding frames (silence dropped), each normalised to its own total from 80 Hz to 10 kHz, so
only the shape is compared, not loudness. The differences are then centred on the median EQ band, so a brighter
reference reads as "raise the top" rather than "cut everything below and raise the top a little". It reports:
  octaves   reference minus take in each octave band, dB; positive = the reference has more there
  eq        the same difference at the pedal's six EQ frequencies, one octave wide: the change that would move
            the take towards the reference (dB, not knob steps: the EQ knob's dB per step is not known)
  centroid  both centroids, and tilt: the slope of each spectrum from 100 Hz to 8 kHz in dB per octave
Not compared: drive and compression, which a full mix's mastering also changes. The mic calibration (--cal)
applies to the take only; a studio reference is already what the record sounds like.

What it reports per string (the mean over its plucks), plus "spread": the largest range, in dB, of any band
across that string's plucks. A spread above about 3 dB means the plucks disagree; replay that take.
  level     loudness of the note's first second, dB relative to full scale
  bands     share of the energy in five bands, in dB relative to the whole note
  centroid  the spectrum's centre of gravity in Hz; higher = brighter
  upper     energy in harmonics 6 and above relative to harmonics 1-5, dB; higher = more drive, fuzz or bite
  sustain   seconds for the note to fall 20 dB from its peak
"""
import argparse, json, os, subprocess, sys
import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfiltfilt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "capture", "audio")
STRINGS = [("E2", 82.41), ("A2", 110.00), ("D3", 146.83), ("G3", 196.00), ("B3", 246.94), ("E4", 329.63)]
BANDS = [("bass <200", 40, 200), ("low-mid 200-800", 200, 800), ("mid 800-2.5k", 800, 2500), ("presence 2.5-5k", 2500, 5000), ("air >5k", 5000, 16000)]


def db(x):
    return 10 * np.log10(np.maximum(x, 1e-20))


def record(name, seconds, mic):
    """Record from the microphone with ffmpeg: AVFoundation on macOS (mic = device name or index), ALSA on Linux
    (mic = ALSA device, default "default"). Without --mic, TONE_MIC is used; on macOS with neither, the devices are listed."""
    mic = mic or os.environ.get("TONE_MIC")
    if sys.platform == "darwin":
        if not mic:
            subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
            sys.exit("pick an audio device from the list above with --mic NAME (or set TONE_MIC)")
        src = ["-f", "avfoundation", "-i", ":" + mic]
    else:
        src = ["-f", "alsa", "-i", mic or "default"]
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name + ".wav")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + src + ["-t", str(seconds), "-ac", "1", "-ar", "48000", path], check=True)
    return path


EQ_FREQS = [100, 220, 500, 1200, 2500, 6400]                     # the pedal's 6-band EQ
OCTAVES = [63, 125, 250, 500, 1000, 2000, 4000, 8000]


def load_audio(path, start=None, end=None, sr=48000):
    """Decode any audio file to mono float at sr through ffmpeg, optionally trimmed to start..end seconds."""
    if start is not None and end is not None and end <= start:
        sys.exit("end must be after start")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    run = subprocess.run(cmd, capture_output=True)
    if run.returncode:
        sys.exit("ffmpeg could not read %s: %s" % (path, (run.stderr.decode(errors="replace").strip().splitlines() or ["unknown error"])[-1]))
    raw = run.stdout
    x = np.frombuffer(raw, dtype="<f4").astype(float)
    if len(x) < sr:
        sys.exit("%s: less than one second of audio" % path)
    return x, sr


def ltas(x, sr, cal=None):
    """Long-term average power spectrum of the sounding frames: 8192-point Hann frames, half overlap; frames more
    than 30 dB below the loud end (95th percentile) are silence or ring-out and are dropped."""
    x = sosfiltfilt(butter(4, 40, "hp", fs=sr, output="sos"), x)
    n, hop = 8192, 4096
    frames = np.array([x[i:i + n] for i in range(0, len(x) - n, hop)])
    lv = db(np.mean(frames ** 2, axis=1))
    keep = frames[lv > np.percentile(lv, 95) - 30]
    spec = np.mean(np.abs(np.fft.rfft(keep * np.hanning(n), axis=1)) ** 2, axis=0)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    if cal is not None:
        spec = spec * cal_gain(cal, freqs)
    spec = spec / spec[(freqs >= 80) & (freqs <= 10000)].sum()
    return freqs, spec, len(keep) * hop / sr


def shape(freqs, spec):
    """Octave and EQ-frequency levels (dB, mean power per bin over one octave), centroid and tilt."""
    def octave(fc):
        m = (freqs >= fc / 2 ** 0.5) & (freqs < fc * 2 ** 0.5)
        return float(db(spec[m].mean()))
    m = (freqs >= 80) & (freqs <= 10000)
    t = (freqs >= 100) & (freqs <= 8000)
    tilt = np.polyfit(np.log2(freqs[t]), db(spec[t]), 1)[0]
    return {"octaves": {fc: octave(fc) for fc in OCTAVES}, "eq": {fc: octave(fc) for fc in EQ_FREQS},
            "centroid": int(round(float((freqs[m] * spec[m]).sum() / spec[m].sum()))), "tilt": round(float(tilt), 1)}


def match(take_path, ref_path, cal_path=None, take_span=(None, None), ref_span=(None, None)):
    cal = load_cal(cal_path) if cal_path else None
    tx, sr = load_audio(take_path, *take_span)
    rx, _ = load_audio(ref_path, *ref_span)
    tf, ts, tsec = ltas(tx, sr, cal)
    rf, rs, rsec = ltas(rx, sr)
    T, R = shape(tf, ts), shape(rf, rs)
    # Only shape is compared, so the differences are free to shift up or down together (that shift is loudness, which
    # preset volume handles). Centre them on the median EQ band: the result needs the fewest and smallest EQ moves.
    centre = float(np.median([R["eq"][fc] - T["eq"][fc] for fc in EQ_FREQS]))
    diff = lambda k: {fc: round(R[k][fc] - T[k][fc] - centre, 1) for fc in R[k]}
    return {"take": os.path.basename(take_path), "reference": os.path.basename(ref_path), "cal": os.path.basename(cal_path) if cal_path else None,
            "take_span": take_span, "ref_span": ref_span, "take_seconds": round(tsec, 1), "ref_seconds": round(rsec, 1),
            "octaves": diff("octaves"), "eq": diff("eq"),
            "centroid": {"take": T["centroid"], "reference": R["centroid"]}, "tilt": {"take": T["tilt"], "reference": R["tilt"]}}


def show_match(r):
    hz = lambda f: "%gk" % (f / 1000) if f >= 1000 else "%d" % f
    print("%s against %s (%.0f s and %.0f s of sound used), calibration: %s" % (r["take"], r["reference"], r["take_seconds"], r["ref_seconds"], r["cal"] or "none"))
    print("reference minus take, dB, centred on the median EQ band (positive = reference has more):")
    print("  octave  " + " ".join("%6s" % hz(f) for f in r["octaves"]))
    print("          " + " ".join("%+6.1f" % v for v in r["octaves"].values()))
    print("centroid  take %d Hz, reference %d Hz;  tilt  take %+.1f, reference %+.1f dB/octave" % (
        r["centroid"]["take"], r["centroid"]["reference"], r["tilt"]["take"], r["tilt"]["reference"]))
    moves = [(f, v) for f, v in r["eq"].items() if abs(v) >= 1.5]
    print("EQ block, to move the take towards the reference:")
    if not moves:
        print("  none: every EQ band is within 1.5 dB")
    for f, v in moves:
        print("  %-5s Hz  %s %.1f dB%s" % (hz(f), "raise" if v > 0 else "cut", abs(v), "   (large: check the amp's tone controls first)" if abs(v) > 6 else ""))


def load_cal(path):
    """Read a microphone calibration file into (frequencies, dB). Lines that do not start with two numbers are skipped."""
    pts = []
    for line in open(path, encoding="utf-8", errors="replace"):
        parts = line.replace(",", " ").split()
        try:
            f, d = float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            continue
        if f > 0:
            pts.append((f, d))
    if len(pts) < 10:
        sys.exit("%s: fewer than 10 frequency/dB lines; not a calibration file" % path)
    pts.sort()
    return np.array([p[0] for p in pts]), np.array([p[1] for p in pts])


def cal_gain(cal, freqs):
    """Power multiplier that removes the microphone's response, interpolated on log frequency; held flat beyond the file's range."""
    cf, cd = cal
    d = np.interp(np.log10(np.maximum(freqs, 1e-3)), np.log10(cf), cd)
    return 10 ** (-d / 10)


def find_notes(x, sr):
    """Split the recording into notes at sharp rises of the loudness envelope.
    The envelope is taken above 150 Hz so that rumble and handling noise do not count as notes."""
    y = sosfiltfilt(butter(4, 150, "hp", fs=sr, output="sos"), x)
    hop = sr // 100
    env = np.sqrt(np.convolve(y ** 2, np.ones(hop * 4) / (hop * 4), mode="same"))[::hop]
    edb = 20 * np.log10(env + 1e-9)
    floor = np.percentile(edb, 25)
    thresh = max(floor + 10, np.max(edb) - 25)
    notes, i, n = [], 20, len(edb)
    while i < n - 30:
        if edb[i] > thresh and edb[i] - edb[i - 12] > 9:          # rose 9 dB within 0.12 s
            pk = i + int(np.argmax(edb[i:i + 30]))
            j = pk + 20
            while j < n - 1 and edb[j] > floor + 5 and not (edb[j] - edb[j - 12] > 9 and edb[j] > thresh):
                j += 1
            if j - i > 50:                                       # ignore anything shorter than half a second
                notes.append((i * hop, min(j * hop, len(x))))
            i = j
        else:
            i += 1
    return notes


def measure(seg, sr, cal=None):
    win = seg[int(0.08 * sr):int(0.88 * sr)]                      # skip the pick attack, take 0.8 second
    if len(win) < sr // 2:
        return None
    w = win * np.hanning(len(win))
    spec = np.abs(np.fft.rfft(w, n=1 << 17)) ** 2
    freqs = np.fft.rfftfreq(1 << 17, 1 / sr)
    if cal is not None:
        spec = spec * cal_gain(cal, freqs)
    total = spec[(freqs > 40) & (freqs < 16000)].sum()
    def peak(f, tol=0.04):
        m = (freqs > f * (1 - tol)) & (freqs < f * (1 + tol))
        return spec[m].sum(), freqs[m][np.argmax(spec[m])]
    # Which string. The open strings share harmonics (B3 is low E's 3rd, high E its 4th, A2's 4th is D3's 3rd), so
    # asking only "are this candidate's harmonics present?" lets a higher string pass when a lower one is played and
    # vice versa. Each candidate therefore also pays for energy where its own series has none: at f0/4, f0/3, f0/2,
    # 2f0/3, 3f0/4 and the same points between its higher harmonics. Played low E puts energy at B3's f0/3 and
    # 2f0/3 (82 and 165 Hz); played B3 leaves low E's 1st and 2nd harmonics empty. Score = mean dB on its
    # harmonics minus mean dB off them, both measured against the noise floor so empty windows do not go to -inf.
    band = (freqs > 40) & (freqs < 3000)
    floor = float(np.median(spec[band]))
    def at(f):                                                   # mean power per bin, so wider windows at high f do not weigh more
        m = (freqs > f * 0.98) & (freqs < f * 1.02)
        return db(spec[m].mean() + floor)
    best = None
    for name, f0 in STRINGS:
        on = [at(f0 * h) for h in range(1, 9) if f0 * h < 3000]
        off = [at(f0 * (k + q)) for k in range(0, 8) for q in (0.25, 1 / 3, 0.5, 2 / 3, 0.75) if f0 * (k + q) < 3000]
        score = float(np.mean(on) - np.mean(off))
        if best is None or score > best[0]:
            best = (score, name)
    name = best[1]
    # upper for every candidate string, so analyse() can use the playing order's string when it overrides the pitch
    f0_by, upper_by = {}, {}
    for s, nominal in STRINGS:
        f0 = peak(nominal)[1]
        harm = np.array([peak(f0 * h, 0.03)[0] for h in range(1, 31) if f0 * h < 15000])
        f0_by[s] = round(float(f0), 1)
        upper_by[s] = round(float(db(harm[5:].sum() / max(harm[:5].sum(), 1e-20))), 1)
    env = np.abs(seg)
    hop = sr // 50
    e = np.array([env[k:k + hop].max() for k in range(0, len(env) - hop, hop)])
    pk = int(np.argmax(e))
    below = np.where(e[pk:] < e[pk] * 0.1)[0]
    return {
        "string": name, "pitch": name, "f0": f0_by[name], "_f0_by": f0_by, "_upper_by": upper_by,
        "level": round(float(20 * np.log10(np.sqrt(np.mean(win ** 2)) + 1e-12)), 1),
        "bands": {b: round(float(db(spec[(freqs >= a) & (freqs < c)].sum() / total)), 1) for b, a, c in BANDS},
        "centroid": int(round(float((freqs * spec)[(freqs > 40) & (freqs < 16000)].sum() / total))),
        "upper": upper_by[name],
        "sustain": round(float(below[0] * hop / sr), 2) if len(below) else None,
    }


def average(group):
    """Mean of several plucks of one string; dB values are averaged in dB."""
    keys = [b for b, _, _ in BANDS]
    sus = [m["sustain"] for m in group if m["sustain"] is not None]
    return {
        "string": group[0]["string"], "plucks": len(group),
        "level": round(float(np.mean([m["level"] for m in group])), 1),
        "bands": {b: round(float(np.mean([m["bands"][b] for m in group])), 1) for b in keys},
        "centroid": int(round(float(np.mean([m["centroid"] for m in group])))),
        "upper": round(float(np.mean([m["upper"] for m in group])), 1),
        "sustain": round(float(np.median(sus)), 2) if sus else None,
        "spread": round(float(max(max(m["bands"][b] for m in group) - min(m["bands"][b] for m in group) for b in keys)), 1),
        "pitch_disagrees": [m["pitch"] for m in group if m["pitch"] != m["string"]],
    }


def analyse(path, cal_path=None, plucks=3):
    cal = load_cal(cal_path) if cal_path else None
    sr, x = wavfile.read(path)
    x = x.astype(float) / (32768 if x.dtype == np.int16 else 1)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = sosfiltfilt(butter(4, 40, "hp", fs=sr, output="sos"), x)
    out = []
    for a, b in find_notes(x, sr):
        m = measure(x[a:b], sr, cal)
        if m:
            m["at"] = round(a / sr, 1)
            out.append(m)
    expected = 6 * plucks
    while len(out) > expected:                                   # drop stray quiet sounds (a hand on the strings) before naming
        med = float(np.median([m["level"] for m in out]))
        quiet = min(out, key=lambda m: m["level"])
        if quiet["level"] > med - 4:
            break
        out.remove(quiet)
    by_order = len(out) == expected
    if by_order:                                                 # played in the agreed order: trust the order over pitch detection
        for k, m in enumerate(out):
            m["string"] = STRINGS[k // plucks][0]
    for m in out:                                                # f0 and upper always describe the string the note is named as
        m["f0"], m["upper"] = m.pop("_f0_by")[m["string"]], m.pop("_upper_by")[m["string"]]
    groups = [[m for m in out if m["string"] == name] for name, _ in STRINGS]
    strings = [average(g) for g in groups if g]
    noise = float(20 * np.log10(np.percentile(np.abs(x), 30) + 1e-12))
    return {"file": os.path.basename(path), "cal": os.path.basename(cal_path) if cal_path else None, "seconds": round(len(x) / sr, 1), "noise_floor": round(noise, 1),
            "plucks": plucks, "named_by": "order" if by_order else "pitch", "strings": strings, "notes": out}


def flag(n):
    """Text naming the pitches detected on plucks that the playing order named differently, e.g. '  (pitch: A2 x2)'."""
    got = n.get("pitch_disagrees") or []
    return "  (pitch: %s)" % ", ".join("%s x%d" % (p, got.count(p)) if got.count(p) > 1 else p for p in sorted(set(got))) if got else ""


def show(res):
    print("%s: %.0f s, %d notes found (%d expected), quiet level %.0f dBFS, calibration: %s" % (
        res["file"], res["seconds"], len(res["notes"]), 6 * res["plucks"], res["noise_floor"], res.get("cal") or "none"))
    if not res["notes"]:
        return
    if res["named_by"] == "pitch":
        print("warning: note count is not 6 x %d, so strings were named by pitch detection; check the plucks column" % res["plucks"])
    else:
        bad = sum(m["pitch"] != m["string"] for m in res["notes"])
        if bad * 2 > len(res["notes"]):
            print("warning: pitch disagrees with the playing order on %d of %d notes; a pluck was probably missed or doubled, replay the take" % (bad, len(res["notes"])))
    print("%-4s %6s %6s | %s | %8s %6s %7s %6s" % ("note", "plucks", "level", "  ".join("%-9s" % b.split()[0] for b, _, _ in BANDS), "centroid", "upper", "sustain", "spread"))
    for n in res["strings"]:
        print("%-4s %6d %6.1f | %s | %6d Hz %6.1f %6s %6.1f%s" % (n["string"], n["plucks"], n["level"], "  ".join("%9.1f" % n["bands"][b] for b, _, _ in BANDS),
                                                                  n["centroid"], n["upper"], ("%.1f s" % n["sustain"]) if n["sustain"] else ">end",
                                                                  n["spread"], ("  replay" if n["spread"] > 3 else "") + flag(n)))


def load(name):
    return json.load(open(os.path.join(OUT, name + ".json")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("record")
    sp.add_argument("name")
    sp.add_argument("--plucks", type=int, default=3, help="plucks per string (default 3)")
    sp.add_argument("--seconds", type=int, help="recording length (default 10 + 3.5 per pluck)")
    sp.add_argument("--mic", help="input device (macOS: name or index; Linux: ALSA device); default TONE_MIC")
    sp.add_argument("--cal", help="microphone calibration file")
    sp = sub.add_parser("analyse")
    sp.add_argument("file")
    sp.add_argument("--plucks", type=int, default=3, help="plucks per string (default 3; 1 for older takes)")
    sp.add_argument("--cal", help="microphone calibration file")
    sp = sub.add_parser("part")
    sp.add_argument("name")
    sp.add_argument("--ref", required=True, help="reference recording")
    sp.add_argument("--seconds", type=int, default=30)
    sp.add_argument("--mic", help="input device (macOS: name or index; Linux: ALSA device); default TONE_MIC")
    sp.add_argument("--cal", help="microphone calibration file")
    sp.add_argument("--ref-start", type=float)
    sp.add_argument("--ref-end", type=float)
    sp = sub.add_parser("match")
    sp.add_argument("take")
    sp.add_argument("ref")
    sp.add_argument("--cal", help="microphone calibration file (applies to the take)")
    for side in ("take", "ref"):
        sp.add_argument("--%s-start" % side, type=float)
        sp.add_argument("--%s-end" % side, type=float)
    sp = sub.add_parser("compare")
    sp.add_argument("a")
    sp.add_argument("b")
    a = ap.parse_args()
    if a.cmd in ("part", "match"):
        if a.cmd == "part":
            take, span = record(a.name, a.seconds, a.mic), (None, None)
        else:
            take, span = a.take, (a.take_start, a.take_end)
        res = match(take, a.ref, a.cal, span, (a.ref_start, a.ref_end))
        out = os.path.join(OUT, os.path.splitext(os.path.basename(take))[0] + "_match.json")
        os.makedirs(OUT, exist_ok=True)
        json.dump(res, open(out, "w"), indent=1)
        show_match(res)
    elif a.cmd in ("record", "analyse"):
        if a.plucks < 1:
            sys.exit("--plucks must be 1 or more")
        if a.cmd == "record":
            path = record(a.name, a.seconds or int(10 + 3.5 * 6 * a.plucks), a.mic)
        else:
            path = a.file
        res = analyse(path, a.cal, a.plucks)
        json.dump(res, open(os.path.splitext(path)[0] + ".json", "w"), indent=1)
        show(res)
    else:
        ra, rb = load(a.a), load(a.b)
        if ra.get("cal") != rb.get("cal"):
            print("warning: calibration differs (%s vs %s); band and centroid differences include the microphone correction" % (ra.get("cal") or "none", rb.get("cal") or "none"))
        by = lambda r: {n["string"]: n for n in r.get("strings") or r["notes"]}
        A, B = by(ra), by(rb)
        print("%s minus %s (positive = more in %s)" % (a.b, a.a, a.b))
        print("%-4s %6s | %s | %8s %6s" % ("note", "level", "  ".join("%-9s" % b.split()[0] for b, _, _ in BANDS), "centroid", "upper"))
        for s, _ in STRINGS:
            if s in A and s in B:
                print("%-4s %+6.1f | %s | %+6d Hz %+6.1f" % (s, B[s]["level"] - A[s]["level"], "  ".join("%+9.1f" % (B[s]["bands"][b] - A[s]["bands"][b]) for b, _, _ in BANDS),
                                                           B[s]["centroid"] - A[s]["centroid"], B[s]["upper"] - A[s]["upper"]))


if __name__ == "__main__":
    main()
