"""Runs ON the UNO Q (Linux side). Keeps the fake joystick fed from local openocd, so Wi-Fi
jitter never reaches the chair.

Laptop (teleop or the AI) -> UDP packet (stick %, already past the deadband) -> this daemon -> 50 Hz writes
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
Each received packet is answered with the latest stats line (read from the MCU 10x/s, not per packet).
Live metrics (estimated speed, turn rate, odometer, link and board health) go to METRICS_PATH as JSON
10x/s; drive_service serves them to the iPad. Speed is a ballpark from stick %, not measured.
"""
import json, math, os, socket, subprocess, threading, time
now = time.monotonic                              # never jumps (wall-clock steps can't freeze a command)

PORT, RATE_HZ = 5005, 50
HOLD_S, RAMP_S, HANDBACK_S = 0.3, 0.3, 2.0
SLEW_UP = 5                                       # max stick % increase per 20 ms tick (smooth starts); decreases are instant
START = 35                                        # starts jump straight here: just under the chair's ~37-41% dead zone
MAGIC = "0xc0ffee01"
CX, CY = 16461, 15361                             # stick centre counts (firmware sketch.ino)
TOP_MPH, TOP_TURN_DPS = 4.0, 90.0                 # ballpark: Culver folding chairs list 3.7-4 mph; turn rate a guess
DEAD_FWD, DEAD_TURN = 0.41, 0.37                  # stick fraction the chair ignores (measured; wheelchair.py maps commands past the same values)
LAG_S = 0.5                                       # rough time the chair takes to reach a new speed (guess)
METRICS_PATH = "/dev/shm/wheelchair.json"
MPH_TO_MPS = 0.44704


def slew(out, target):
    """Next output: slowing down / stopping is instant, speeding up is limited to SLEW_UP per tick,
    a reversal stops first (then ramps the other way)."""
    if out * target < 0: return 0
    if abs(target) <= abs(out): return target
    if out == 0: out = START if target > 0 else -START      # skip the part of the ramp the chair ignores
    if abs(target) <= abs(out): return target
    return out + max(-SLEW_UP, min(SLEW_UP, target - out))


def stick_pct(cnt, centre, pos, neg):
    """Stick counts sent to the chair -> % (+ = forward / right), per the firmware's mapping."""
    d = centre - cnt
    return d / (pos if d > 0 else neg)


def past_dead(pct, dead, top):
    """Ballpark chair response: nothing inside the dead zone, then linear up to top at 100%."""
    f = min(1.0, abs(pct) / 100)
    return math.copysign(top * max(0.0, (f - dead) / (1 - dead)), pct)


HEALTH = {"board_temp_c": None, "wifi_signal_pct": None}


def watch_health():
    """Background thread: CPU temperature and Wi-Fi signal every 2 s (nmcli is too slow for the 50 Hz loop)."""
    while True:
        try: HEALTH["board_temp_c"] = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        except (OSError, ValueError): HEALTH["board_temp_c"] = None
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list", "--rescan", "no"],
                                 capture_output=True, text=True, timeout=5).stdout
            HEALTH["wifi_signal_pct"] = next((int(l.split(":")[1]) for l in out.splitlines() if l.startswith("*")), None)
        except (OSError, ValueError, subprocess.TimeoutExpired): HEALTH["wifi_signal_pct"] = None
        time.sleep(2)


class Metrics:
    """Turns the MCU stats block into live numbers for the dashboard."""
    def __init__(self):
        self.t = self.reads = self.good = self.bad = None
        self.mph = self.turn = self.odo_m = 0.0; self.packets = 0; self.pkt_rate = 0.0

    def update(self, v, t, mode, latched, gap):
        reads, resets, sx, sy, kx, ky, good, bad, takeover = v[0], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9]
        fwd, right = stick_pct(sy, CY, 96, 113), stick_pct(sx, CX, 103, 104)
        dt = 0.0 if self.t is None else t - self.t
        if dt > 0:
            a = dt / (LAG_S + dt)
            self.mph += a * (past_dead(fwd, DEAD_FWD, TOP_MPH) - self.mph)
            self.turn += a * (past_dead(right, DEAD_TURN, TOP_TURN_DPS) - self.turn)
            self.odo_m += abs(self.mph) * MPH_TO_MPS * dt
            poll_hz, stick_hz = (reads - self.reads) / dt, (good - self.good) / dt
            bad_new = bad - self.bad
            self.pkt_rate += 0.3 * (self.packets / dt - self.pkt_rate); self.packets = 0
        else:
            poll_hz = stick_hz = 0.0; bad_new = 0
        self.t, self.reads, self.good, self.bad = t, reads, good, bad
        fsd = mode == 1 and not latched and gap <= HANDBACK_S and not takeover
        return {
            "t": time.time(),
            "driver": "fsd" if fsd else "joystick",     # who is moving the chair right now
            "speed_mph": round(self.mph, 2) + 0.0, "speed_mps": round(self.mph * MPH_TO_MPS, 2) + 0.0,   # +0.0: no "-0.0"
            "turn_dps": round(self.turn, 1) + 0.0,            # + = turning right
            "odometer_m": round(self.odo_m, 1),         # since the daemon started; estimate
            "stick_pct": {"fwd": round(fwd), "right": round(right)},          # what the chair receives
            "real_stick_pct": {"fwd": round(stick_pct(ky, CY, 96, 113)), "right": round(stick_pct(kx, CX, 103, 104))},
            "chair_poll_hz": round(poll_hz),            # ~200 when the chair is on and happy
            "stick_read_hz": round(stick_hz), "stick_read_errors": bad_new,
            "chair_resets": resets, "takeover": bool(takeover), "dropout_latched": latched,
            "command_age_ms": round(gap * 1000) if gap < 60 else None,   # None: no sender lately
            "command_rate_hz": round(self.pkt_rate, 1),
            **HEALTH,
        }


