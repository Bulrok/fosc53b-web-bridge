"""Fosc53B (focussz.com) Wi-Fi oscilloscope protocol client. Stdlib only.

Wire protocol (reverse engineered from the vendor Android app):
  * UDP 8991  : discovery broadcast, 28-byte request -> 112-byte reply
  * TCP ctr   : serial-bridge control (baud/format/GPIO) + 1 s heartbeat
  * TCP data  : raw serial stream to the scope MCU (challenge handshake,
                16-byte control packets every 300 ms, framed sample bursts)
"""
import asyncio, socket, struct, time, logging

log = logging.getLogger("fosc")

# ---------------------------------------------------------------- CRC / framing
def _make_table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        t.append(c)
    return t
_T = _make_table()

def crc32(seed, data):
    c = seed & 0xFFFFFFFF
    for b in data:
        c = _T[(c ^ b) & 0xFF] ^ (c >> 8)
    return c & 0xFFFFFFFF

HEAD = 0x20180205
CRC_SEED = 0x5A5A5A5A

def frame(total_len, payload):
    """Build a bridge packet: head, len, payload@8, crc32 over everything before the crc."""
    buf = bytearray(total_len)
    buf[0:4] = struct.pack('<I', HEAD)
    buf[4:8] = struct.pack('<I', total_len)
    buf[8:8 + len(payload)] = payload
    buf[-4:] = struct.pack('<I', crc32(CRC_SEED, bytes(buf[:-4])))
    return bytes(buf)

def frame_ok(data):
    return (len(data) >= 12 and struct.unpack('<I', data[:4])[0] == HEAD
            and struct.unpack('<I', data[4:8])[0] == len(data)
            and struct.unpack('<I', data[-4:])[0] == crc32(CRC_SEED, data[:-4]))

# ---------------------------------------------------------------- tables (from the app)
TIME_VOL  = [1,2,4,5,10,20,40,50,100,200,400,500, 1,2,4,5,10,20,40,50,100,200,400,500, 1,2,4,5,10,20,40,50,100,200,400,500, 1,2,4,8,16,20,40,60,100,200,400,500,900,1800, 1,2,3]
TIME_UNIT = ['ns']*12 + ['us']*12 + ['ms']*12 + ['s']*14 + ['h']*3
TIME_MS   = [1e-6,2e-6,4e-6,5e-6,1e-5,2e-5,4e-5,5e-5,1e-4,2e-4,4e-4,5e-4, 0.001,0.002,0.004,0.005,0.01,0.02,0.04,0.05,0.1,0.2,0.4,0.5, 1,2,4,5,10,20,40,50,100,200,400,500, 1000,2000,4000] + [4000]*14
TIME_INV  = [1]*39 + [2,4,5,10,15,25,50,100,125,225,450,900,1800,2700]
TIME_HARD_MAX = 38   # indices above this are display-only decimation of the 4 s/div stream

MODELS = {
    '00000000': dict(name='Fosc53B',   tmin=20, stall=[1,2,5,10,20,25,40,50,80, 0.1,0.5,1,2,5,10,20,50,100,200,500,1000,2000],
                     unit=['mV']*9 + ['V']*13),
    '4E57DFAC': dict(name='Fosc53B V2', tmin=13, stall=[1,2,5,10,20,25,40,50,80, 0.1,0.2,0.5,1,2,5,10,20,50,100,200,500,1000,2000],
                     unit=['mV']*9 + ['V']*14),
}

def stall_volts(model, idx):
    """Volts per division for stall index idx."""
    m = MODELS.get(model, MODELS['00000000'])
    if idx >= len(m['stall']): idx = len(m['stall']) - 1
    v = m['stall'][idx]
    return v / 1000.0 if m['unit'][idx] == 'mV' else float(v)

