"""Runs ON the UNO Q (Linux side). Keeps the fake joystick fed from local openocd, so Wi-Fi
jitter never reaches the chair.

Mac/AI -> UDP "x y mode" (stick %, already past the deadband) -> this daemon -> 50 Hz writes
into the MCU's RAM command box (SWD via local openocd) -> fake MLX90393 -> chair.

Gaps in UDP (bad Wi-Fi):  <= HOLD_S keep the last command (no stutter),
                           then ease to zero over RAMP_S,
                           after HANDBACK_S fall back to JOYSTICK mode (real stick drives).
If this daemon dies, the MCU's own ~0.25 s watchdog hands control to the real stick.
Listens only on the Tailscale address (and localhost), so only tailnet devices can drive.
Each received packet is answered with a stats line.
"""
import socket, subprocess, time

PORT, RATE_HZ = 5005, 50
HOLD_S, RAMP_S, HANDBACK_S = 0.6, 0.3, 2.0
MAGIC = "0xc0ffee01"


def ocd_connect():
    if subprocess.run(["pgrep", "-x", "openocd"], capture_output=True).returncode:
        subprocess.Popen(["setsid", "-f", "/opt/openocd/bin/arduino-debug.sh"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        time.sleep(3)
    s = socket.create_connection(("127.0.0.1", 6666), timeout=5)

    def ocd(c):
        s.sendall(c.encode() + b"\x1a"); buf = b""
        while not buf.endswith(b"\x1a"):
            chunk = s.recv(65536)
            if not chunk: raise ConnectionError("openocd closed")
            buf += chunk
        return buf[:-1].decode()
    return s, ocd


def find_box(ocd):
    base = 0x20010000
    words = ocd(f"read_memory {base:#x} 32 16384").split()
    hits = [base + 4 * i for i, w in enumerate(words) if w == MAGIC]
    if len(hits) == 1: return hits[0]
    live = [h for h in hits if ocd(f"read_memory {h + 28:#x} 32 1") != (time.sleep(0.05) or ocd(f"read_memory {h + 28:#x} 32 1"))]
    if len(live) != 1: raise RuntimeError(f"command box: {len(hits)} found, {len(live)} live")
    return live[0]


def listen():
    ts_ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.split()
    socks = []
    for ip in ["127.0.0.1"] + ts_ip[:1]:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind((ip, PORT)); u.setblocking(False)
        socks.append(u)
    return socks


def main():
    while True:                                   # reconnect forever (openocd restarts, sketch reloads)
        try:
            s, ocd = ocd_connect(); box = find_box(ocd); socks = listen()
            print(f"wheelchaird: box {box:#x}, listening on {[u.getsockname() for u in socks]}", flush=True)
            x = y = mode = 0; last_rx = 0.0; seq = 0
            while True:
                t0 = time.time()
                for u in socks:
                    while True:
                        try: data, addr = u.recvfrom(64)
                        except BlockingIOError: break
                        try: x, y, mode = (int(v) for v in data.split()[:3]); last_rx = t0
                        except ValueError: continue
                        st = ocd(f"read_memory {box + 28:#x} 32 10").replace("0x", "")
                        u.sendto(st.encode(), addr)
                gap = t0 - last_rx
                k = 1.0 if gap <= HOLD_S else max(0.0, 1 - (gap - HOLD_S) / RAMP_S)
                m = mode if gap <= HANDBACK_S else 0
                seq = (seq + 1) & 0xFFFFFFFF
                ocd(f"write_memory {box + 4:#x} 32 {{{int(x * k) & 0xFFFFFFFF} {int(y * k) & 0xFFFFFFFF} {m} 0 0 {seq}}}")
                time.sleep(max(0, 1 / RATE_HZ - (time.time() - t0)))
        except Exception as e:                    # MCU watchdog covers us while we restart
            print("wheelchaird: restarting after", repr(e), flush=True)
            for u in locals().get("socks", []): u.close()
            time.sleep(1)


if __name__ == "__main__":
    main()
