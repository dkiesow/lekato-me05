# Lekato JME-01 (ME-05) Bluetooth protocol

Worked out from Bluetooth packet captures of the official iOS app and from reading the pedal back after each change. One pedal, firmware V1.1.1. Where something is inferred and not confirmed, it says so.

## Transport

- The pedal advertises over Bluetooth LE as `JME STOMPBOX`. The app lists it as "JME Stompbox-(BLUE)". It stops advertising while a central is connected, and accepts one connection.
- No pairing and no encryption.
- Standard BLE-MIDI: service `03B80E5A-EDE8-4B33-A751-6CE34EC4C700`, characteristic `7772E5DB-3868-4112-A1A9-F2669D106BF3` (write without response, notify).
- The pedal asks for an MTU of 517. Preset messages are about 140 bytes, so the default 23-byte MTU is not enough.
- A separate classic-Bluetooth device with the next-lower address carries audio (A2DP sink) and two serial-port services. Those were not explored.

## Message forms

Everything is ordinary MIDI inside BLE-MIDI framing (`80 80` header and timestamp, and `80` before a closing `F7`).

- **SysEx** with manufacturer ID `00 21 7A`: `F0 00 21 7A <body> F7`. Bodies are given below without that wrapper.
- **Control Change and Program Change on MIDI channel 2**: `B1 <cc> <value>`, `C1 <slot>`. The pedal sends its own Control Changes back on channel 1 (`B0 ...`).

In SysEx bodies the third byte is the operation: `00` read, `01` write, `02` reply to a read, `03` acknowledgement of a write.

## SysEx

| Body | Meaning |
|---|---|
| `00` | Identity. Reply `10` + firmware, maker and model as zero-padded text (`V1.1.1`, `LEKATO`, `JME-01`) |
| `60` | Device name. Reply `60` + text |
| `70 0B 00 <slot> 00x13` | Read preset. Reply `70 0B 02 <slot> <125 bytes>` |
| `70 0B 01 <slot> <125 bytes>` | Write preset. Reply `70 0B 03 <slot> ...` |
| `70 0C 00x14` | Read the edit buffer, the live unsaved preset. Reply `70 0C 02 00 <125 bytes>` |
| `70 0D 01 <8 block numbers> 00x4` | Set the chain order of the live preset. Reply `70 0D 02 <order>` |
| `70 14 00` / `70 14 01 <20 bytes>` | Read / write global settings |
| `70 18 00` | Read looper state. Reply `70 18 02 <a> <rec level> <playback level> <b> <mode>`. `<a>` is 1 once the Loop page has been opened; `<b>` is not understood. Loop sync is not reported |
| `70 19 00` | Read drum state. Reply `70 19 02 <on> <pattern> <level> 00 <bpm hi> <bpm lo>` |
| `70 19 01 <on> <pattern> <level> 00 <bpm hi> <bpm lo>` | Write drum state. This is how the app sets the tempo. BPM = hi x 128 + lo, range 40-240 |
| `70 6F 00` | Read tuner state. Reply `70 6F 02 <on> 00 <ref> <mute>` |
| `70 6F 01 <on> 00 <ref> <mute> 00` | Tuner on/off. `<mute>` 1 = output muted while tuning, 0 = bypass. `<ref>` read 10 when the app showed A4 = 440 Hz, so probably 430 + value |
| `70 7E 02 ...` | Unsolicited status from the pedal: `70 7E 02 19 ...` drum state, `70 7E 02 6F ...` tuner reading |

Slots are 0 to 98 (the app shows P01 to P99). Reading an out-of-range slot returns an empty reply.

The app's connection sequence is: identity, device name, global reads (`70 14`, `70 15`, `70 19`, `70 18`), `70 09 00 <0-9>`, `70 58 ...` ten times, then all 99 presets. `70 09`, `70 15` and `70 58` are not understood; the ten `70 58` blocks may be the User AMP slots.

## Preset data

A preset is 125 seven-bit bytes. Read them as a little-endian bitstream (first byte holds the lowest 7 bits), **drop the first bit**, and take 8-bit bytes: 109 bytes result. Offsets below are into those.

| Offset | Content |
|---|---|
| 0-10 | Constant across all factory presets (byte 7 is always 98); not understood |
| 11-18 | One byte per block: model number, plus `0x40` if the block is bypassed |
| 19-76 | Per block: a knob-count byte followed by that block's knob values |
| 77 | Preset volume, 0-100 |
| 81-88 | Chain order, as block numbers |
| 93-108 | Name, 16 ASCII characters, zero padded |

| Block | Number | Model byte | Knob count byte | Knobs at | Models |
|---|---|---|---|---|---|
| OD/FX | 0 | 11 | 24 | 25-28 | 16 |
| Amp | 1 | 12 | 31 | 32-37 | 30 + 10 user slots |
| Cab | 2 | 13 | 40 | 41-45 | 27 named, to 40 |
| Mod | 3 | 14 | 60 | 61-64 | 13 |
| Delay | 4 | 15 | 65 | 66-68 | 7 |
| Reverb | 5 | 16 | 72 | 73-75 | 6 |
| Gate | 6 | 17 | 19 | 20-21 | 1 |
| EQ | 7 | 18 | 47 | 48-59 | 2 |

