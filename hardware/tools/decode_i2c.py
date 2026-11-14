"""Decode raw I2C edges dumped by raw.ino. Input lines: '<delta_cycles> <state>' (bit0 SDA, bit1 SCL), 160 MHz cycles.
Prints one line per transaction: start time (us), gap since previous start, bytes with ack/nack."""
import sys

def decode(lines):
    t = 0; prev = None; txs = []; cur = None; bits = []
    for ln in lines:
        p = ln.split()
        if len(p) != 2 or not p[0].isdigit(): continue
        t += int(p[0]); s = int(p[1]); sda, scl = s & 1, (s >> 1) & 1
        if prev is None: prev = (sda, scl); continue
        psda, pscl = prev
        if scl and pscl and psda and not sda:            # START / repeated START
            if cur: txs.append(cur)
            cur = {"t": t / 160, "bytes": [], "stop": False}; bits = []
        elif scl and pscl and not psda and sda:          # STOP
            if cur: cur["stop"] = True; txs.append(cur); cur = None
        elif scl and not pscl and cur is not None:      # SCL rising: sample SDA
            bits.append(sda)
            if len(bits) == 9:
                v = 0
                for b in bits[:8]: v = (v << 1) | b
                cur["bytes"].append((v, "a" if bits[8] == 0 else "n")); bits = []
        prev = (sda, scl)
    if cur: txs.append(cur)
    return txs

if __name__ == "__main__":
    txs = decode(open(sys.argv[1]).read().splitlines())
    last = 0
    for tx in txs:
        b = " ".join(f"{v:02X}{a}" for v, a in tx["bytes"])
        print(f"{tx['t']:10.1f}us +{tx['t']-last:8.1f}  {b}{' P' if tx['stop'] else ''}")
        last = tx["t"]
