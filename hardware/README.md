# Hardware: drive the wheelchair from code

The chair (Culver WOLF) reads its joystick from a **Melexis MLX90393** magnet sensor over I2C.
An **Arduino UNO Q** pretends to be that sensor, so the chair's own controller (motors, brakes,
speed buttons, all safety logic) stays stock. Nothing is cut or soldered.

```
Mac / AI  --set(x, y)-->  wheelchair.py  --SWD (USB or Tailscale)-->  UNO Q  --I2C-->  chair controller
                                                                        ^
                                                  real joystick sensor (Grove port D2)
```

## Wiring (see `img/final_wiring.png`)
- Chair's stick cable, sensor-end 4-hole plug -> shield: **red -> SDA, white -> SCL, black -> GND, yellow empty.**
- Real joystick sensor -> Grove cable -> shield port **D2** (not an I2C port).
- Shield switch on **3V3**. UNO Q powered by USB-C (Mac or a 5V/3A power bank).
- Never connect the real sensor to SDA/SCL: two 0x0C chips on the chair's bus = garbage + beeps.
- Back to stock in 10 s: unplug the jumpers, plug the stick cable back into the sensor.

## Cold start
1. Power the UNO Q. It auto-starts the App Lab app `mlx-soft` (`firmware/sketch.ino`) in ~1 min.
2. Turn the chair on. It must be quiet. If the UNO Q lost power while the chair was on, power-cycle the chair.
3. `python3 hardware/teleop.py`: MODE button (JOYSTICK / FSD), hold W/A/S/D in FSD.
   Link: USB if plugged in, else Tailscale to `wheelchair-unoq` (override with `WHEELCHAIR_HOST`).

## From code (the AI)
```python
from wheelchair import Wheelchair
chair = Wheelchair()
chair.fsd = True
chair.set(x, y)          # percent, x>0 right, y>0 forward; call at least every 0.3 s
chair.stats()            # reads, resets, sent, stick, takeover
```
- Deadbands are handled for you (measured: forward 41%, turn 37%), so 1% = just creeping.
- **Safety:** stop calling `set()` -> stops in 0.3 s. Link dies -> the UNO Q hands control back to
  the real stick in ~0.25 s. Pushing the real stick in FSD takes over (`stats()["takeover"]`).
  The chair's power button is the e-stop.

## Uploading firmware
Turn the chair **off** first. Stop openocd on the board (`pkill -f bin/openocd`), then App Lab or
`arduino-app-cli app start ~/ArduinoApps/mlx-soft` on the board with `sketch.ino` copied into the app.

## Files
- `firmware/sketch.ino`: software I2C target faking the MLX90393 + software I2C master reading the real stick.
- `wheelchair.py`: the control interface. `teleop.py`: hold-to-drive window.
- `tools/decode_i2c.py`, `tools/decode2.py`: decode raw I2C captures.
