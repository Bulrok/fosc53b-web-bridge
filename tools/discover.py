import socket, struct, time, sys

def make_table():
    t=[]
    for i in range(256):
        c=i
        for _ in range(8):
            c = (c>>1) ^ 0xEDB88320 if c&1 else c>>1
        t.append(c)
    return t
T=make_table()
def crc(seed, data):
    c=seed
    for b in data:
        c = T[(c ^ b) & 0xFF] ^ (c >> 8)
    return c & 0xFFFFFFFF

HEAD=0x20180205
def frame(body_len, payload):
    """payload = bytes from offset 8 to len-4"""
    buf = bytearray(body_len)
    buf[0:4]=struct.pack('<I',HEAD); buf[4:8]=struct.pack('<I',body_len)
    buf[8:8+len(payload)]=payload
    buf[body_len-4:]=struct.pack('<I',crc(0x5A5A5A5A, bytes(buf[:body_len-4])))
    return bytes(buf)

# usage: discover.py [LOCAL_IP] [BROADCAST]  (defaults: auto-detected LAN IP, 255.255.255.255)
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 53)); return s.getsockname()[0]
    except OSError:
        return '0.0.0.0'
    finally:
        s.close()
local_ip = sys.argv[1] if len(sys.argv)>1 else lan_ip()
bcast = sys.argv[2] if len(sys.argv)>2 else '255.255.255.255'
PORT=8991
payload = socket.inet_aton(local_ip) + b'\0\0' + struct.pack('<H',PORT) + b'FindDev\0'
req = frame(28, payload)
print("REQ", req.hex(' '))

rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
rx.bind(('', PORT))
rx.settimeout(3)
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
tx.sendto(req, (bcast, PORT))
t0=time.time()
while time.time()-t0<3:
    try:
        data, addr = rx.recvfrom(1024)
    except socket.timeout:
        break
    print("FROM", addr, len(data), data.hex(' '))
    if len(data)!=112: continue
    ok = struct.unpack('<I',data[:4])[0]==HEAD and struct.unpack('<I',data[4:8])[0]==len(data) and struct.unpack('<I',data[-4:])[0]==crc(0x5A5A5A5A,data[:-4])
    print(" crc/head ok:", ok)
    print(" src_ip", socket.inet_ntoa(data[8:12]), "src_port", struct.unpack('<H',data[14:16])[0])
    print(" mac", data[16:22].hex(':'))
    print(" server_ip", socket.inet_ntoa(data[22:26]), "data_port", struct.unpack('<H',data[28:30])[0], "ctr_port", struct.unpack('<H',data[30:32])[0])
    print(" flags", data[32], "connected", bool(data[32]&1), "licensed", bool(data[32]&2))
    print(" mod", data[52:64], "ver_soft", struct.unpack('<H',data[64:66])[0], "ver_hard", struct.unpack('<H',data[66:68])[0], "idle", struct.unpack('<H',data[68:70])[0])
    print(" name", data[72:92])
    print(" unknown[26:28]", data[26:28].hex(), "[33:52]", data[33:52].hex(' '), "[70:72]", data[70:72].hex(), "[92:108]", data[92:108].hex(' '))
