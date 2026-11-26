#!/usr/bin/env python3
"""lekato_web.py - a small touch page for the pedal: pick a preset, adjust its main knobs live, save when told to.

Runs next to lekato.py and needs nothing beyond it (plain asyncio HTTP server, no web framework).

  lekato_web.py                      serve on port 8766 and talk to the pedal
  lekato_web.py --mock backup.json   no Bluetooth: serve a simulated pedal from a backup file (for working on the page)

The pedal accepts one Bluetooth connection, so the page connects only when asked and lets go after --idle seconds
without use, which frees the pedal for the phone app.
"""
import argparse, asyncio, json, os, time

import lekato as L

HERE = os.path.dirname(os.path.abspath(__file__))
KNOB_CC = {"OD-FX": 12, "Amp": 18, "Cab": 26, "Mod": 44, "Delay": 48, "Reverb": 54, "Gate": 8, "EQ": 32}   # first knob's controller
CC_VOLUME = 58
SHOWN = ["OD-FX", "Amp", "EQ", "Mod", "Delay", "Reverb", "Gate"]       # Cab (mic, position, cuts) is deliberately left out
# Knobs that are switches or lists rather than 0-100. Harmony's ranges are assumptions from the app's description.
KNOB_MAX = {"mode": 1, "on/off": 1, "key": 11, "harm": 7}


class MockPedal:
    """Stands in for the pedal: 99 presets from a backup file, an edit buffer, and the same calls the page uses."""

    def __init__(self, path):
        self.presets = {int(k): bytes.fromhex(v) for k, v in json.load(open(path))["presets"].items()}
        self.slot = 0
        self.edit = bytearray(L.decode(self.presets[0]))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def identity(self):
        return "MOCK LEKATO JME-01"

    async def read(self, slot):
        return self.presets[slot]

    async def write(self, slot, data7, select=True):
        self.presets[slot] = bytes(data7)

    async def select(self, slot):
        self.slot = slot
        self.edit = bytearray(L.decode(self.presets[slot]))

    async def cc(self, number, value):
        if number == CC_VOLUME:
            self.edit[L.VOLUME] = value
        elif number < 8:
            self.edit[L.BLOCKS[L.BLOCK_IDS[number]][0]] = value
        else:
            for block, base in KNOB_CC.items():
                cap = L.BLOCKS[block][2]
                if base <= number < base + cap:
                    self.edit[L.BLOCKS[block][1] + 1 + number - base] = value

    async def edit_buffer(self):
        return L.encode(self.edit, self.presets[self.slot])


async def edit_buffer(pedal) -> bytes:
    """The live, unsaved preset as the pedal holds it."""
    if isinstance(pedal, MockPedal):
        return await pedal.edit_buffer()
    r = await pedal.ask(b"\x70\x0c" + bytes(14))
    if r[:3] != b"\x70\x0c\x02" or len(r) < 100:
        raise RuntimeError("unexpected edit-buffer reply")
    return r[4:]