# ---------------------------------------------------------------- discovery
async def discover(local_ip=None, bcast='255.255.255.255', timeout=2.0, port=8991):
    """Broadcast a FindDev request; return list of device-info dicts."""
    loop = asyncio.get_running_loop()
    if local_ip is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 53)); local_ip = s.getsockname()[0]
        except OSError:
            local_ip = '0.0.0.0'
        finally:
            s.close()
    found = {}
    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if len(data) != 112 or not frame_ok(data): return
            info = dict(
                ip=socket.inet_ntoa(data[22:26]), mac=data[16:22].hex(':'),
                data_port=struct.unpack('<H', data[28:30])[0], ctr_port=struct.unpack('<H', data[30:32])[0],
                connected=bool(data[32] & 1), licensed=bool(data[32] & 2),
                model=data[52:64].split(b'\0')[0].decode(errors='replace'),
                ver_soft=struct.unpack('<H', data[64:66])[0], ver_hard=struct.unpack('<H', data[66:68])[0],
                idle=struct.unpack('<H', data[68:70])[0],
                name=data[72:92].split(b'\0')[0].decode(errors='replace'))
            found[info['ip']] = info
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(('', port))
    sock.setblocking(False)
    transport, _ = await loop.create_datagram_endpoint(P, sock=sock)
    try:
        payload = socket.inet_aton(local_ip) + b'\0\0' + struct.pack('<H', port) + b'FindDev\0'
        req = frame(28, payload)
        for target in {bcast, '255.255.255.255'}:
            try: transport.sendto(req, (target, port))
            except OSError as e: log.warning("bcast %s: %s", target, e)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return list(found.values())