- The stored model number is the app's number minus 1.
- The knob-count byte is the number of knobs the model uses. Values left over from a previous model can remain in the unused positions.
- No checksum.
- A write is the same 125 bytes as a read. The safest way to build one is to read the slot, change the decoded bytes and re-encode onto the bytes you read, so anything not understood is preserved.

Model and knob names are in [`data/models.json`](../data/models.json). Points that are easy to get wrong:

- **Knob order differs per model.** TS9 is Drive, Tone, Level; BD2 is Level, Tone, Gain; Klone is Volume, Drive, Tone.
- **Most knobs are 0-100**, and the app displays them that way, including EQ bands (labelled dB but shown 0-100, 50 = centre), delay time and Detune.
- **Not 0-100:** Cab Mic is a list 0-7 (E906, M160, MD421, R121, SM57, SM58, SM7b, U47); Cab Position is 0 edge, 1 middle, 2 center; Cab Low Cut is 0-100 for 20-1000 Hz and High Cut 0-100 for 5000-20000 Hz; Hall Pre Delay is milliseconds; Mode knobs (Chorus +, Uni Vibe) are 0/1; OD/FX "Boost" has an on/off as its first knob.
- **EQ 2** is a 10-band EQ with 12 knobs: Gain, ten bands, Volume.

## Controllers (channel 2)

| CC | Control | Values |
|---|---|---|
| 0-7 | Block model and bypass, by block number | Model number, plus 64 to bypass. The pedal loads the model's defaults and echoes them as CCs on channel 1 |
| 8-9 | Gate knobs | |
| 12-15 | OD/FX knobs | |
| 18-23 | Amp knobs | Gain, Bass, Middle, Treble, Presence, Master |
| 26-30 | Cab knobs | Mic, Position, Low Cut, High Cut, Volume |
| 32-43 | EQ knobs | |
| 44-47 | Mod knobs | |
| 48-51 | Delay knobs | |
| 54-57 | Reverb knobs | |
| 58 | Preset volume | 0-100 |
| 59 | Which block's page is open | Block number 0-7, 8 Loop, 9 Drum |
| 63 | Drum on/off | 1 / 0 |
| 64 | Drum pattern | 0-99 = genre x 10 + (pattern - 1). Genres in order: Pop, Rock, Metal, Disco, Funk, Blues, Hip Hop, Jazz, Latin, Metro |
| 65 | Drum level | 0-100 |
| 66 | Looper Rec level | 0-100 |
| 67 | Looper Playback level | 0-100 |
| 68 | Looper buttons | 1 Rec (again = stop recording and play), 2 Stop, 4 Clear. **Ignored unless CC 59 = 8 was sent first.** The pedal answers with the looper state on channel 1: 0 empty, 1 recording, 2 playing, 4 stopped |
| 73 | Loop sync | 1 / 0 |
| 74 | Loop mode | 0 NR, 1 AR |

Controller changes act on the edit buffer. `C1 <slot>` loads a preset and discards unsaved edits. To save, read the edit buffer (`70 0C`) and write it to the slot (`70 0B 01`).

**Unidentified:** 10-11, 16-17, 24-25, 31, 52-53 (gaps between knob groups, probably unused), 60-62, 69-72 (the pedal sends 69-72 = 0 when the tuner opens), 75 and above. Looper Undo is presumably CC 68 = 3 but was never seen. **Do not sweep unidentified controllers**: several act on global state.

## Global settings

`70 14` carries 20 seven-bit bytes. Unpack them as a little-endian bitstream **without** dropping a bit.

| Unpacked byte | Setting | Notes |
|---|---|---|
| 1 | Input trim | dB + 10: 10 = 0 dB, 20 = +10 dB. The lower limit was not checked |
| 4, 5, 6 | Global EQ Bass, Mid, Treble | 0-100, 50 = centre |
| 10 | Loopback | 1 / 0 |
| 2, 3, 7, 8, 9, 11, 12 | Unknown | Read 30, 10, 50, 50, 1, 60, 180 |

## Open questions

- Other firmware versions and other Lekato models.
- The unidentified controllers above, and Looper Undo.
- `70 09`, `70 15`, `70 58`, and uploading to the User AMP slots.
- The unknown bytes in the preset (0-10, 78-80, 89-92) and in the global block.
- Which Detune value means no pitch shift, and whether the Cab cut values map to Hz linearly.
- Whether the classic-Bluetooth serial ports speak the same protocol.

## How it was mapped

1. **Capture.** On iOS or iPadOS, install Apple's Bluetooth logging profile (developer.apple.com, "Profiles and Logs"), restart, use the app, then trigger a sysdiagnose. The archive contains `logs/Bluetooth/bluetoothd-hci-latest.pklg`, a full packet log. On Android, the "Bluetooth HCI snoop log" developer option gives the same thing.
2. **One action per step.** Change one control in the app, note the time, move to the next. Matching packets to actions is then trivial. This was far more reliable, and far safer, than probing the pedal with guessed messages.
3. **Read back.** After each write from a computer, read the preset or state back and compare.
4. **Names by hand.** Model and knob names exist only in the app, so they were typed into a spreadsheet, using the default knob values the pedal reports for each model to confirm the order.
