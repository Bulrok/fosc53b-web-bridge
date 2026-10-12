#!/usr/bin/env python3
"""Fosc53B bridge: holds the single allowed connection to the scope and fans the
waveform stream out to any number of browsers over WebSocket, serving the web UI
from ./web on the same port.

    python3 server.py [--port 8053] [--ip SCOPE_IP] [--settings settings.json] [--sim]

Requires Python 3.10+ and the `websockets` package (>= 13).
"""
import argparse, asyncio, json, logging, mimetypes, os, socket, struct, sys, time
from http import HTTPStatus

from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fosc

log = logging.getLogger("bridge")
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")

FRAME_MAGIC = 0xF5
NCH = 5

DEFAULT_SETTINGS = dict(
    timebase=26, ch_en=0x1F, ch_acdc=0, ch_sel=0,
    stall=[9] * NCH, vref=[125] * NCH,
    trig=dict(en=False, ch=0, level=125, rising=True, point=200),
)

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53)); return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

def bcast_for(ip):
    """Best-effort /24 broadcast address for the LAN interface."""
    parts = ip.split(".")
    return ".".join(parts[:3] + ["255"]) if len(parts) == 4 else "255.255.255.255"


class SimDevice:
    """Stand-in for fosc.Fosc53B that synthesises waveforms (server.py --sim).
    Lets the UI be developed and demoed without the scope on the LAN."""
    def __init__(self, on_frame, on_status):
        self.on_frame, self.on_status = on_frame, on_status
        self.timebase = 26; self.ch_en = 0x1F; self.ch_acdc = 0
        self.stall = [9] * 10; self.vref = [125] * 10
        self.trig = dict(en=False, ch=0, level=125, rising=True, point=100)
        self.model_key = "00000000"; self.nch = NCH
        m = fosc.MODELS[self.model_key]
        self.attrs = dict(model="Fosc53B (simulated)", nch=NCH, key=self.model_key, height_extent_max=0,
                          stalls_max=len(m["stall"]) - 1, stalls_org=9, volt_mult=1, abs_volt=False, vol_adj_dis=False, raw="")
        self.status = "connected"; self.last_frame_t = 0.0; self.frames = 0
        self._task = None

    def select_channel(self, ch): pass

    async def connect(self):
        self.on_status("connected")
        self._task = asyncio.create_task(self._run())

    async def close(self):
        if self._task: self._task.cancel()
        self.status = "closed"

    # one test signal per channel: (shape, frequency Hz, peak-to-peak volts, offset volts)
    SIGNALS = [("sine", 200.0, 0.40, 0.0), ("square", 100.0, 0.50, 0.0), ("tri", 50.0, 0.30, 0.0),
               ("mains", 60.0, 0.20, 0.05), ("pulse", 25.0, 0.35, -0.1)]
    FS_MAX = 25000.0          # the MCU's fastest sample rate (1600 samples per 16 x 4 ms screen)

    @staticmethod
    def _wave(shape, ph, rnd):
        import math
        f = ph - math.floor(ph)
        if shape == "sine": return 0.5 * math.sin(2 * math.pi * f)
        if shape == "square": return 0.5 if f < 0.5 else -0.5
        if shape == "tri": return (4 * f - 1) * 0.5 if f < 0.5 else (3 - 4 * f) * 0.5
        if shape == "mains": return 0.5 * math.sin(2 * math.pi * f) + 0.15 * math.sin(2 * math.pi * 3 * f) + (rnd() - 0.5) * 0.25
        return 0.5 if f < 0.2 else -0.5      # pulse

    async def _run(self):
        import random
        rnd = random.random
        t = 0.0                                # simulated signal time (s)
        while True:
            tb = min(self.timebase, fosc.TIME_HARD_MAX)
            tdiv = fosc.TIME_MS[tb] / 1000.0
            dip = max(16, min(1600, round(16 * tdiv * self.FS_MAX)))
            fs = dip / (16 * tdiv)
            roll = tb >= 32                    # 100 ms/div and slower: rolling chunks like the real unit
            n = max(1, round(fs * 0.1)) if roll else 1600
            period = 0.1 if roll else max(1600 / fs, 0.05)
            chans = {}
            for ch in range(NCH):
                if not (self.ch_en >> ch) & 1: continue
                shape, freq, vpp, off = self.SIGNALS[ch]
                vdiv = fosc.stall_volts(self.model_key, self.stall[ch])
                ac = (self.ch_acdc >> ch) & 1
                buf = bytearray(n)
                for i in range(n):
                    v = self._wave(shape, (t + i / fs) * freq, rnd) * vpp + (0 if ac else off) + (rnd() - 0.5) * 0.004
                    buf[i] = max(1, min(254, int(round(self.vref[ch] + v / vdiv * 25))))
                chans[ch] = bytes(buf)
            t += n / fs
            self.frames += 1; self.last_frame_t = time.time()
            self.on_frame(dict(t=self.last_frame_t, seq=self.frames, dip=dip, roll=roll, trig_ecc=0, ch_en=self.ch_en,
                               timebase=tb, nraw=n * len(chans), chans=chans))
            await asyncio.sleep(period)