# ---------------------------------------------------------------- device
class Fosc53B:
    """One live session with a scope. Call connect(); frames arrive via on_frame(frame_dict)."""
    NCH_MAX = 8

    def __init__(self, ip, data_port=8992, ctr_port=8993, on_frame=None, on_status=None):
        self.ip, self.data_port, self.ctr_port = ip, data_port, ctr_port
        self.on_frame = on_frame or (lambda f: None)
        self.on_status = on_status or (lambda s: None)
        # user settings
        self.timebase = 26            # index into TIME_* tables (hardware uses min(idx, 38))
        self.ch_en = 0x1F             # bitmask of enabled channels
        self.ch_acdc = 0              # bitmask: 1 = AC coupling
        self.stall = [8] * 10         # volts/div index per channel
        self.vref = [125] * 10        # vertical reference (50..200, 0..250 screen units)
        self.trig = dict(en=False, ch=0, level=125, rising=True, point=100)
        # device attributes (from handshake)
        self.attrs = None
        self.model_key = ''
        self.nch = 5
        # device data-control state (from frame trailers)
        self.dip_size = 1600
        self.roll = False
        self.trig_ecc = 0
        self.ch_shift = 0
        self.ch_ret = 0
        self.ch_two = 0
        # link state
        self.ctr_w = self.data_w = None
        self._tasks = []
        self._io_st = 0
        self._dtr_ticks = 5
        self._ctr_send_check = None
        self._ctr_resend = False
        self._pass = None
        self._pass_ret = 0
        self.handshake_ok = False
        self._rx_step = 0
        self._rx_small = bytearray()
        self._rx_buf = bytearray()
        self._rx_sum = 0
        self._adj_sel = 0
        self._send_cnt = 0
        self._last_sum = None
        self.frames = 0
        self.last_frame_t = 0.0
        self.status = 'idle'

    # ---- public
    async def connect(self):
        self._set_status('connecting')
        self._tasks = [asyncio.create_task(self._ctr_loop()), asyncio.create_task(self._data_loop())]

    async def close(self):
        for t in self._tasks: t.cancel()
        for w in (self.ctr_w, self.data_w):
            if w:
                try: w.close()
                except Exception: pass
        self._set_status('closed')

    def timebase_label(self, idx=None):
        i = self.timebase if idx is None else idx
        return f"{TIME_VOL[i]} {TIME_UNIT[i]}/div"

    # ---- status
    def _set_status(self, s):
        if s != self.status:
            self.status = s
            log.info("status: %s", s)
            self.on_status(s)

    # ---- control channel (serial-bridge config + heartbeat + DTR)
    def _ctr_all(self):
        # type 0xF0, baud 2 000 000, 8N1 (ByteSize=3, StopBits=1, Parity_mod=2, Parity_en=0, flow_ctrl=1)
        bits = (3 & 3) | ((1 << 2) & 12) | ((2 << 4) & 48) | ((0 << 6) & 64) | ((1 << 7) & 384)
        payload = bytes([0xF0, 0, 0, 0]) + struct.pack('<I', 2000000) + struct.pack('<H', bits) + b'\0' * 6 + struct.pack('<H', self._io_st) + b'\0\0'
        pkt = frame(32, payload)
        self._ctr_send_check = struct.unpack('<I', pkt[28:32])[0]
        return pkt

    def _heart(self):
        return frame(20, bytes([1, 0, 0, 0]) + struct.pack('<I', 0))

    def _set_dtr(self, on):
        want = (self._io_st & ~0xF) | (9 if on else 11)
        if want != self._io_st:
            self._io_st = want
            self._ctr_send(self._ctr_all())

    def _ctr_send(self, pkt):
        if self.ctr_w and not self.ctr_w.is_closing():
            self.ctr_w.write(pkt)

    async def _ctr_loop(self):
        while True:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(self.ip, self.ctr_port), 5)
                self.ctr_w = w
                log.info("ctr connected")
                self._dtr_ticks = 5
                self._io_st = 0
                self._ctr_send(self._ctr_all())
                rx = asyncio.create_task(self._ctr_rx(r))
                tick = 0
                while not w.is_closing():
                    await asyncio.sleep(0.03)
                    if self._dtr_ticks > 0:
                        self._dtr_ticks -= 1; self._set_dtr(True)
                    else:
                        self._set_dtr(False)
                    tick += 1
                    if tick >= 33:
                        tick = 0
                        if self._ctr_resend:
                            self._ctr_resend = False; self._ctr_send(self._ctr_all())
                        else:
                            self._ctr_send(self._heart())
                    if rx.done(): break
                rx.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ctr: %s", e)
            self.ctr_w = None
            await asyncio.sleep(1.0)

    async def _ctr_rx(self, r):
        while True:
            data = await r.read(4096)
            if not data: return
            if len(data) >= 12 and frame_ok(data) and len(data) == 32:
                check = struct.unpack('<I', data[28:32])[0]
                if check != self._ctr_send_check:
                    self._ctr_resend = True
                log.debug("ctr rx baud=%d bits=%04x io=%04x match=%s", struct.unpack('<I', data[12:16])[0],
                          struct.unpack('<H', data[16:18])[0], struct.unpack('<H', data[24:26])[0], check == self._ctr_send_check)

    # ---- data channel
    def _make_pass(self):
        """Challenge bytes FF 05 xx xx xx xx and the expected 32-bit answer."""
        p = 0x03040506 if self._pass is None else self._pass
        while True:
            p = (int(time.time() * 1000) + p + 0x73061212 + 1) & 0xFFFFFFFF
            buf = bytearray(b'\xff\x05' + struct.pack('<I', p))
            p = crc32(0x12345678, bytes(buf))
            buf[2:6] = struct.pack('<I', p)
            for i in range(2, 6):
                if buf[i] == 0xFF: buf[i] = 0xEF
            p = struct.unpack('<I', buf[2:6])[0]
            r0 = (p ^ 0x78110224) ^ (((p >> 16) & 0xFFFF) | 0x55550000) ^ (((p << 16) & 0xFFFF0000) | 0xAAAA) ^ (((p << 24) & 0xFF000000) | 0xAAAAAA)
            r1 = (p ^ 0x20090612) ^ (((p >> 16) & 0xFFFF) | 0x55550000) ^ (((p << 16) & 0xFFFF0000) | 0x5555) ^ (((p << 24) & 0xFF000000) | 0xBBBBBB)
            r0 &= 0xFFFFFFFF; r1 &= 0xFFFFFFFF
            if 0xFF in struct.pack('<II', r0, r1):
                continue
            self._pass, self._pass_ret = p, r0
            return bytes(buf)

    def _enabled_list(self):
        return [i for i in range(self.nch) if (self.ch_ret >> i) & 1]

    def _ctrl_packet(self, adj_ch):
        """16-byte scope control packet (vendor: sNetConnectDeviceDataCtrSend)."""
        adj_ch %= max(1, self.nch)
        b = bytearray(16)
        b[0] = 0xFF; b[1] = 0x0F
        b[2] = (min(self.timebase, TIME_HARD_MAX) & 63) | 0x40          # soft_flag
        t = self.trig
        trig_en = t['en'] and ((self.ch_en >> t['ch']) & 1)
        b[3] = t['level'] & 0xFF
        w = (t['point'] & 0x7FF) | ((t['ch'] << 11) & 0x3800)
        if t['rising']: w |= 0x4000
        if trig_en: w |= 0x8000
        w |= (self.stall[adj_ch] << 16) & 0xFF0000
        w |= (self.vref[adj_ch] << 24) & 0xFF000000
        b[4:8] = struct.pack('<I', w & 0xFFFFFFFF)
        b[8] = (adj_ch & 7) | ((2 << 5) & 0xF8)                        # dev_connect_type = 2
        b[9] = self.ch_en & 0xFF
        b[10] = 13 & 63                                                 # vol0
        b[11] = self.ch_acdc & 0xFF
        b[15] = sum(b[2:15]) & 0xFF
        return bytes(b)

    def _next_ctrl(self):
        """Alternate between the 'selected' channel and the others so every channel's stall/vref reaches the scope."""
        self._send_cnt += 1
        en = [i for i in range(self.nch) if (self.ch_en >> i) & 1] or [0]
        if self._send_cnt & 1:
            ch = en[(self._send_cnt // 2) % len(en)]
        else:
            ch = self._adj_sel if (self.ch_en >> self._adj_sel) & 1 else en[0]
        pkt = self._ctrl_packet(ch)
        if not (self._send_cnt & 1):
            if pkt[15] != self._last_sum:
                self._last_sum = pkt[15]
                self._dtr_ticks = 30          # pulse DTR ~1 s so the MCU reloads settings
        return pkt

    def select_channel(self, ch):
        self._adj_sel = ch % max(1, self.nch)

    async def _data_loop(self):
        while True:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(self.ip, self.data_port), 5)
                self.data_w = w
                self.handshake_ok = False
                self._rx_step = 0; self._rx_buf.clear(); self._rx_small.clear(); self._rx_sum = 0
                log.info("data connected")
                self._set_status('handshake')
                rx = asyncio.create_task(self._data_rx(r))
                while not w.is_closing() and not rx.done():
                    if not self.handshake_ok:
                        w.write(self._make_pass())
                    else:
                        w.write(self._next_ctrl())
                    await w.drain()
                    await asyncio.sleep(0.3)
                rx.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("data: %s", e)
            self.data_w = None
            self.handshake_ok = False
            self._set_status('reconnecting')
            await asyncio.sleep(1.0)

    async def _data_rx(self, r):
        while True:
            data = await r.read(65536)
            if not data: return
            for b in data:
                self._rx_byte(b)

    def _rx_byte(self, b):
        if b == 0xFF:
            self._rx_step = -1          # next byte is the record type
            self._rx_small.clear()
            return
        st = self._rx_step
        if st == -1:
            self._rx_step = b
            if b not in (2, 15):
                # start of a sample burst: bytes accumulate until the FF 02 trailer
                pass
            return
        if st == 2 or st == 15:
            self._rx_small.append(b)
            if len(self._rx_small) >= 14:
                rec = bytes(self._rx_small); self._rx_small.clear(); self._rx_step = 0
                if st == 2: self._on_trailer(rec)
                else: self._on_password(rec)
            return
        # sample byte
        if len(self._rx_buf) < 65536:
            self._rx_buf.append(b)
            self._rx_sum = (self._rx_sum + b) & 0xFFFF

    def _on_password(self, rec):
        got = struct.unpack('<I', rec[0:4])[0]
        if got != self._pass_ret:
            log.warning("handshake mismatch: got %08x want %08x", got, self._pass_ret)
            return
        b4 = rec[4]
        nch = (b4 & 7) + 1
        model = 'Fosc' + '0123456789ABCDEF'[nch & 15] + '0123456789ABCDEF'[(b4 >> 4) & 15] + chr(rec[5])
        if rec[7]: model += ' ' + chr(rec[7])
        key = rec[10:14].hex().upper()
        stalls_max = min(rec[6] & 31, 31)
        stalls_org = max((rec[9] >> 3) & 31, 1)
        mult = 10 ** (rec[9] & 7)
        self.attrs = dict(model=model, nch=nch, key=key, height_extent_max=rec[6], stalls_max=stalls_max,
                          stalls_org=stalls_org, volt_mult=mult, abs_volt=bool(rec[8] & 1), vol_adj_dis=bool(rec[8] & 2),
                          raw=rec.hex(' '))
        self.model_key = key
        self.nch = nch
        if not self.handshake_ok:
            for i in range(nch): self.stall[i] = stalls_org
        self.handshake_ok = True
        log.info("handshake ok: %s", self.attrs)
        self._set_status('connected')

    def _on_trailer(self, rec):
        x = 0
        for i in range(6, 13): x ^= rec[i]
        if rec[13] != (x & 0x7F):
            log.debug("trailer xor mismatch"); self._reset_rx(); return
        w0 = struct.unpack('<I', rec[6:10])[0]
        w1 = struct.unpack('<I', rec[10:14])[0]
        self.dip_size = max(0, min(1600, (w0 >> 1) & 0x7FF))
        self.roll = bool(w0 & 0x8000)
        self.trig_ecc = min(1600, (w0 >> 17) & 0x7FF)
        self.ch_shift = (w0 >> 29) & 7
        self.ch_two = (w1 >> 1) & 0xFF
        self.ch_ret = (w1 >> 10) & 0xFF
        ln = (rec[0] & 0x7F) + (rec[1] & 0x7F) * 128
        ck = struct.unpack('<H', rec[2:4])[0] & 0x7F7F
        mysum = self._rx_sum & 0x7F7F
        if ln == len(self._rx_buf) and ck == mysum:
            self._deliver(bytes(self._rx_buf))
        else:
            log.debug("frame len/check mismatch len=%d/%d ck=%04x/%04x", ln, len(self._rx_buf), ck, mysum)
        self._reset_rx()

    def _reset_rx(self):
        self._rx_buf.clear(); self._rx_sum = 0

    def _deliver(self, raw):
        en = self._enabled_list()
        n = len(en)
        if n < 1: return
        try: start = en.index(self.ch_shift)
        except ValueError: start = n - 1
        chans = {c: bytearray() for c in en}
        i = start
        for b in raw:
            i = (i + 1) % n
            chans[en[i]].append(b)
        self.frames += 1
        self.last_frame_t = time.time()
        f = dict(t=self.last_frame_t, seq=self.frames, dip=self.dip_size, roll=self.roll, trig_ecc=self.trig_ecc,
                 ch_en=self.ch_ret, timebase=min(self.timebase, TIME_HARD_MAX), nraw=len(raw),
                 chans={c: bytes(v) for c, v in chans.items()})
        self.on_frame(f)