def write_metrics(m):
    tmp = METRICS_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(m, f)
    os.replace(tmp, METRICS_PATH)                 # readers never see a half-written file


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
    threading.Thread(target=watch_health, daemon=True).start()
    while True:                                   # reconnect forever (openocd restarts, sketch reloads)
        try:
            s, ocd = ocd_connect(); box = find_box(ocd); socks = listen()
            print(f"wheelchaird: box {box:#x}, listening on {[u.getsockname() for u in socks]}", flush=True)
            x = y = mode = 0; last_rx = 0.0; seq = 0
            session = last_seq = None; latched = False; arm = 0; dead_arm = None; ox = oy = 0; tick = 0
            metrics = Metrics(); st = ocd(f"read_memory {box + 28:#x} 32 10").replace("0x", "")
            while True:
                t0 = now(); tick += 1
                if tick % 50 == 0 and ocd(f"read_memory {box:#x} 32 1") != MAGIC:
                    raise RuntimeError("command box gone (MCU reset/reflashed)")
                for u in socks:
                    while True:
                        try: data, addr = u.recvfrom(64)
                        except BlockingIOError: break
                        try: px, py, pm, parm, sess, sq = (int(v) for v in data.split()[:6])
                        except ValueError: continue
                        # Nothing from the packet is kept until it passes the sender and order checks.
                        if sess != session:
                            if t0 - last_rx < HANDBACK_S: continue                          # another sender is driving: ignore
                            session, last_seq, latched = sess, -1, False                    # new sender takes over
                        if sq <= last_seq: continue                                         # late / duplicate
                        last_seq = sq; x, y, mode, arm, last_rx = px, py, pm, parm, t0
                        if latched and arm != dead_arm: latched = False                     # re-armed
                        metrics.packets += 1
                        u.sendto(f"{st} {int(latched)}".encode(), addr)
                gap = t0 - last_rx
                if tick % 5 == 0:                     # 10x/s: stats for replies + dashboard metrics
                    st = ocd(f"read_memory {box + 28:#x} 32 10").replace("0x", "")
                    try: write_metrics(metrics.update([int(w, 16) for w in st.split()], t0, mode, latched, gap))
                    except (OSError, ValueError, IndexError) as e: print("wheelchaird: metrics:", repr(e), flush=True)
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
        assert round(stick_pct(CY - 96 * 50, CY, 96, 113)) == 50 and round(stick_pct(CX + 104 * 30, CX, 103, 104)) == -30
        assert past_dead(30, DEAD_FWD, TOP_MPH) == 0 and past_dead(100, DEAD_FWD, TOP_MPH) == TOP_MPH and past_dead(-100, DEAD_FWD, TOP_MPH) == -TOP_MPH
        m = Metrics(); v = [0, 0, 0, CX, CY - 9600, CX, CY, 0, 0, 0]
        for i in range(30): r = m.update(v, i * 0.1, 1, False, 0.05); v[0] += 20
        assert r["driver"] == "fsd" and 3.5 < r["speed_mph"] <= TOP_MPH and r["chair_poll_hz"] == 200 and r["odometer_m"] > 0
        assert [slew(0, 70), slew(70, 0), slew(70, -70), slew(0, -70), slew(40, 20), slew(68, 70), slew(0, 30)] == [40, 0, 0, -40, 20, 70, 30]
        print("ok"); sys.exit()
    main()
