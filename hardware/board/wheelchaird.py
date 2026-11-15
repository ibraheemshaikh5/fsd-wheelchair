"""Runs ON the UNO Q (Linux side). Keeps the fake joystick fed from local openocd, so Wi-Fi
jitter never reaches the chair.

Mac/AI -> UDP "x y mode" (stick %, already past the deadband) -> this daemon -> 50 Hz writes
into the MCU's RAM command box (SWD via local openocd) -> fake MLX90393 -> chair.

Packet: "x y mode arm session seq". Gaps in UDP (bad Wi-Fi):
  <= HOLD_S keep the last command (no stutter), then ease to zero over RAMP_S (0.6 s total),
  starts jump to START (under the chair's dead zone) then ramp at SLEW_UP %/tick; stops are instant,
  after HANDBACK_S fall back to JOYSTICK mode and LATCH it: FSD stays off until the sender
  re-arms (arm counter changes = user clicked FSD again). Late/out-of-order packets are dropped
  (seq per session), so a delayed old command can never restart motion.
One sender at a time: packets from a second sender (e.g. teleop left open while the AI drives)
  are ignored, unanswered, until the current one has been silent for HANDBACK_S.
If this daemon dies, the MCU's own ~0.25 s watchdog hands control to the real stick.
Listens only on the Tailscale address (and localhost), so only tailnet devices can drive.
Each received packet is answered with a stats line.
"""
import socket, subprocess, time
now = time.monotonic                              # never jumps (wall-clock steps can't freeze a command)

PORT, RATE_HZ = 5005, 50
HOLD_S, RAMP_S, HANDBACK_S = 0.3, 0.3, 2.0
SLEW_UP = 5                                       # max stick % increase per 20 ms tick (smooth starts); decreases are instant
START = 35                                        # starts jump straight here: just under the chair's ~37-41% dead zone
MAGIC = "0xc0ffee01"


def slew(out, target):
    """Next output: slowing down / stopping is instant, speeding up is limited to SLEW_UP per tick,
    a reversal stops first (then ramps the other way)."""
    if out * target < 0: return 0
    if abs(target) <= abs(out): return target
    if out == 0: out = START if target > 0 else -START      # skip the part of the ramp the chair ignores
    if abs(target) <= abs(out): return target
    return out + max(-SLEW_UP, min(SLEW_UP, target - out))


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
    if not ts_ip: raise RuntimeError("no Tailscale IP yet")       # outer loop retries until Tailscale is up
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
            session = last_seq = None; latched = False; dead_arm = None; ox = oy = 0; tick = 0
            while True:
                t0 = now(); tick += 1
                if tick % 50 == 0 and ocd(f"read_memory {box:#x} 32 1") != MAGIC:
                    raise RuntimeError("command box gone (MCU reset/reflashed)")
                for u in socks:
                    while True:
                        try: data, addr = u.recvfrom(64)
                        except BlockingIOError: break
                        try: px, py, pm, arm, sess, sq = (int(v) for v in data.split()[:6])
                        except ValueError: continue
                        if sess != session:
                            if t0 - last_rx < HANDBACK_S: continue                          # another sender is driving: ignore
                            session, last_seq, latched = sess, -1, False                    # new sender takes over
                        if sq <= last_seq: continue                                         # late / duplicate
                        last_seq = sq; x, y, mode, last_rx = px, py, pm, t0
                        if latched and arm != dead_arm: latched = False                     # re-armed
                        st = ocd(f"read_memory {box + 28:#x} 32 10").replace("0x", "")
                        u.sendto(f"{st} {int(latched)}".encode(), addr)
                gap = t0 - last_rx
                if gap > HANDBACK_S and mode == 1 and not latched: latched, dead_arm = True, arm
                k = 1.0 if gap <= HOLD_S else max(0.0, 1 - (gap - HOLD_S) / RAMP_S)
                m = 0 if latched or gap > HANDBACK_S else mode
                tx, ty = int(x * k), int(y * k)
                ox, oy = slew(ox, tx), slew(oy, ty)
                seq = (seq + 1) & 0xFFFFFFFF
                ocd(f"write_memory {box + 4:#x} 32 {{{ox & 0xFFFFFFFF} {oy & 0xFFFFFFFF} {m} 0 0 {seq}}}")
                time.sleep(max(0, 1 / RATE_HZ - (now() - t0)))
        except Exception as e:                    # MCU watchdog covers us while we restart
            print("wheelchaird: restarting after", repr(e), flush=True)
            for u in locals().get("socks", []): u.close()
            time.sleep(1)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["test"]:          # self-check, no hardware
        assert [slew(0, 70), slew(70, 0), slew(70, -70), slew(0, -70), slew(40, 20), slew(68, 70), slew(0, 30)] == [40, 0, 0, -40, 20, 70, 30]
        print("ok"); sys.exit()
    main()
