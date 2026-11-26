#!/usr/bin/env python3
"""lekato.py - read, write, back up and restore presets on a Lekato JME-01 (ME-05) pedal over Bluetooth LE.

Needs Python 3.8+ and `pip install bleak`. On Linux it also uses BlueZ's `gatttool` (see GatttoolPedal for why).
Unofficial; tested on one pedal, firmware V1.1.1.
The pedal accepts one Bluetooth LE connection, so close the phone app (or turn the phone's Bluetooth off) first.

  lekato.py info                      identify the pedal
  lekato.py list                      names of all 99 presets
  lekato.py show 6                    one preset, with model and knob names
  lekato.py export 6 -o preset.json      save one preset as editable JSON
  lekato.py apply preset.json --slot 6   write a JSON preset to a slot and verify it
  lekato.py backup all.json           save all 99 presets
  lekato.py restore all.json 6        write slot 6 back from a backup
  lekato.py select 6                  switch the pedal to a preset
  lekato.py drum                      show the drum machine; --on/--off, --pattern "Rock 03", --level 50, --bpm 120
  lekato.py looper                    show the looper; --rec 50, --playback 50, --sync on|off, --mode NR|AR, --press rec|stop|clear|undo
  lekato.py global                    show global settings; --trim 0, --bass 50, --mid 50, --treble 50, --loopback on|off
  lekato.py tuner                     show the tuner state; --on/--off, --mute on|off
"""
import argparse, asyncio, datetime, json, os, re, sys

MIDI_CHAR = "7772e5db-3868-4112-a1a9-f2669d106bf3"      # standard BLE-MIDI data characteristic
MFR = bytes.fromhex("00217a")                             # SysEx manufacturer ID used by the pedal
SLOTS = 99
MIDI_CH = 1                                               # the pedal listens on MIDI channel 2 (0-based 1)

# block -> (model byte, knob-count byte, knob capacity). Knobs follow the count byte. Offsets are into the decoded preset.
BLOCKS = {
    "Gate":   (17, 19, 2),
    "OD-FX":  (11, 24, 4),
    "Amp":    (12, 31, 6),
    "Cab":    (13, 40, 5),
    "EQ":     (18, 47, 12),
    "Mod":    (14, 60, 4),
    "Delay":  (15, 65, 4),
    "Reverb": (16, 72, 4),
}
BLOCK_IDS = ["OD-FX", "Amp", "Cab", "Mod", "Delay", "Reverb", "Gate", "EQ"]   # numbering used in the chain-order bytes
BYPASS = 0x40
VOLUME, ORDER, NAME, NAME_LEN = 77, 81, 93, 16


# ---------------------------------------------------------------- preset codec (no Bluetooth needed)

