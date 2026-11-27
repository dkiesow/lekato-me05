# lekato-me05

Control a Lekato multi-effects pedal (sold as the ME-05, identifies itself as `JME-01`) from a computer over Bluetooth, without the phone app.

This repository has four things:

- **[docs/PROTOCOL.md](docs/PROTOCOL.md)**: the pedal's Bluetooth MIDI protocol, worked out from packet captures of the official app. If you are building your own controller, this is the part you want.
- **`lekato.py`**: a command-line tool to read, edit, back up and restore presets, and to control the drum machine, looper, tuner and global settings.
- **`lekato_web.py`**: a small touch page for a Raspberry Pi screen: pick a preset, adjust its knobs live, save when you choose.
- **`tone.py` and `ingest.py`** (optional): measure the pedal's sound through a microphone, compare it with a reference recording, and derive a starting preset from a recording of the sound you want.

It is unofficial and has no connection to Lekato.

## Status

- Tested on **one pedal, firmware V1.1.1**. Other Lekato models and other firmware may differ.
- Works on **Linux** (Raspberry Pi OS / Debian, BlueZ 5.82) and **macOS**; reading and writing presets were verified on both. **Windows has not been tried.**
- Every preset write is read back and compared. Even so: **back up your pedal first** (`lekato.py backup`), and expect rough edges.

## What you need

- The pedal, switched on and within Bluetooth range.
- Python 3.8 or newer and `pip install bleak`.
- On Linux, `alsa-utils` 1.2.9 or newer as well (for `aconnect`, `aseqdump` and `aseqsend`). See "Linux" below.
- The phone app **disconnected**. The pedal accepts one Bluetooth LE connection at a time. On iOS, closing the app is not always enough; turn Bluetooth off in Settings.

## Quick start

```
pip install bleak
python lekato.py info                       # prints the firmware and model
python lekato.py backup my-pedal.json       # save all 99 presets first
python lekato.py show 0                     # preset 0 with model and knob names
python lekato.py export 0 -o p0.json        # save it as editable JSON
python lekato.py apply p0.json --dry-run    # preview a change
python lekato.py apply p0.json              # write it and verify
```

Slots are numbered 0 to 98 here. The app shows them as P01 to P99, so P09 is slot 8.

## Commands

| Command | What it does |
|---|---|
| `info` | Firmware, maker and model as the pedal reports them |
| `list` | Names of all 99 presets |
| `show N` | One preset in readable form |
| `export N [-o file]` | One preset as JSON |
| `apply file [--slot N] [--dry-run]` | Write a JSON preset to a slot, then read it back and compare |
| `backup file` | All 99 presets to one file |
| `restore file N... [--to M] [--all]` | Write slots back from a backup |
| `select N` | Switch the pedal to a preset |
| `drum [--on\|--off] [--pattern "Rock 03"] [--level N] [--bpm N]` | Drum machine |
| `looper [--rec N] [--playback N] [--sync on\|off] [--mode NR\|AR] [--press rec\|stop\|clear\|undo]` | Looper levels, switches and buttons |
| `tuner [--on\|--off] [--mute on\|off]` | Tuner |
| `global [--trim dB] [--bass N] [--mid N] [--treble N] [--loopback on\|off]` | Input trim, global EQ, USB loopback |

`show` and `export` also take `--backup file` to read a slot from a backup file instead of the pedal.

## Preset files

`export` writes, and `apply` reads, JSON like [examples/example_preset.json](examples/example_preset.json):

```json
{
 "slot": 98,
 "name": "Example",
 "volume": 80,
 "blocks": {
  "OD-FX":  {"on": true, "model": "TS9", "knobs": {"Drive": 30, "Tone": 55, "Level": 60}},
  "Reverb": {"on": true, "model": "Spring", "knobs": {"Mix": 25, "Decay": 40}}
 }
}
```

- Blocks you leave out are not changed. Knobs you leave out keep their value, or take the model's default if the model changed.
- Models can be given by name or by the number the app shows. Names and knob names are matched without regard to case; a wrong one is rejected with the list of valid ones.
- Model and knob names come from [data/models.json](data/models.json), which was typed in by hand from the app ([docs/pedal_models.xlsx](docs/pedal_models.xlsx) is the source; `export_models.py` converts it). **Read the notes there:** knob order differs from model to model, and some "knobs" are switches or lists.
- Names are limited to 16 ASCII characters.

## The touch page

```
python lekato_web.py                 # http://<this machine>:8766/
python lekato_web.py --mock my-pedal.json   # simulated pedal from a backup, no Bluetooth
```

- Laid out for an 800 x 480 screen. Preset list on the left; one chip per effect block; the selected block's knobs as sliders with their real names; preset volume; Save, Revert and Connect/Release.
- Sliders change the live sound. Nothing is written to a preset until you press **Save**; **Revert** reloads the saved preset.
- The cab block is left off the page on purpose, and models cannot be changed from it. It is for adjusting a sound, not building one.
- It connects only when asked and lets go after 10 minutes without use (`--idle`), so the phone app can connect again. While it is connected, nothing else can be.
- [contrib/lekato-web.service](contrib/lekato-web.service) is a systemd user unit for running it on a Raspberry Pi.
- There is **no authentication**. Anyone who can reach the port can change your pedal. Run it on a trusted network or bind it to localhost (`--bind 127.0.0.1`).

