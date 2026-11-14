# Hardware: drive the wheelchair from code

The chair (Culver WOLF) reads its joystick from a **Melexis MLX90393** magnet sensor over I2C.
An **Arduino UNO Q** pretends to be that sensor, so the chair's own controller (motors, brakes,
speed buttons, all safety logic) stays stock. Nothing is cut or soldered.

```
Mac / AI --set(x,y)--> wheelchair.py --UDP over Tailscale--> wheelchaird (UNO Q Linux) --SWD 50 Hz--> MCU --I2C--> chair
                                                                                            ^
                                                                    real joystick sensor (Grove port D2)
```
Wi-Fi gaps up to 0.3 s are bridged on the board (no stutter); longer gaps ease to a stop by 0.6 s, and after 2 s the
real stick drives again.

## Chair dimensions (Culver WOLF, measured)
| | inches | cm |
|---|---|---|
| Overall length | 40 | 101.6 |
| Overall width | 25 | 63.5 |
| Rear (drive) wheel diameter | 12 | 30.5 |
| Front (caster) wheel diameter | 7 | 17.8 |

## Wiring (see `img/final_wiring.png`)
- Chair's stick cable, sensor-end 4-hole plug -> shield: **red -> SDA, white -> SCL, black -> GND, yellow empty.**
- Real joystick sensor -> Grove cable -> shield port **D2** (not an I2C port).
- Shield switch on **3V3**. UNO Q powered by USB-C (Mac or a 5V/3A power bank).
- Never connect the real sensor to SDA/SCL: two 0x0C chips on the chair's bus = garbage + beeps.
- Back to stock in 10 s: unplug the jumpers, plug the stick cable back into the sensor.

## Cold start
1. Power the UNO Q. The fake sensor starts **within ~1 s** (firmware is flashed in "Immediate" mode, no Linux needed).
2. Turn the chair on (any time after the UNO Q has power). It must be quiet. If the UNO Q lost power while the
   chair was on, power-cycle the chair. Wireless control comes up ~1 min later, once Linux and Tailscale boot.
3. `python3 hardware/teleop.py`: MODE button (JOYSTICK / FSD), hold W/A/S/D in FSD.
   The Mac and the board must both be on the team Tailscale (board: `wheelchair-unoq`, override with `WHEELCHAIR_HOST`).
   The board runs `board/wheelchaird.py` as a systemd service (`wheelchaird`), started at boot.

## From code (the AI)
```python
from wheelchair import Wheelchair
chair = Wheelchair()
chair.fsd = True
chair.set(x, y)          # percent, x>0 right, y>0 forward; call at least every 0.3 s
chair.stats()            # reads, resets, sent, stick, takeover, dropout_latched, age
```
- Deadbands are handled for you (measured: forward 41%, turn 37%), so 1% = just creeping.
- **Safety:** stop calling `set()` -> stops in 0.3 s. Wi-Fi gap > 0.3 s -> eases to a stop by 0.6 s; > 2 s -> real stick
  drives and FSD stays latched off until re-armed (set `fsd` again). Starts ramp smoothly; stops are instant. Board daemon dies -> the MCU hands control to the real stick in ~0.25 s. Pushing the real stick in FSD takes over (`stats()["takeover"]`).
  The chair's power button is the e-stop.

## Uploading firmware
Turn the chair **off** first. On the board (adb shell or ssh), with `sketch.ino` copied to `/tmp/fw/fw.ino`:
```sh
sudo systemctl stop wheelchaird; pkill -x openocd
I=~/.arduino15/internal; L=""; for n in Arduino_RouterBridge Arduino_RPClite MsgPack DebugLog ArxTypeTraits ArxContainer; do L="$L --library $(ls -d $I/${n}_*/$n)"; done
cd /tmp/fw && arduino-cli compile -b arduino:zephyr:unoq:wait_linux_boot=no --build-path /tmp/fwbuild $L .
Z=~/.arduino15/packages/arduino/hardware/zephyr/1.1.0
cd /opt/openocd && ./bin/openocd -s /opt/openocd -f openocd_gpiod.cfg \
  -c "set filename0 $Z/firmwares/zephyr-arduino_uno_q_stm32u585xx.elf; set filename1 /tmp/fwbuild/fw.ino.elf-zsk.bin" \
  -f $Z/variants/arduino_uno_q_stm32u585xx/flash_sketch.cfg
sudo systemctl start wheelchaird
```
Do **not** use App Lab / `arduino-app-cli app start` for this sketch: it builds in "Wait for App" mode (the sensor
then only exists after Linux boots, so the chair beeps) and its boot-time re-flash fights the daemon for the SWD pins.

## Board setup (already done on `wheelchair-unoq`)
Tailscale installed and logged in; Wi-Fi power saving off (`/etc/NetworkManager/conf.d/99-wifi-powersave-off.conf`);
**no** default app (`arduino-app-cli properties set default none`, so Linux never re-flashes the MCU at boot); `board/wheelchaird.py` copied to
`/home/arduino/` and `board/wheelchaird.service` installed in `/etc/systemd/system/` (enabled).

## Files
- `firmware/sketch.ino`: software I2C target faking the MLX90393 + software I2C master reading the real stick.
- `wheelchair.py`: the control interface (Mac). `teleop.py`: hold-to-drive window.
- `board/wheelchaird.py` + `.service`: the feeder daemon on the UNO Q.
- `tools/decode_i2c.py`, `tools/decode2.py`: decode raw I2C captures.
