# lekato-me05

Control a Lekato multi-effects pedal (sold as the ME-05, identifies itself as `JME-01`) from a computer over Bluetooth, without the phone app.

This repository has three things:

- **[docs/PROTOCOL.md](docs/PROTOCOL.md)**: the pedal's Bluetooth MIDI protocol, worked out from packet captures of the official app. If you are building your own controller, this is the part you want.
- **`lekato.py`**: a command-line tool to read, edit, back up and restore presets, and to control the drum machine, looper, tuner and global settings.
- **`lekato_web.py`**: a small touch page for a Raspberry Pi screen: pick a preset, adjust its knobs live, save when you choose.

It is unofficial and has no connection to Lekato.

## Status

- Tested on **one pedal, firmware V1.1.1**. Other Lekato models and other firmware may differ.
- Works on **Linux** (Raspberry Pi OS / Debian, BlueZ 5.82, through `gatttool`) and **macOS** (through `bleak`); reading and writing presets were verified on both. **Windows has not been tried.**
- Every preset write is read back and compared. Even so: **back up your pedal first** (`lekato.py backup`), and expect rough edges.

## What you need

- The pedal, switched on and within Bluetooth range.
- Python 3.8 or newer and `pip install bleak`.
- On Linux, BlueZ's `gatttool` as well (package `bluez` or `bluez-deprecated` depending on the distribution). See "Linux" below for why.
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

## macOS

The first run asks for Bluetooth permission for the terminal application you run it from. Until that is granted, a scan can hang without an error. macOS hides Bluetooth addresses, so the pedal is found by name; `--address` takes the device UUID macOS assigns instead.

## Linux

Stock BlueZ is built with a MIDI plugin that claims every BLE-MIDI device. A write through BlueZ's normal API, which `bleak` uses, is then refused with "Insufficient Authorization". `lekato.py` therefore drives `gatttool` on Linux when it is installed (`--transport gatttool`, chosen automatically) and uses `bleak` only to find the pedal by name. The alternative is to start `bluetoothd` with `-P midi`.

## Things to know before you experiment

- **Do not send MIDI controllers blindly.** Several controller numbers above 58 act on global state. A sweep of all 128 controllers during the mapping switched the drum machine on, changed its pattern and levels and flipped the looper mode. The unidentified ones are listed in the protocol document.
- The pedal has a second, classic-Bluetooth side for audio that is not used here.
- Uploading to the User AMP slots, and the app's Import/Export, are not mapped.

## Contributing

Reports from other firmware versions or other Lekato models are the most useful thing, followed by any of the gaps listed at the end of [docs/PROTOCOL.md](docs/PROTOCOL.md). The method that worked for the mapping is described there too.

## Licence

MIT. See [LICENSE](LICENSE).