## Tone matching (optional)

Two scripts for working out settings by ear and by measurement. They do not talk to the pedal; set the preset with `lekato.py` or the touch page, then record. Both need `ffmpeg`, `numpy` and `scipy` (`pip install numpy scipy`). Results go to `capture/`, which git ignores.

**`tone.py`** records the amp through a microphone and measures it.

| Command | What it does |
|---|---|
| `record NAME [--plucks 3] [--mic DEVICE] [--cal FILE]` | Record open strings, low E to high E, each plucked 3 times (pluck, ring 2 s, damp), then measure each string: level, five band shares, centroid, upper-harmonic ratio, sustain, and the spread between plucks |
| `analyse FILE.wav [--plucks N] [--cal FILE]` | The same on an existing recording |
| `compare A B` | Per-string differences between two takes |
| `part NAME --ref REF [--seconds 30]` | Record yourself playing the same part as a reference recording, then `match` it |
| `match TAKE REF [--ref-start S] [--ref-end S] [--cal FILE]` | Compare the long-term spectrum of a take with a reference (any format ffmpeg reads); prints octave differences and the change at each of the pedal's six EQ frequencies, in dB |

`--mic` is the input device (macOS: an AVFoundation name or index, run without it to list them; Linux: an ALSA device). The `TONE_MIC` environment variable sets a default. `--cal` applies a measurement microphone's calibration file (frequency and dB per line, as supplied with a Dayton UMM-6 or miniDSP UMIK-1).

What we learned using it: put the microphone within a foot of the speaker and keep it there. At five feet, two takes at identical settings differed by 6 to 14 dB in the bass band, so only loudness could be compared.

**`ingest.py`** analyses a reference recording, such as a guitar solo, and writes a starting preset in `lekato.py apply` format.

```
python ingest.py solo.m4a --start 95 --end 130 --name "Solo" --slot 10
python lekato.py apply capture/ingest/Solo_preset.json --slot 10 --dry-run
```

It measures drive (how much held notes sustain, and how fast the harmonics fall off), modulation (a level wobble inside held notes and chords that the pitch does not share; tremolo if every band moves together, vibe otherwise), delay (an onset's spectrum repeating a fixed time later), reverb (RT60 from note endings) and the spectrum's tilt, and picks models and knob values from fixed rules written into the preset's `notes`. Use an isolated guitar track if you have one; `--separate` runs Demucs to pull the guitar out of a mix, if Demucs is installed (about 2 GB with PyTorch).

Treat its output as a starting point:

- The drive thresholds were set on synthetic test signals, not on real recordings.
- Three knob mappings are assumptions: Mod Speed (0-100 taken as 0-10 Hz), Delay Time (0-100 taken as 0-1000 ms) and Reverb Decay.
- A wobble must last three cycles inside one held note or chord, so slow rates (a Leslie's slow setting, under about 1.5 Hz) need long held notes to be seen.
- Echo and reverb in the reference make the drive read one class low.

## macOS

The first run asks for Bluetooth permission for the terminal application you run it from. Until that is granted, a scan can hang without an error. macOS hides Bluetooth addresses, so the pedal is found by name; `--address` takes the device UUID macOS assigns instead.

## Linux

BlueZ has a MIDI plugin that takes over any Bluetooth MIDI device and exposes it as a normal ALSA MIDI port. That is the path `lekato.py` uses on Linux: `bleak` finds the pedal and holds the connection, and the MIDI itself goes through the port with `alsa-utils`. Nothing needs configuring.

The same plugin is why a direct write through `bleak` fails on Linux with "Insufficient Authorization": the plugin has claimed the device.

`--transport` selects the path explicitly:

| Transport | Used when | Notes |
|---|---|---|
| `alsa` | Linux, if `aseqsend` is installed (the default) | The system MIDI port. Tested |
| `gatttool` | Linux fallback if `alsa-utils` is too old | Drives BlueZ's deprecated `gatttool`, which bypasses the plugin. Tested |
| `bleak` | macOS and Windows | Direct BLE-MIDI. Tested on macOS. On Linux it works only if `bluetoothd` is started with `-P midi` |

While the pedal is connected this way it also appears to any other MIDI software on the machine as "JME STOMPBOX".

## Things to know before you experiment

- **Do not send MIDI controllers blindly.** Several controller numbers above 58 act on global state. A sweep of all 128 controllers during the mapping switched the drum machine on, changed its pattern and levels and flipped the looper mode. The unidentified ones are listed in the protocol document.
- The pedal has a second, classic-Bluetooth side for audio that is not used here.
- Uploading to the User AMP slots, and the app's Import/Export, are not mapped.

## Contributing

Reports from other firmware versions or other Lekato models are the most useful thing, followed by any of the gaps listed at the end of [docs/PROTOCOL.md](docs/PROTOCOL.md). The method that worked for the mapping is described there too.

## Licence

MIT. See [LICENSE](LICENSE).
