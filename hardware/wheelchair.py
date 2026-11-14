"""The one interface to the wheelchair: Wheelchair().set(x, y).

x, y are percent of stick (-100..100; x>0 right, y>0 forward). Used by teleop.py now and by
the AI driver later. A 20 Hz sender thread fires UDP packets over Tailscale to the daemon on
the UNO Q (board/wheelchaird.py), which feeds the fake joystick locally at 50 Hz. Nothing here
waits on the network, so Wi-Fi jitter doesn't stall the caller.

Mode: chair.fsd = False -> the real stick drives (stock behaviour); True -> set(x, y) drives.
Dropout: after > 2 s without packets FSD latches off (stats()['dropout_latched']); set fsd again to re-arm.
Takeover: in FSD, pushing the real stick hands control back (stats()['takeover'] == True).
Deadman layers:
  - here: if set() has not been called for 0.3 s, we send zeros;
  - board daemon: holds the last command through Wi-Fi gaps up to 0.3 s, eases to zero by 0.6 s,
    and after 2 s hands control back to the real stick;
  - MCU: if the daemon stops feeding it for ~0.25 s, the real stick takes over.
Board address: WHEELCHAIR_HOST (default "wheelchair-unoq", Tailscale MagicDNS).
"""
import os, socket, threading, time
now = time.monotonic

HOST = os.environ.get("WHEELCHAIR_HOST", "wheelchair-unoq")
PORT, RATE_HZ, FRESH_S = 5005, 20, 0.3


class Wheelchair:
    def __init__(self, deadband=41, turn_deadband=37):   # measured: forward creeps at 41%, turning at 37%
        self.deadband = deadband            # % of stick the chair ignores (forward/back); mapped past it
        self.turn_deadband = turn_deadband  # same for left/right
        self._fsd = False; self._arm = 0    # arm bumps on every off->on, so the board can re-arm after a dropout
        self._session = int.from_bytes(os.urandom(3), "big"); self._seq = 0
        self._x = self._y = 0; self._at = 0.0; self._stop = False; self._send_lock = threading.Lock()
        self._stats = None; self._stats_at = 0.0
        self._addr = (socket.gethostbyname(HOST), PORT)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); self._sock.setblocking(False)
        self.link = f"tailscale:{HOST}"
        threading.Thread(target=self._sender, daemon=True).start()
        for _ in range(60):                 # wait up to 3 s for the board to answer
            if self._stats: return
            time.sleep(0.05)
        raise ConnectionError(f"no answer from {HOST}:{PORT} (board on? Tailscale up? wheelchaird running?)")

    @property
    def fsd(self): return self._fsd

    @fsd.setter
    def fsd(self, on):
        """False: the real stick drives. True: set(x, y) drives (re-arms after a Wi-Fi dropout)."""
        if on and not self._fsd: self._arm += 1
        self._fsd = bool(on)

    def set(self, x, y):
        """Latest wish from the driver (GUI or AI). Must be called at least every 0.3 s."""
        changed = (int(x), int(y)) != (self._x, self._y)
        self._x, self._y, self._at = int(x), int(y), now()
        if changed:                          # don't wait for the next tick: send key presses/releases at once
            try: self._send(self._x, self._y)
            except OSError: pass

    def stop(self): self.set(0, 0)

    def stats(self):
        """Latest board stats (updated ~20x/s); 'age' is seconds since the board last answered."""
        return dict(self._stats, age=round(now() - self._stats_at, 2))

    def close(self):
        self._stop = True; time.sleep(0.1); self.fsd = False      # hand control back to the real stick
        for _ in range(5): self._send(0, 0); time.sleep(0.05)
        self._sock.close()

    # -- internals ---------------------------------------------------------------
    def _past_deadband(self, p, db=None):
        db = self.deadband if db is None else db
        p = max(-100, min(100, p))
        if p == 0: return 0
        return (1 if p > 0 else -1) * round(db + (100 - db) * abs(p) / 100)

    def _send(self, x, y):
        sx, sy = self._past_deadband(x, self.turn_deadband), self._past_deadband(y)
        with self._send_lock: self._seq += 1; seq = self._seq
        msg = f"{sx} {sy} {1 if self._fsd else 0} {self._arm} {self._session} {seq}"
        self._sock.sendto(msg.encode(), self._addr)

    def _sender(self):
        while not self._stop:
            fresh = now() - self._at < FRESH_S
            try: self._send(self._x if fresh else 0, self._y if fresh else 0)
            except OSError: pass
            time.sleep(1 / RATE_HZ)
            while True:                     # drain every reply that arrived; keep the newest
                try: v = [int(w, 16) for w in self._sock.recv(256).split()]
                except (BlockingIOError, OSError): break
                except ValueError: continue
                if len(v) < 10: continue
                self._stats = dict(reads=v[0], resets=v[2], sent=(v[3], v[4]), stick=(v[5], v[6]),
                                   stick_good=v[7], stick_bad=v[8], takeover=bool(v[9]),
                                   dropout_latched=bool(v[10]) if len(v) > 10 else False)
                self._stats_at = now()


if __name__ == "__main__":  # self-check of the deadband mapping (no hardware needed)
    c = Wheelchair.__new__(Wheelchair); c.deadband = 30; c._fsd = False; c._arm = 0
    c.fsd = True; c.fsd = True; c.fsd = False; c.fsd = True; assert c._arm == 2   # only off->on re-arms
    assert [c._past_deadband(p) for p in (0, 10, 100, -1, 150)] == [0, 37, 100, -31, 100]
    print("ok")