class Bridge:
    def __init__(self, args):
        self.args = args
        self.dev = None
        self.info = None
        self.clients = {}          # ServerConnection -> asyncio.Queue
        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.recent = []           # last few binary frames for new clients
        self.status = "searching"
        self.fps_t = time.time(); self.fps_n = 0; self.fps = 0.0
        self._save_handle = None
        self.local_ip = lan_ip()
        self.load_settings()

    # ---------------------------------------------------------------- settings
    def load_settings(self):
        p = self.args.settings
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    s = json.load(f)
                self.apply_settings(s, save=False, push=False)
                log.info("settings loaded from %s", p)
            except Exception as e:
                log.warning("settings load failed: %s", e)

    def save_settings_later(self):
        if not self.args.settings: return
        loop = asyncio.get_running_loop()
        if self._save_handle: self._save_handle.cancel()
        self._save_handle = loop.call_later(1.0, self._save_now)

    def _save_now(self):
        try:
            with open(self.args.settings, "w") as f:
                json.dump(self.settings, f, indent=1)
        except Exception as e:
            log.warning("settings save failed: %s", e)

    def limits(self):
        a = self.dev.attrs if self.dev and self.dev.attrs else None
        model = fosc.MODELS.get(self.dev.model_key if self.dev else "", fosc.MODELS["00000000"])
        return dict(tmin=model["tmin"], tmax=fosc.TIME_HARD_MAX,
                    stall_max=(a["stalls_max"] if a else len(model["stall"]) - 1),
                    nch=(a["nch"] if a else NCH))

    def apply_settings(self, s, save=True, push=True):
        """Merge a (partial) settings dict, clamp, push to the device, broadcast."""
        lim = self.limits()
        cur = self.settings
        if "timebase" in s: cur["timebase"] = max(lim["tmin"], min(lim["tmax"], int(s["timebase"])))
        if "ch_en" in s: cur["ch_en"] = int(s["ch_en"]) & ((1 << lim["nch"]) - 1)
        if "ch_acdc" in s: cur["ch_acdc"] = int(s["ch_acdc"]) & 0xFF
        if "ch_sel" in s: cur["ch_sel"] = max(0, min(lim["nch"] - 1, int(s["ch_sel"])))
        if "stall" in s:
            for i, v in enumerate(s["stall"][:NCH]):
                if v is not None: cur["stall"][i] = max(1, min(lim["stall_max"], int(v)))
        if "vref" in s:
            for i, v in enumerate(s["vref"][:NCH]):
                if v is not None: cur["vref"][i] = max(50, min(200, int(v)))
        if "trig" in s:
            t = cur["trig"]; n = s["trig"]
            if "en" in n: t["en"] = bool(n["en"])
            if "ch" in n: t["ch"] = max(0, min(lim["nch"] - 1, int(n["ch"])))
            if "level" in n: t["level"] = max(1, min(254, int(n["level"])))
            if "rising" in n: t["rising"] = bool(n["rising"])
            if "point" in n: t["point"] = max(0, min(1600, int(n["point"])))
        if self.dev: self.push_to_device()
        if save: self.save_settings_later()
        if push: self.broadcast_json(dict(type="settings", settings=self.settings))

    def push_to_device(self):
        d, s = self.dev, self.settings
        d.timebase = s["timebase"]; d.ch_en = s["ch_en"]; d.ch_acdc = s["ch_acdc"]
        for i in range(NCH):
            d.stall[i] = s["stall"][i]; d.vref[i] = s["vref"][i]
        d.trig = dict(en=s["trig"]["en"], ch=s["trig"]["ch"], level=s["trig"]["level"],
                      rising=s["trig"]["rising"], point=s["trig"]["point"])
        d.select_channel(s["ch_sel"])

    # ---------------------------------------------------------------- device
    async def device_loop(self):
        while True:
            try:
                await self._device_session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("device session error: %s", e)
            await asyncio.sleep(2)

    async def _device_session(self):
        # 1. find the scope
        while True:
            if self.args.sim:
                self.info = dict(ip="simulated", data_port=0, ctr_port=0, name="Fosc53B (simulated)", model="Fosc53B",
                                 mac="", connected=False, licensed=True, ver_soft=0, ver_hard=0)
                break
            if self.args.ip:
                self.info = dict(ip=self.args.ip, data_port=8992, ctr_port=8993, name="Fosc53B", model="Fosc53B",
                                 mac="", connected=False, licensed=True, ver_soft=0, ver_hard=0)
                break
            self.set_status("searching")
            devs = await fosc.discover(local_ip=self.local_ip, bcast=bcast_for(self.local_ip), timeout=1.5)
            free = [d for d in devs if not d["connected"]]
            if free:
                self.info = free[0]; break
            if devs:
                self.set_status("busy")           # another app holds the scope
                log.info("scope %s is in use by another client", devs[0]["ip"])
            await asyncio.sleep(2)
        log.info("using scope %s", self.info)
        self.broadcast_json(self.hello())
        # 2. run a session until it stalls for good
        if self.args.sim:
            self.dev = SimDevice(on_frame=self.on_frame, on_status=self.set_status)
        else:
            self.dev = fosc.Fosc53B(self.info["ip"], self.info["data_port"], self.info["ctr_port"],
                                    on_frame=self.on_frame, on_status=self.set_status)
        self.push_to_device()
        await self.dev.connect()
        last_ok = time.time()
        try:
            while True:
                await asyncio.sleep(1)
                if self.dev.status == "connected" and time.time() - self.dev.last_frame_t < 5:
                    last_ok = time.time()
                if self.dev.attrs and not getattr(self, "_attrs_sent", False):
                    self._attrs_sent = True
                    self.apply_settings({}, save=False)         # re-clamp with real limits
                    self.broadcast_json(self.hello())
                if time.time() - last_ok > 30 and not (self.args.ip or self.args.sim):
                    log.warning("no data for 30 s, rediscovering")
                    break
        finally:
            await self.dev.close()
            self.dev = None; self._attrs_sent = False

    def set_status(self, s):
        self.status = s
        self.broadcast_json(dict(type="status", status=s, fps=round(self.fps, 1)))

    def on_frame(self, f):
        self.fps_n += 1
        now = time.time()
        if now - self.fps_t >= 1.0:
            self.fps = self.fps_n / (now - self.fps_t); self.fps_n = 0; self.fps_t = now
            self.broadcast_json(dict(type="status", status=self.status, fps=round(self.fps, 1)))
        chans = f["chans"]
        mask = 0
        for c in chans: mask |= 1 << c
        n = max((len(v) for v in chans.values()), default=0)
        if n == 0: return
        hdr = struct.pack("<BBHBBHId", FRAME_MAGIC, mask, f["dip"], f["timebase"], 1 if f["roll"] else 0, n, f["seq"] & 0xFFFFFFFF, f["t"] * 1000.0)
        body = b"".join(chans[c].ljust(n, b"\0") for c in sorted(chans))
        pkt = hdr + body
        self.recent.append(pkt)
        if len(self.recent) > 6: self.recent.pop(0)
        for q in self.clients.values():
            if q.full():
                try: q.get_nowait()
                except asyncio.QueueEmpty: pass
            q.put_nowait(pkt)

    # ---------------------------------------------------------------- clients
    def hello(self):
        model = fosc.MODELS.get(self.dev.model_key if self.dev else "", fosc.MODELS["00000000"])
        return dict(type="hello", device=self.info, status=self.status, fps=round(self.fps, 1),
                    attrs=(self.dev.attrs if self.dev else None), settings=self.settings, limits=self.limits(),
                    tables=dict(time=[[fosc.TIME_VOL[i], fosc.TIME_UNIT[i]] for i in range(len(fosc.TIME_VOL))],
                                time_ms=fosc.TIME_MS, stall=[[model["stall"][i], model["unit"][i]] for i in range(len(model["stall"]))]),
                    url=f"http://{self.local_ip}:{self.args.port}/")

    def broadcast_json(self, obj):
        msg = json.dumps(obj)
        for q in self.clients.values():
            if not q.full(): q.put_nowait(msg)

    async def ws_handler(self, ws):
        q = asyncio.Queue(maxsize=12)
        self.clients[ws] = q
        log.info("client %s connected (%d)", ws.remote_address, len(self.clients))
        try:
            await ws.send(json.dumps(self.hello()))
            for pkt in self.recent: await ws.send(pkt)
            sender = asyncio.create_task(self._sender(ws, q))
            async for msg in ws:
                if isinstance(msg, bytes): continue
                try: m = json.loads(msg)
                except ValueError: continue
                t = m.get("type")
                if t == "set" and isinstance(m.get("settings"), dict):
                    self.apply_settings(m["settings"])
                elif t == "hello":
                    await ws.send(json.dumps(self.hello()))
            sender.cancel()
        finally:
            self.clients.pop(ws, None)
            log.info("client %s gone (%d)", ws.remote_address, len(self.clients))

    async def _sender(self, ws, q):
        while True:
            item = await q.get()
            await ws.send(item)

    # ---------------------------------------------------------------- http
    def process_request(self, connection, request):
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        if path == "/": path = "/index.html"
        fp = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not fp.startswith(WEB) or not os.path.isfile(fp):
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f: body = f.read()
        headers = Headers([("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else "")),
                           ("Content-Length", str(len(body))), ("Cache-Control", "no-cache"), ("Connection", "close")])
        return Response(200, "OK", headers, body)

    async def run(self):
        asyncio.create_task(self.device_loop())
        async with serve(self.ws_handler, "0.0.0.0", self.args.port, process_request=self.process_request,
                         compression=None, max_size=None, ping_interval=20, ping_timeout=20):
            log.info("web UI: http://%s:%d/   (this machine: http://localhost:%d/)", self.local_ip, self.args.port, self.args.port)
            await asyncio.Future()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8053)
    ap.add_argument("--ip", help="scope IP (skips UDP discovery)")
    ap.add_argument("--sim", action="store_true", help="no hardware: serve synthetic waveforms (UI development / demo)")
    ap.add_argument("--settings", default=os.path.join(HERE, "settings.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.CRITICAL)   # port probes (HEAD, empty connects) are noise
    try:
        asyncio.run(Bridge(args).run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
