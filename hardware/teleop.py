"""Drive window. Mode button: JOYSTICK (real stick drives) or FSD (hold W/A/S/D / arrows, let go to stop).

Speed and deadband sliders. Calls Wheelchair.set(x, y) 20x per second, exactly like the AI will.
Run:  python3 hardware/teleop.py   (Mac and board on the team Tailscale)
"""
import time, tkinter as tk
from wheelchair import Wheelchair

KEYS = {"w": (0, 1), "Up": (0, 1), "s": (0, -1), "Down": (0, -1),
        "a": (-1, 0), "Left": (-1, 0), "d": (1, 0), "Right": (1, 0)}

print("connecting to the UNO Q (takes ~5-10 s)...", flush=True)
chair = Wheelchair()
print("connected, opening window", flush=True)
root = tk.Tk(); root.title(f"Wheelchair teleop ({chair.link})")
root.lift(); root.attributes("-topmost", True); root.after(500, lambda: root.attributes("-topmost", False))
held = {}                     # key -> True while held
speed = tk.IntVar(value=70); turn = tk.IntVar(value=50); deadband = tk.IntVar(value=chair.deadband); turn_db = tk.IntVar(value=chair.turn_deadband)
status = tk.StringVar(value="hold W/A/S/D. Window must be focused.")

for label, var, hi in (("speed %", speed, 100), ("turn %", turn, 100), ("deadband fwd %", deadband, 60), ("deadband turn %", turn_db, 60)):
    tk.Scale(root, label=label, variable=var, from_=0, to=hi, orient="horizontal", length=320).pack(padx=12)
mode_btn = tk.Button(root, text="MODE: JOYSTICK  (click for FSD)", font=("Helvetica", 16, "bold"), width=30,
                     command=lambda: toggle())
mode_btn.pack(pady=8)
big = tk.Label(root, text="STOPPED", font=("Helvetica", 28, "bold"), width=16); big.pack(pady=8)


def toggle():
    chair.fsd = not chair.fsd; held.clear()
    mode_btn.config(text="MODE: FSD  (click for JOYSTICK)" if chair.fsd else "MODE: JOYSTICK  (click for FSD)")
tk.Label(root, textvariable=status, font=("Menlo", 11)).pack(padx=12, pady=(0, 12))


last_press = {}


def press(e):
    if e.keysym in KEYS:
        new = not held.get(e.keysym); held[e.keysym] = True; last_press[e.keysym] = time.time()
        if new: tick(reschedule=False)                 # send the moment the key goes down


def release(e):
    k = e.keysym
    if k in KEYS:  # autorepeat fires release+press pairs: drop the key only if no press follows
        t = time.time()
        root.after(60, lambda: held.update({k: False}) if last_press.get(k, 0) < t else None)


def tick(reschedule=True):
    x = sum(KEYS[k][0] for k, v in held.items() if v)
    y = sum(KEYS[k][1] for k, v in held.items() if v)
    chair.deadband = deadband.get(); chair.turn_deadband = turn_db.get()
    cx, cy = max(-1, min(1, x)) * turn.get(), max(-1, min(1, y)) * speed.get()
    chair.set(cx, cy)
    if not chair.fsd: big.config(text="real stick drives", fg="#555")
    else: big.config(text="STOPPED" if cx == cy == 0 else f"fwd {cy:+d}  turn {cx:+d}",
                     fg="black" if cx == cy == 0 else "#0a7a2f")
    if reschedule: root.after(50, tick)


def show_stats():
    try:
        st = chair.stats()
        if chair.fsd and (st["takeover"] or st["dropout_latched"]): toggle()   # stick pushed / Wi-Fi dropout: show JOYSTICK
        status.set(f"chair reads {st['reads']} resets {st['resets']} | sent {st['sent']} | "
                   f"stick {st['stick']} ok {st['stick_good']} bad {st['stick_bad']}")
    except Exception as e:
        status.set(f"link error: {e}")
    root.after(500, show_stats)


root.bind("<KeyPress>", press); root.bind("<KeyRelease>", release)
root.bind("<FocusOut>", lambda e: held.clear())          # lose focus = stop
root.protocol("WM_DELETE_WINDOW", lambda: (chair.close(), root.destroy()))
tick(); show_stats(); root.mainloop()
