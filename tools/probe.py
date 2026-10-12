import asyncio, logging, sys, time
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import fosc
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')

async def main():
    devs = await fosc.discover(timeout=1.5)
    print("DISCOVERED", devs)
    if not devs: return
    d = devs[0]
    stats = {}
    def on_frame(f):
        k = (f['dip'], f['roll'], f['ch_en'], f['nraw'])
        stats[k] = stats.get(k, 0) + 1
        if f['seq'] <= 3 or f['seq'] % 20 == 0:
            desc = {c: (len(v), list(v[:8]), min(v) if v else None, max(v) if v else None) for c, v in f['chans'].items()}
            print(f"FRAME {f['seq']} dip={f['dip']} roll={f['roll']} ecc={f['trig_ecc']} ch_en={f['ch_en']:02x} shift={dev.ch_shift} nraw={f['nraw']} {desc}")
    dev = fosc.Fosc53B(d['ip'], d['data_port'], d['ctr_port'], on_frame=on_frame)
    dev.timebase = int(sys.argv[1]) if len(sys.argv) > 1 else 26
    dev.ch_en = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x1F
    await dev.connect()
    t0 = time.time()
    while time.time() - t0 < float(sys.argv[3]) if len(sys.argv) > 3 else 12:
        await asyncio.sleep(1)
        print(f"-- t={time.time()-t0:.0f}s status={dev.status} frames={dev.frames} attrs={dev.attrs} dip={dev.dip_size} roll={dev.roll} ch_ret={dev.ch_ret:02x}")
    print("STATS", stats)
    await dev.close()
asyncio.run(main())
