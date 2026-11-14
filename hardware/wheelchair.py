"""The one interface to the wheelchair: Wheelchair().set(x, y).

x, y are percent of stick (-100..100; x>0 right, y>0 forward). Used by teleop.py now and
by the AI driver later. A 20 Hz sender thread writes the latest command into the UNO Q's
RAM command box over SWD (openocd on the board, its TCL port tunnelled to this machine).

Link to the board, picked automatically:
  - USB:       if adb sees the UNO Q (Arduino's adb, App Lab install).
  - Tailscale: otherwise (or WHEELCHAIR_LINK=tailscale), SSH to WHEELCHAIR_HOST (default "wheelchair-unoq").

Mode: chair.fsd = False -> the real stick drives (stock behaviour); True -> set(x, y) drives.
Takeover: in FSD, pushing the real stick hands control back (stats()['takeover'] == True).
Two deadman layers (FSD falls back to the real stick):
  - here: if set() has not been called for 0.3 s, the sender writes zeros;
  - on the UNO Q: if writes stop for ~0.25 s, the real stick takes over again.
"""
import os, socket, subprocess, threading, time

ADB = os.path.expanduser("~/Library/Arduino15/packages/arduino/tools/adb/32.0.0/adb")
HOST = os.environ.get("WHEELCHAIR_HOST", "wheelchair-unoq")
START_OCD = "pgrep -x openocd >/dev/null || (setsid -f /opt/openocd/bin/arduino-debug.sh > /tmp/ocd.log 2>&1 < /dev/null; sleep 3)"
MAGIC = "0xc0ffee01"
FRESH_S, RATE_HZ, PORT = 0.3, 20, 6666


def _run(*cmd): return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout


class Wheelchair:
    def __init__(self, deadband=41, turn_deadband=37):   # measured: forward creeps at 41%, turning at 37%
        self.deadband = deadband            # % of stick the chair ignores (forward/back); mapped past it
        self.turn_deadband = turn_deadband  # same for left/right
        self.fsd = False                    # False: the real stick drives. True: set(x, y) drives.
        self._x = self._y = 0; self._at = 0.0; self._seq = 0
        self._lock = threading.Lock(); self._stop = False; self._tunnel = None
        self.link = self._open_link()
        self._sock = socket.create_connection(("127.0.0.1", self._port)); self._sock.settimeout(5)
        self.box = self._find_box()
        threading.Thread(target=self._sender, daemon=True).start()

    def set(self, x, y):
        """Latest wish from the driver (GUI or AI). Must be called at least every 0.3 s."""
        self._x, self._y, self._at = int(x), int(y), time.time()

    def stop(self): self.set(0, 0)

    def stats(self):
        """Live dict straight from the UNO Q's command box."""
        v = [int(w, 16) for w in self._ocd(f"read_memory {self.box + 28:#x} 32 10").split()]
        return dict(reads=v[0], resets=v[2], sent=(v[3], v[4]), stick=(v[5], v[6]),
                    stick_good=v[7], stick_bad=v[8], takeover=bool(v[9]))

    def close(self):
        self._stop = True; time.sleep(0.1); self.fsd = False      # hand control back to the real stick
        for _ in range(3): self._write(0, 0); time.sleep(0.05)
        self._sock.close()
        if self._tunnel: self._tunnel.terminate()

    # -- internals ---------------------------------------------------------------
    def _open_link(self):
        if os.environ.get("WHEELCHAIR_LINK") != "tailscale" and os.path.exists(ADB) and "\tdevice" in _run(ADB, "devices"):
            _run(ADB, "shell", START_OCD); _run(ADB, "forward", f"tcp:{PORT}", f"tcp:{PORT}")
            self._port = PORT; return "usb"
        with socket.socket() as s: s.bind(("127.0.0.1", 0)); self._port = s.getsockname()[1]   # free local port
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]
        _run(*ssh, f"arduino@{HOST}", START_OCD)
        self._tunnel = subprocess.Popen([*ssh, "-N", "-L", f"{self._port}:127.0.0.1:{PORT}", f"arduino@{HOST}"])
        for _ in range(50):                                   # wait for the tunnel to accept
            try: socket.create_connection(("127.0.0.1", self._port), timeout=0.2).close(); return f"tailscale:{HOST}"
            except OSError: time.sleep(0.1)
        raise ConnectionError(f"no USB board and no SSH to {HOST} (Tailscale up? board on?)")

    def _ocd(self, c):
        with self._lock:
            self._sock.sendall(c.encode() + b"\x1a"); buf = b""
            while not buf.endswith(b"\x1a"):
                chunk = self._sock.recv(65536)
                if not chunk: raise ConnectionError("openocd connection lost")
                buf += chunk
            return buf[:-1].decode()

    def _find_box(self):
        base = 0x20010000
        words = self._ocd(f"read_memory {base:#x} 32 16384").split()
        hits = [base + 4 * i for i, w in enumerate(words) if w == MAGIC]

        def live(a):  # the real box's chair-read counter is advancing
            r = self._ocd(f"read_memory {a + 28:#x} 32 1"); time.sleep(0.05)
            return self._ocd(f"read_memory {a + 28:#x} 32 1") != r
        hits = [h for h in hits if live(h)]
        if len(hits) != 1:
            raise RuntimeError(f"need one live command box, found {len(hits)} (chair on? sketch running?)")
        return hits[0]

    def _past_deadband(self, p, db=None):
        db = self.deadband if db is None else db
        p = max(-100, min(100, p))
        if p == 0: return 0
        return (1 if p > 0 else -1) * round(db + (100 - db) * abs(p) / 100)

    def _write(self, x, y):
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        sx, sy = self._past_deadband(x, self.turn_deadband), self._past_deadband(y)
        mode = 1 if self.fsd else 0
        self._ocd(f"write_memory {self.box + 4:#x} 32 {{{sx & 0xFFFFFFFF} {sy & 0xFFFFFFFF} {mode} 0 0 {self._seq}}}")

    def _sender(self):
        while not self._stop:
            fresh = time.time() - self._at < FRESH_S
            try: self._write(self._x if fresh else 0, self._y if fresh else 0)
            except (OSError, ConnectionError): pass          # link hiccup: the board's watchdog covers it
            time.sleep(1 / RATE_HZ)


if __name__ == "__main__":  # self-check of the deadband mapping (no hardware needed)
    c = Wheelchair.__new__(Wheelchair); c.deadband = 30
    assert [c._past_deadband(p) for p in (0, 10, 100, -1, 150)] == [0, 37, 100, -31, 100]
    print("ok")
