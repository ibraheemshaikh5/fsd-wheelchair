"""Robust decode: a transaction = from a START to a STOP that is followed by >30us of bus idle.
Bits are sampled on SCL rising edges; SDA changes while SCL is high inside a transaction are reported as glitches."""
import sys
rows=[]; t=0
for ln in open(sys.argv[1]):
    p=ln.split()
    if len(p)!=2 or not p[0].isdigit(): continue
    t+=int(p[0]); s=int(p[1]); rows.append((t/160, s&1, (s>>1)&1))
txs=[]; i=0; prev=rows[0]
cur=None; bits=[]; glitches=0
for k in range(1,len(rows)):
    tt,sda,scl=rows[k]; pt,psda,pscl=prev
    nxt = rows[k+1][0] if k+1 < len(rows) else tt+1000
    if scl and pscl and psda and not sda and (cur is None):
        cur={"t":tt,"bytes":[],"gl":0}; bits=[]
    elif scl and pscl and not psda and sda and cur is not None:
        if nxt-tt > 30: cur["stop"]=True; txs.append(cur); cur=None
        else: cur["gl"]+=1
    elif scl and pscl and psda and not sda and cur is not None:
        cur["gl"]+=1
    elif scl and not pscl and cur is not None:
        bits.append(sda)
        if len(bits)==9:
            v=0
            for b in bits[:8]: v=(v<<1)|b
            cur["bytes"].append((v,"a" if bits[8]==0 else "n")); bits=[]
    prev=rows[k]
last=0
for tx in txs:
    print(f"{tx['t']:10.1f}us +{tx['t']-last:8.1f}  "+" ".join(f"{v:02X}{a}" for v,a in tx["bytes"])+(f"   [mid-transfer SDA-while-SCL-high glitches: {tx['gl']}]" if tx['gl'] else ""))
    last=tx["t"]