class Session:
    def __init__(self, args):
        self.args = args
        self.models = L.load_models()
        self.lock = asyncio.Lock()
        self.pedal = None
        self.status = "released"            # released | connecting | connected | error
        self.error = ""
        self.names = self._load_names()
        self.names_complete = len(self.names) == L.SLOTS
        self.slot = None                    # slot the page selected; None = unknown
        self.base7 = None                   # that slot's saved content
        self.live = None                    # decoded live preset as this page believes it to be
        self.last_use = time.time()

    # ---- preset-name cache, so connecting does not need 99 reads every time
    def _names_path(self):
        return os.path.join(HERE, "data", "preset_names.json")

    def _load_names(self):
        try:
            return {int(k): v for k, v in json.load(open(self._names_path())).items()}
        except (OSError, ValueError):
            return {}

    def _save_names(self):
        try:
            os.makedirs(os.path.dirname(self._names_path()), exist_ok=True)
            json.dump({str(k): v for k, v in sorted(self.names.items())}, open(self._names_path(), "w"), indent=1)
        except OSError:
            pass

    def _name_of(self, data7):
        return bytes(L.decode(data7)[L.NAME:L.NAME + L.NAME_LEN]).split(b"\0")[0].decode("ascii", "replace")

    # ---- connection
    async def connect(self):
        if self.pedal is not None:
            return
        self.status, self.error = "connecting", ""
        try:
            pedal = MockPedal(self.args.mock) if self.args.mock else L.open_pedal(self.args)
            self.pedal = await pedal.__aenter__()
            await self.pedal.identity()
            self.status = "connected"
            await self._sync_from_pedal()
            if not self.names_complete:
                asyncio.ensure_future(self._read_all_names())
        except (SystemExit, Exception) as e:      # lekato.py reports failures with SystemExit
            self.pedal = None
            self.status, self.error = "error", str(e) or e.__class__.__name__

    async def release(self):
        pedal, self.pedal = self.pedal, None
        self.status = "released"
        if pedal is not None:
            try:
                await pedal.__aexit__(None, None, None)
            except Exception:
                pass

    async def _read_all_names(self):
        for s in range(L.SLOTS):
            if s in self.names:
                continue
            async with self.lock:
                if self.pedal is None:
                    return
                try:
                    self.names[s] = self._name_of(await self.pedal.read(s))
                except (SystemExit, Exception):
                    return
        self.names_complete = True
        self._save_names()
        async with self.lock:
            if self.pedal is not None and self.slot is None:
                try:
                    await self._sync_from_pedal()            # now that every name is known, find the live preset's slot
                except (SystemExit, Exception):
                    pass

    async def _sync_from_pedal(self):
        """Work out what is live on the pedal: read the edit buffer and find the slot it came from by name."""
        live7 = await edit_buffer(self.pedal)
        name = self._name_of(live7)
        self.slot, self.base7 = None, None
        for s in sorted(k for k, v in self.names.items() if v == name):
            saved = await self.pedal.read(s)
            if self._name_of(saved) == name:
                self.slot, self.base7 = s, saved
                break
        self.live = bytearray(L.decode(live7))

    # ---- actions
    async def select(self, slot):
        await self.pedal.select(slot)
        self.base7 = await self.pedal.read(slot)
        self.slot = slot
        self.live = bytearray(L.decode(self.base7))
        self.names[slot] = self._name_of(self.base7)

    async def knob(self, block, index, value):
        mb, cb, cap = L.BLOCKS[block]
        if not 0 <= index < min(self.live[cb], cap) or not 0 <= value <= 127:
            raise ValueError("no such knob")
        await self.pedal.cc(KNOB_CC[block] + index, value)
        self.live[cb + 1 + index] = value

    async def toggle(self, block, on):
        mb = L.BLOCKS[block][0]
        v = (self.live[mb] & 0x3F) | (0 if on else L.BYPASS)
        await self.pedal.cc(L.BLOCK_IDS.index(block), v)      # same model, so the pedal keeps the knob values
        self.live[mb] = v

    async def volume(self, value):
        await self.pedal.cc(CC_VOLUME, value)
        self.live[L.VOLUME] = value

    async def save(self):
        if self.slot is None:
            raise ValueError("select a preset first")
        live7 = await edit_buffer(self.pedal)                  # save what the pedal actually holds, not this page's copy
        await self.pedal.write(self.slot, live7, select=False)
        self.base7 = live7
        self.live = bytearray(L.decode(live7))
        self.names[self.slot] = self._name_of(live7)
        self._save_names()

    async def revert(self):
        if self.slot is None:
            raise ValueError("select a preset first")
        await self.select(self.slot)

    # ---- what the page draws
    def state(self):
        out = {"status": self.status, "error": self.error, "mock": bool(self.args.mock),
               "names": [self.names.get(s, "") for s in range(L.SLOTS)], "names_complete": self.names_complete,
               "slot": self.slot, "preset": None, "dirty": False, "idle": self.args.idle}
        if self.pedal is not None and self.live is not None:
            d = L.preset_to_dict(self.live, self.models)
            blocks = []
            for b in SHOWN:
                info = d["blocks"][b]
                knobs = [{"name": n, "value": v, "max": KNOB_MAX.get(L._norm(n), 100)} for n, v in info["knobs"].items()]
                blocks.append({"block": b, "on": info["on"], "model": info["model_name"] or "model %d" % info["model"], "knobs": knobs})
            out["preset"] = {"name": d["name"], "volume": d["volume"], "blocks": blocks}
            out["dirty"] = self.base7 is not None and bytes(self.live) != bytes(L.decode(self.base7))
        return out


async def handle_api(sess: Session, path: str, body: dict) -> dict:
    sess.last_use = time.time()
    if path == "/api/state":
        return sess.state()
    async with sess.lock:
        try:
            if path == "/api/connect":
                await sess.connect()
            elif path == "/api/release":
                await sess.release()
            elif sess.pedal is None:
                raise ValueError("not connected")
            elif path == "/api/select":
                await sess.select(int(body["slot"]))
            elif path == "/api/knob":
                await sess.knob(body["block"], int(body["index"]), int(body["value"]))
            elif path == "/api/toggle":
                await sess.toggle(body["block"], bool(body["on"]))
            elif path == "/api/volume":
                await sess.volume(int(body["value"]))
            elif path == "/api/save":
                await sess.save()
            elif path == "/api/revert":
                await sess.revert()
            else:
                return {"http": 404}
            sess.error = sess.error if sess.status == "error" else ""
        except (KeyError, ValueError) as e:
            sess.error = str(e)
        except (SystemExit, Exception) as e:                  # the link dropped, or the pedal did not answer
            await sess.release()
            sess.status, sess.error = "error", str(e) or e.__class__.__name__
    return sess.state()


async def serve_client(sess: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode("latin1").split("\r\n")
        method, target = lines[0].split(" ")[:2]
        headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        raw = await reader.readexactly(int(headers.get("content-length", 0) or 0))
        path = target.split("?")[0]
        if path.startswith("/api/"):
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            result = await handle_api(sess, path, body)
            code = result.pop("http", 200) if isinstance(result, dict) else 200
            payload, ctype = json.dumps(result).encode(), "application/json"
        elif path in ("/", "/index.html"):
            payload, ctype, code = open(os.path.join(HERE, "web", "index.html"), "rb").read(), "text/html; charset=utf-8", 200
        else:
            payload, ctype, code = b"not found", "text/plain", 404
        writer.write(("HTTP/1.1 %d OK\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n" % (code, ctype, len(payload))).encode() + payload)
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def idle_watch(sess: Session):
    while True:
        await asyncio.sleep(15)
        if sess.pedal is not None and sess.args.idle and time.time() - sess.last_use > sess.args.idle:
            async with sess.lock:
                await sess.release()


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--idle", type=int, default=600, help="let go of the pedal after this many seconds without use (0 = never)")
    ap.add_argument("--mock", help="serve a simulated pedal from this backup file instead of using Bluetooth")
    ap.add_argument("--name", default="JME")
    ap.add_argument("--address")
    ap.add_argument("--transport", choices=["auto", "bleak", "gatttool"], default="auto")
    args = ap.parse_args()
    sess = Session(args)
    server = await asyncio.start_server(lambda r, w: serve_client(sess, r, w), args.bind, args.port)
    asyncio.ensure_future(idle_watch(sess))
    print("lekato_web on http://%s:%d%s" % (args.bind, args.port, "  (mock)" if args.mock else ""), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