def decode(data7: bytes) -> bytearray:
    """125 seven-bit bytes from the pedal -> 109 eight-bit bytes. The stream is LSB-first and starts with one unused bit."""
    acc = 0
    for i, x in enumerate(data7):
        acc |= (x & 0x7F) << (7 * i)
    acc >>= 1
    return bytearray((acc >> (8 * i)) & 0xFF for i in range((7 * len(data7) - 1) // 8))


def encode(raw: bytes, template7: bytes) -> bytes:
    """Inverse of decode(). Bits outside the decoded bytes are copied from template7 (a preset read from the pedal)."""
    acc = 0
    for i, x in enumerate(template7):
        acc |= (x & 0x7F) << (7 * i)
    for i, v in enumerate(raw):
        sh = 1 + 8 * i
        acc = (acc & ~(0xFF << sh)) | (v << sh)
    return bytes((acc >> (7 * i)) & 0x7F for i in range(len(template7)))


def load_models():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "models.json")
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return {}


def _norm(s) -> str:
    s = re.sub(r"\((db|hz|ms)\)", "", str(s).lower())
    return re.sub(r"[\s()_\-]", "", s).replace(",", ".")


def _model_info(models, block, app_no):
    lst = models.get(block) or []
    return lst[app_no - 1] if 0 < app_no <= len(lst) else None


def preset_to_dict(raw: bytes, models) -> dict:
    out = {"name": bytes(raw[NAME:NAME + NAME_LEN]).split(b"\0")[0].decode("ascii", "replace"),
           "volume": raw[VOLUME],
           "order": [BLOCK_IDS[b] if b < len(BLOCK_IDS) else b for b in raw[ORDER:ORDER + 8]],
           "blocks": {}}
    for block, (mb, cb, cap) in BLOCKS.items():
        app_no = (raw[mb] & 0x3F) + 1
        info = _model_info(models, block, app_no)
        count = min(raw[cb], cap)
        names = (info or {}).get("knobs") or []
        knobs = {}
        for k in range(count):
            nm = names[k] if k < len(names) and names[k] else "knob%d" % (k + 1)
            knobs[nm] = raw[cb + 1 + k]
        out["blocks"][block] = {"on": not raw[mb] & BYPASS, "model": app_no, "model_name": (info or {}).get("name"), "knobs": knobs}
    return out


def dict_to_preset(d: dict, base: bytes, models) -> bytearray:
    """Apply a preset description onto `base` (the decoded current content of the slot). Blocks left out are not changed."""
    raw = bytearray(base)
    if "name" in d:
        nm = d["name"].encode("ascii")
        if len(nm) > NAME_LEN:
            raise ValueError("name is longer than %d characters" % NAME_LEN)
        raw[NAME:NAME + NAME_LEN] = nm.ljust(NAME_LEN, b"\0")
    if "volume" in d:
        raw[VOLUME] = _val(d["volume"], "volume")
    if "order" in d:
        order = [BLOCK_IDS.index(b) if isinstance(b, str) else int(b) for b in d["order"]]
        if sorted(order) != list(range(8)):
            raise ValueError("order must list each of the 8 blocks once: %s" % BLOCK_IDS)
        raw[ORDER:ORDER + 8] = bytes(order)
    for block, spec in (d.get("blocks") or {}).items():
        key = next((b for b in BLOCKS if _norm(b) == _norm(block)), None)
        if key is None:
            raise ValueError("unknown block %r; blocks are %s" % (block, list(BLOCKS)))
        mb, cb, cap = BLOCKS[key]
        old_no = (raw[mb] & 0x3F) + 1
        app_no = old_no
        if spec.get("model") is not None:
            m = spec["model"]
            if isinstance(m, str):
                hits = [x["app"] for x in models.get(key, []) if _norm(x["name"]) == _norm(m)]
                if not hits:
                    raise ValueError("%s: no model named %r" % (key, m))
                m = hits[0]
            app_no = int(m)
        elif spec.get("model_name"):
            hits = [x["app"] for x in models.get(key, []) if _norm(x["name"]) == _norm(spec["model_name"])]
            app_no = hits[0] if hits else old_no
        if not 1 <= app_no <= 64:
            raise ValueError("%s: model %r out of range" % (key, app_no))
        info = _model_info(models, key, app_no)
        on = spec.get("on", not raw[mb] & BYPASS)
        raw[mb] = (app_no - 1) | (0 if on else BYPASS)
        names = (info or {}).get("knobs") or []
        if app_no != old_no:
            if info is None:
                raise ValueError("%s: model %d is not in data/models.json, so its knob count is unknown" % (key, app_no))
            raw[cb] = len(names)
            for k in range(cap):
                raw[cb + 1 + k] = info["defaults"][k] if k < len(info["defaults"]) else 0
        knobs = spec.get("knobs") or {}
        if isinstance(knobs, list):
            knobs = {("knob%d" % (i + 1)): v for i, v in enumerate(knobs)}
        index = {_norm(n): i for i, n in enumerate(names) if n}
        for nm, v in knobs.items():
            k = index.get(_norm(nm))
            if k is None:
                mt = re.fullmatch(r"knob(\d+)", _norm(nm))
                k = int(mt.group(1)) - 1 if mt else None
            if k is None or not 0 <= k < min(raw[cb] or cap, cap):
                raise ValueError("%s model %d (%s) has no knob %r; its knobs are %s" % (key, app_no, (info or {}).get("name"), nm, names))
            raw[cb + 1 + k] = _val(v, "%s %s" % (key, nm))
    return raw


def _val(v, what):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 127:
        raise ValueError("%s must be a whole number 0-127, got %r" % (what, v))
    return v


def format_preset(d: dict) -> str:
    lines = ["%s   (volume %s)" % (d["name"], d["volume"]), "chain: " + " > ".join(str(b) for b in d["order"])]
    for block, b in d["blocks"].items():
        knobs = ", ".join("%s %s" % kv for kv in b["knobs"].items())
        lines.append("  %-6s %-3s %2d %-16s %s" % (block, "ON" if b["on"] else "off", b["model"], b["model_name"] or "", knobs if b["on"] else ""))
    return "\n".join(lines)


GENRES = ["Pop", "Rock", "Metal", "Disco", "Funk", "Blues", "Hip Hop", "Jazz", "Latin", "Metro"]
CC_DRUM_ON, CC_DRUM_PATTERN, CC_DRUM_LEVEL = 63, 64, 65
CC_LOOP_REC, CC_LOOP_PLAY, CC_LOOP_SYNC, CC_LOOP_MODE = 66, 67, 73, 74
CC_LOOP_BUTTON = 68
LOOP_BUTTONS = {"rec": 1, "stop": 2, "undo": 3, "clear": 4}       # rec again ends recording and plays; undo (3) is a guess, never seen
LOOP_STATES = {0: "empty", 1: "recording", 2: "playing", 4: "stopped"}
GLOBAL_TRIM, GLOBAL_BASS, GLOBAL_MID, GLOBAL_TREBLE, GLOBAL_LOOPBACK = 1, 4, 5, 6, 10      # offsets in the unpacked global block


def pattern_name(i: int) -> str:
    return "%s %02d" % (GENRES[i // 10], i % 10 + 1) if 0 <= i < 100 else "pattern %d" % i


def pattern_index(text) -> int:
    """'Rock 03', 'hip hop 10' or a plain number 0-99 -> pattern index (genre x 10 + pattern - 1)."""
    t = str(text).strip()
    if t.isdigit():
        i = int(t)
    else:
        m = re.fullmatch(r"(.*?)\s*(\d+)", t)
        g = [k for k, name in enumerate(GENRES) if m and _norm(name) == _norm(m.group(1))]
        if not g or not 1 <= int(m.group(2)) <= 10:
            raise SystemExit("pattern must look like 'Rock 03' (genres: %s; patterns 01-10) or be a number 0-99" % ", ".join(GENRES))
        i = g[0] * 10 + int(m.group(2)) - 1
    if not 0 <= i <= 99:
        raise SystemExit("pattern number must be 0-99")
    return i


def unpack7(data7: bytes) -> bytearray:
    """Seven-bit bytes -> eight-bit bytes with no skipped bit (the global settings block uses this form)."""
    acc = 0
    for i, x in enumerate(data7):
        acc |= (x & 0x7F) << (7 * i)
    return bytearray((acc >> (8 * i)) & 0xFF for i in range(7 * len(data7) // 8))


def pack7(raw: bytes, template7: bytes) -> bytes:
    acc = 0
    for i, x in enumerate(template7):
        acc |= (x & 0x7F) << (7 * i)
    for i, v in enumerate(raw):
        acc = (acc & ~(0xFF << (8 * i))) | (v << (8 * i))
    return bytes((acc >> (7 * i)) & 0x7F for i in range(len(template7)))


def _onoff(text) -> int:
    t = str(text).lower()
    if t in ("on", "1", "true", "yes"):
        return 1
    if t in ("off", "0", "false", "no"):
        return 0
    raise SystemExit("expected on or off, got %r" % text)


def _level(v, what, lo=0, hi=100) -> int:
    if not lo <= v <= hi:
        raise SystemExit("%s must be %d-%d" % (what, lo, hi))
    return v


# ---------------------------------------------------------------- Bluetooth

class Pedal:
    def __init__(self, name="JME", address=None, timeout=10.0):
        self.name, self.address, self.timeout = name, address, timeout
        self.client = None
        self._replies = None
        self._sysex = None          # bytes collected for a SysEx message that may span several notifications
        self.cc_seen = []           # (channel, controller, value) messages the pedal has sent

    async def _find(self):
        from bleak import BleakScanner
        dev = None
        for attempt in range(3):                        # the pedal takes a few seconds to advertise again after a disconnect
            if self.address:
                dev = await BleakScanner.find_device_by_address(self.address, timeout=self.timeout)
            else:
                want = self.name.upper()
                dev = await BleakScanner.find_device_by_filter(
                    lambda d, ad: want in ((ad.local_name or d.name or "").upper()), timeout=self.timeout)
            if dev is not None:
                break
        if dev is None:
            raise SystemExit("Pedal not found. It must be on and in range, and the phone app must not be connected to it.")
        return dev

    async def __aenter__(self):
        from bleak import BleakClient
        self._replies = asyncio.Queue()
        dev = await self._find()
        self.client = BleakClient(dev)
        await self.client.connect()
        await self.client.start_notify(MIDI_CHAR, self._on_notify)
        return self

    async def __aexit__(self, *exc):
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def _send(self, data: bytes):
        await self.client.write_gatt_char(MIDI_CHAR, data, response=False)

    def _on_notify(self, _, data: bytearray):
        # BLE-MIDI: header byte, then timestamp bytes (bit 7 set) mixed with MIDI. SysEx data bytes are all below 0x80.
        d = bytes(data)
        if len(d) == 5 and d[2] & 0xF0 == 0xB0 and self._sysex is None:       # a single Control Change from the pedal
            self.cc_seen.append((d[2] & 0x0F, d[3], d[4]))
            return
        for b in bytes(data)[1:]:
            if b == 0xF0:
                self._sysex = bytearray()
            elif b == 0xF7:
                if self._sysex is not None and bytes(self._sysex[:3]) == MFR:
                    self._replies.put_nowait(bytes(self._sysex[3:]))
                self._sysex = None
            elif b < 0x80 and self._sysex is not None:
                self._sysex.append(b)

    async def midi(self, msg: bytes, pause=0.15):
        await self._send(b"\x80\x80" + msg)
        await asyncio.sleep(pause)

    async def ask(self, body: bytes, timeout=3.0) -> bytes:
        for attempt in range(2):                    # the pedal occasionally drops a request sent right after a knob change
            while not self._replies.empty():
                self._replies.get_nowait()
            await self._send(b"\x80\x80\xf0" + MFR + body + b"\x80\xf7")
            try:
                return await asyncio.wait_for(self._replies.get(), timeout)
            except asyncio.TimeoutError:
                if attempt or body[2:3] == b"\x01":   # never resend a write
                    raise SystemExit("No reply from the pedal to %s" % body[:4].hex())
                await asyncio.sleep(0.5)

    async def identity(self) -> str:
        r = await self.ask(b"\x00")
        return " ".join(p.decode("ascii", "replace") for p in r[1:].split(b"\0") if p)

    async def read(self, slot: int) -> bytes:
        _slot(slot)
        r = await self.ask(bytes([0x70, 0x0B, 0x00, slot]) + bytes(13))
        if r[:3] != b"\x70\x0b\x02" or len(r) < 100:
            raise SystemExit("Unexpected reply reading slot %d: %s" % (slot, r[:8].hex()))
        return r[4:]

    async def write(self, slot: int, data7: bytes, select=True):
        """Write a preset, then read it back. Raises if the pedal does not hold exactly what was sent."""
        _slot(slot)
        r = await self.ask(bytes([0x70, 0x0B, 0x01, slot]) + data7)
        if r[:4] != bytes([0x70, 0x0B, 0x03, slot]):
            raise SystemExit("Slot %d: write not acknowledged (%s)" % (slot, r[:6].hex()))
        if select:
            await self.select(slot)
        if await self.read(slot) != data7:
            raise SystemExit("Slot %d: read-back differs from what was written" % slot)

    async def cc(self, number: int, value: int):
        await self.midi(bytes([0xB0 | MIDI_CH, number, value]))

    async def drum(self) -> dict:
        r = await self.ask(b"\x70\x19\x00")
        if r[:3] != b"\x70\x19\x02":
            raise SystemExit("Unexpected drum reply: %s" % r.hex())
        return {"on": bool(r[3]), "pattern": r[4], "level": r[5], "bpm": r[7] * 128 + r[8]}

    async def set_bpm(self, bpm: int):
        d = await self.drum()                       # the tempo travels in a message that restates the whole drum state
        r = await self.ask(bytes([0x70, 0x19, 0x01, int(d["on"]), d["pattern"], d["level"], 0, bpm >> 7, bpm & 0x7F]))
        if r[:3] != b"\x70\x19\x03":
            raise SystemExit("BPM write not acknowledged (%s)" % r.hex())

    async def looper(self) -> dict:
        r = await self.ask(b"\x70\x18\x00")
        if r[:3] != b"\x70\x18\x02":
            raise SystemExit("Unexpected looper reply: %s" % r.hex())
        # r[3] and r[6] are not understood. Loop sync is not in this reply, so it can be set but not read back.
        return {"rec": r[4], "playback": r[5], "mode": "AR" if r[7] else "NR"}

    async def press(self, button: str):
        """Press a looper button. Returns the looper state the pedal reports afterwards, or None if it says nothing."""
        await self.cc(59, 8, )                      # the buttons are ignored unless the Loop page is the open one
        await asyncio.sleep(0.2)
        self.cc_seen.clear()
        await self.cc(CC_LOOP_BUTTON, LOOP_BUTTONS[button])
        for _ in range(20):
            hits = [v for ch, c, v in self.cc_seen if c == CC_LOOP_BUTTON]
            if hits:
                return hits[-1]
            await asyncio.sleep(0.1)
        return None

    async def tuner(self) -> dict:
        r = await self.ask(b"\x70\x6f\x00")
        if r[:3] != b"\x70\x6f\x02":
            raise SystemExit("Unexpected tuner reply: %s" % r.hex())
        return {"on": bool(r[3]), "ref": r[5], "mute": bool(r[6])}

    async def set_tuner(self, on: bool, ref: int, mute: bool):
        r = await self.ask(bytes([0x70, 0x6F, 0x01, int(on), 0, ref, int(mute), 0]))
        if r[:3] != b"\x70\x6f\x03":
            raise SystemExit("Tuner write not acknowledged (%s)" % r.hex())

    async def global_raw(self) -> bytes:
        r = await self.ask(b"\x70\x14\x00")
        if r[:3] != b"\x70\x14\x02" or len(r) < 23:
            raise SystemExit("Unexpected global-settings reply: %s" % r.hex())
        return r[3:]

    async def global_write(self, data7: bytes):
        r = await self.ask(b"\x70\x14\x01" + data7)
        if r[:3] != b"\x70\x14\x03":
            raise SystemExit("Global settings write not acknowledged (%s)" % r.hex())
        if await self.global_raw() != data7:
            raise SystemExit("Global settings: read-back differs from what was written")

    async def select(self, slot: int):
        _slot(slot)
        await self.midi(bytes([0xC0 | MIDI_CH, slot]), pause=0.5)


class GatttoolPedal(Pedal):
    """Linux transport that drives BlueZ's `gatttool`.

    Stock BlueZ is built with a MIDI plugin that claims every BLE-MIDI device for itself, so a write through the
    normal API (which bleak uses) is refused with "Insufficient Authorization". gatttool opens its own connection
    and is not affected. The alternative is to start bluetoothd with `-P midi`."""

    async def __aenter__(self):
        import shutil
        self._replies = asyncio.Queue()
        self._lines = asyncio.Queue()
        if not self.address:
            self.address = (await self._find()).address
            await asyncio.sleep(1.0)                    # let the scan stop before connecting
        cmd = ["gatttool", "-b", self.address, "-t", "public", "-I"]
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-o0"] + cmd
        self.proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._reader = asyncio.ensure_future(self._read())
        for attempt in range(6):
            await self._cmd("connect")
            if await self._expect("Connection successful", 8):
                break
            await asyncio.sleep(1.0)
        else:
            await self.__aexit__()
            raise SystemExit("Could not connect to %s. The phone app may be holding the pedal." % self.address)
        await self._cmd("mtu 247")
        await self._expect("MTU was exchanged", 3)
        await self._cmd("characteristics")
        line = await self._expect(MIDI_CHAR, 5)
        m = re.search(r"char value handle: (0x[0-9a-f]+)", line or "")
        if not m:
            await self.__aexit__()
            raise SystemExit("The device has no BLE-MIDI characteristic")
        self.handle = int(m.group(1), 16)
        await self._cmd("char-write-req 0x%04x 0100" % (self.handle + 1))     # enable notifications
        await self._expect("written successfully", 3)
        return self

    async def __aexit__(self, *exc):
        try:
            await self._cmd("disconnect")
            await asyncio.sleep(0.3)
            await self._cmd("exit")
            await asyncio.sleep(0.2)
            self.proc.kill()
        except Exception:
            pass
        self._reader.cancel()

    async def _cmd(self, text):
        self.proc.stdin.write((text + "\n").encode())
        await self.proc.stdin.drain()

    async def _expect(self, text, timeout):
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            try:
                line = await asyncio.wait_for(self._lines.get(), max(0.05, end - loop.time()))
            except asyncio.TimeoutError:
                return None
            if text in line:
                return line
        return None

    async def _read(self):
        buf = b""
        while True:
            c = await self.proc.stdout.read(1)
            if not c:
                return
            if c in b"\r\n":
                line = re.sub(rb"\x1b\[[0-9;]*[a-zA-Z]", b"", buf).decode("latin1").strip()
                buf = b""
                m = re.search(r"Notification handle = 0x[0-9a-f]+ value: (.*)", line)
                if m:
                    self._on_notify(None, bytes.fromhex(m.group(1).replace(" ", "")))
                elif line:
                    self._lines.put_nowait(line)
            else:
                buf += c

    async def _send(self, data: bytes):
        await self._cmd("char-write-cmd 0x%04x %s" % (self.handle, data.hex()))
        await asyncio.sleep(0.05)


def open_pedal(a):
    import shutil
    transport = a.transport
    if transport == "auto":
        transport = "gatttool" if sys.platform.startswith("linux") and shutil.which("gatttool") else "bleak"
    return (GatttoolPedal if transport == "gatttool" else Pedal)(name=a.name, address=a.address)


def _slot(slot):
    if not 0 <= slot < SLOTS:
        raise SystemExit("slot must be 0-%d" % (SLOTS - 1))


# ---------------------------------------------------------------- commands

async def run(a):
    models = load_models()
    mk = lambda: open_pedal(a)

    if a.cmd == "info":
        async with mk() as p:
            print(await p.identity())

    elif a.cmd == "list":
        async with mk() as p:
            for s in range(SLOTS):
                print("%2d  %s" % (s, preset_to_dict(decode(await p.read(s)), models)["name"]))

    elif a.cmd in ("show", "export"):
        if a.backup:
            data7 = bytes.fromhex(json.load(open(a.backup))["presets"][str(a.slot)])
        else:
            async with mk() as p:
                data7 = await p.read(a.slot)
        d = preset_to_dict(decode(data7), models)
        if a.cmd == "show":
            print(format_preset(d))
        else:
            d = {"slot": a.slot, **d}
            text = json.dumps(d, indent=1)
            if a.output:
                open(a.output, "w").write(text + "\n")
                print("wrote", a.output)
            else:
                print(text)

    elif a.cmd == "apply":
        d = json.load(open(a.file))
        slot = a.slot if a.slot is not None else d.get("slot")
        if slot is None:
            raise SystemExit("no slot given: use --slot or put \"slot\" in the file")
        async with mk() as p:
            before7 = await p.read(slot)
            try:
                new7 = encode(dict_to_preset(d, decode(before7), models), before7)
            except ValueError as e:
                raise SystemExit("%s: %s" % (a.file, e))
            print(format_preset(preset_to_dict(decode(new7), models)))
            if new7 == before7:
                print("slot %d already holds this preset; nothing written" % slot)
            elif a.dry_run:
                print("dry run: slot %d not written" % slot)
            else:
                await p.write(slot, new7)
                print("slot %d written and verified" % slot)

    elif a.cmd == "backup":
        async with mk() as p:
            ident = await p.identity()
            presets = {}
            for s in range(SLOTS):
                presets[str(s)] = (await p.read(s)).hex()
        json.dump({"device": ident, "read_at": datetime.datetime.now().isoformat(timespec="seconds"), "presets": presets}, open(a.file, "w"), indent=1)
        print("saved %d presets to %s" % (len(presets), a.file))

    elif a.cmd == "restore":
        presets = json.load(open(a.file))["presets"]
        slots = list(range(SLOTS)) if a.all else a.slots
        if not slots:
            raise SystemExit("name the slots to restore, or pass --all to restore every slot")
        async with mk() as p:
            for s in slots:
                data7 = bytes.fromhex(presets[str(s)])
                name = preset_to_dict(decode(data7), models)["name"]
                target = a.to if (a.to is not None and len(slots) == 1) else s
                if await p.read(target) == data7:
                    print("slot %d already holds %r" % (target, name))
                    continue
                await p.write(target, data7, select=False)
                print("slot %d <- %r (backup slot %d), verified" % (target, name, s))

    elif a.cmd == "drum":
        async with mk() as p:
            want = {}
            if a.pattern is not None:
                want["pattern"] = pattern_index(a.pattern)
                await p.cc(CC_DRUM_PATTERN, want["pattern"])
            if a.level is not None:
                want["level"] = _level(a.level, "level")
                await p.cc(CC_DRUM_LEVEL, want["level"])
            if a.bpm is not None:
                want["bpm"] = _level(a.bpm, "bpm", 40, 240)
                await p.set_bpm(want["bpm"])
            if a.on or a.off:
                want["on"] = bool(a.on)
                await p.cc(CC_DRUM_ON, 1 if a.on else 0)
            d = await p.drum()
            print("drum %s   %s   level %d   %d BPM" % ("ON" if d["on"] else "off", pattern_name(d["pattern"]), d["level"], d["bpm"]))
            bad = {k: (v, d[k]) for k, v in want.items() if d[k] != v}
            if bad:
                raise SystemExit("not applied (asked, pedal reports): %s" % bad)

    elif a.cmd == "looper":
        async with mk() as p:
            want = {}
            if a.rec is not None:
                want["rec"] = _level(a.rec, "rec")
                await p.cc(CC_LOOP_REC, want["rec"])
            if a.playback is not None:
                want["playback"] = _level(a.playback, "playback")
                await p.cc(CC_LOOP_PLAY, want["playback"])
            sync_note = ""
            if a.sync is not None:
                await p.cc(CC_LOOP_SYNC, _onoff(a.sync))
                sync_note = "   sync %s sent (the pedal does not report sync, so it is not verified)" % ("on" if _onoff(a.sync) else "off")
            if a.mode is not None:
                want["mode"] = a.mode.upper()
                await p.cc(CC_LOOP_MODE, 1 if want["mode"] == "AR" else 0)
            if a.press:
                st = await p.press(a.press)
                sync_note += "   pressed %s -> %s" % (a.press, LOOP_STATES.get(st, "state %s" % st) if st is not None else "no state reported")
            d = await p.looper()
            print("looper   rec level %d   playback level %d   mode %s%s" % (d["rec"], d["playback"], d["mode"], sync_note))
            bad = {k: (v, d[k]) for k, v in want.items() if d[k] != v}
            if bad:
                raise SystemExit("not applied (asked, pedal reports): %s" % bad)

    elif a.cmd == "tuner":
        async with mk() as p:
            t = await p.tuner()
            if a.on or a.off or a.mute is not None:
                on = True if a.on else False if a.off else t["on"]
                mute = bool(_onoff(a.mute)) if a.mute is not None else t["mute"]
                await p.set_tuner(on, t["ref"], mute)
                got = await p.tuner()
                if (got["on"], got["mute"]) != (on, mute):
                    raise SystemExit("tuner not applied; pedal reports %s" % got)
                t = got
            print("tuner %s   %s while tuning   reference value %d (10 was shown as A4 = 440 Hz)" % ("ON" if t["on"] else "off", "muted" if t["mute"] else "bypass", t["ref"]))

    elif a.cmd == "global":
        async with mk() as p:
            data7 = await p.global_raw()
            g = unpack7(data7)
            if a.trim is not None:
                g[GLOBAL_TRIM] = _level(a.trim, "trim (dB)", -10, 10) + 10
            for off, v, what in ((GLOBAL_BASS, a.bass, "bass"), (GLOBAL_MID, a.mid, "mid"), (GLOBAL_TREBLE, a.treble, "treble")):
                if v is not None:
                    g[off] = _level(v, what)
            if a.loopback is not None:
                g[GLOBAL_LOOPBACK] = _onoff(a.loopback)
            new7 = pack7(g, data7)
            if new7 != data7:
                await p.global_write(new7)
                print("global settings written and verified")
            print("input trim %+d dB   global EQ bass %d  mid %d  treble %d   loopback %s" % (g[GLOBAL_TRIM] - 10, g[GLOBAL_BASS], g[GLOBAL_MID], g[GLOBAL_TREBLE], "on" if g[GLOBAL_LOOPBACK] else "off"))

    elif a.cmd == "select":
        async with mk() as p:
            await p.select(a.slot)
            print("selected slot", a.slot)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="JME", help="text the pedal's Bluetooth name contains (default: JME)")
    ap.add_argument("--address", help="Bluetooth address (Linux/Windows) or device UUID (macOS), instead of searching by name")
    ap.add_argument("--transport", choices=["auto", "bleak", "gatttool"], default="auto",
                    help="auto picks gatttool on Linux when it is installed (see GatttoolPedal), bleak elsewhere")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("list")
    for c in ("show", "export"):
        sp = sub.add_parser(c)
        sp.add_argument("slot", type=int)
        sp.add_argument("--backup", help="read the slot from a backup file instead of the pedal")
        if c == "export":
            sp.add_argument("-o", "--output")
    sp = sub.add_parser("apply")
    sp.add_argument("file")
    sp.add_argument("--slot", type=int)
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("backup")
    sp.add_argument("file")
    sp = sub.add_parser("restore")
    sp.add_argument("file")
    sp.add_argument("slots", type=int, nargs="*")
    sp.add_argument("--to", type=int, help="write a single backup slot to a different slot")
    sp.add_argument("--all", action="store_true")
    sp = sub.add_parser("drum", help="drum machine (global, not part of a preset)")
    sw = sp.add_mutually_exclusive_group()
    sw.add_argument("--on", action="store_true")
    sw.add_argument("--off", action="store_true")
    sp.add_argument("--pattern", help="'Rock 03', or a number 0-99")
    sp.add_argument("--level", type=int)
    sp.add_argument("--bpm", type=int, help="40-240")
    sp = sub.add_parser("looper", help="looper levels and switches (does not press Rec/Stop/Clear/Undo)")
    sp.add_argument("--rec", type=int)
    sp.add_argument("--playback", type=int)
    sp.add_argument("--sync")
    sp.add_argument("--mode", choices=["NR", "AR", "nr", "ar"])
    sp.add_argument("--press", choices=sorted(LOOP_BUTTONS), help="press a looper button; rec twice = record then play; undo is unconfirmed")
    sp = sub.add_parser("tuner", help="tuner on/off and whether the output is muted while tuning")
    sw = sp.add_mutually_exclusive_group()
    sw.add_argument("--on", action="store_true")
    sw.add_argument("--off", action="store_true")
    sp.add_argument("--mute")
    sp = sub.add_parser("global", help="input trim, global EQ, loopback")
    sp.add_argument("--trim", type=int, help="dB; the -10 to +10 range is assumed from one reading (+10)")
    sp.add_argument("--bass", type=int)
    sp.add_argument("--mid", type=int)
    sp.add_argument("--treble", type=int)
    sp.add_argument("--loopback")
    sp = sub.add_parser("select")
    sp.add_argument("slot", type=int)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
