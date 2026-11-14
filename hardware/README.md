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

## Phase 1 autonomous loop on the board
iPhone --wss--> driving service (board, :8000) --> `ml/fsd_ml/drive_loop.py` (board) --HTTPS--> OmniVLA on Modal
--> path --> stick % --> `wheelchaird` (UDP 127.0.0.1:5005). Everything but the model runs on the UNO Q.

1. **Model (Mac, teammate's Modal account).** In `ml/modal_omnivla.py` set `min_containers=1` (a GPU stays up and
   bills until set back to 0), then `cd ml && uv run --with modal modal deploy modal_omnivla.py`. Note the URL.
   OmniVLA takes one frame per call and the point goal; about 255 ms round trip. Any `remote:<url>` driver works.
2. **Code to the board (Mac).** `cd web && npm run build`, then
   `rsync -a --exclude .venv --exclude node_modules --exclude recordings --exclude .git ./ arduino@wheelchair-unoq:fsd-wheelchair/`
   from the repo root (this includes `web/dist`).
3. **Deps (board, once).** No uv, torch, ONNX or ffmpeg needed:
   ```sh
   cd ~/fsd-wheelchair && python3 -m venv .venv   # sudo apt install python3-venv if this fails
   .venv/bin/pip install fastapi 'uvicorn[standard]' pydantic pydantic-settings pygeomag numpy pillow httpx websockets
   ```
4. **Service (board, once).** `sudo cp hardware/board/drive-service.service hardware/board/drive-loop.service /etc/systemd/system/`,
   `sudo systemctl daemon-reload && sudo systemctl enable --now drive-service`, then `sudo tailscale serve --bg 8000`.
   Do not enable `drive-loop`.
5. **Phone.** Open `https://wheelchair-unoq.<tailnet>.ts.net` in Safari, **Chair camera**, **Start streaming**.
6. **Dry run (board), chair still.** Prints one JSON line per decision; nothing moves:
   ```sh
   cd ~/fsd-wheelchair/ml && ../.venv/bin/python -m fsd_ml.drive_loop --driver remote:<url> --goal 5,0 --dry
   ```
   Check `frame_age_ms` stays under 500 and `decision_hz` is about 3-4.
7. **Live, wheels up first.** Drop `--dry` and add `WHEELCHAIR_HOST=127.0.0.1` in front (and `--log drive.jsonl`).
   Or as a unit: write `/home/arduino/drive-loop.env`:
   ```sh
   DRIVER_URL=https://<omnivla>.modal.run
   LOOP_ARGS=--goal 5,0 --fwd 25 --log /home/arduino/drive.jsonl
   WHEELCHAIR_HOST=127.0.0.1
   ```
   and `sudo systemctl start drive-loop` / `stop drive-loop`. Frames older than 500 ms or a failed model call send
   nothing, so the chair stops within ~0.9 s (0.6 s hold + 0.3 s deadman). Pushing the real stick ends the loop.
   Ctrl-C (or `systemctl stop`) stops the chair and hands back the stick. Afterwards set `min_containers=0` and redeploy.


### Current board deployment (set up and verified)
- Service: `drive-service` systemd unit, venv at `/home/arduino/fsd-wheelchair/.venv` (Python 3.13, deps via pip).
- Tailscale serve on the board:
  - `https://wheelchair-unoq.<tailnet>.ts.net/` -> service: phone camera page `/capture/`, sensor viewer `/viewer/`.
  - `https://wheelchair-unoq.<tailnet>.ts.net:8443/` -> iPad app (`app/dist` copied to `/home/arduino/ipad-app`),
    with `/api` and `/ws` proxied to the service:
    `tailscale serve --bg --https=8443 --set-path=/api http://127.0.0.1:8000/api`,
    same for `/ws`, then `tailscale serve --bg --https=8443 /home/arduino/ipad-app`.
    The map needs `VITE_GOOGLE_MAPS_API_KEY` at build time (`app/.env`).
- Verified: iPhone streaming to the board at ~14 fps, sensors OK; OmniVLA on Modal from the board:
  round trip p50 457 ms / p95 746 ms (inference ~200 ms, network ~255 ms), no token required (public URL).
- Not yet run: `drive_loop --dry`, wheels-up live, on-ground tests.

## Files
- `firmware/sketch.ino`: software I2C target faking the MLX90393 + software I2C master reading the real stick.
- `wheelchair.py`: the control interface (Mac). `teleop.py`: hold-to-drive window.
- `board/wheelchaird.py` + `.service`: the feeder daemon on the UNO Q.
- `board/drive-service.service`, `board/drive-loop.service`: the Phase 1 loop on the UNO Q (above).
- `tools/decode_i2c.py`, `tools/decode2.py`: decode raw I2C captures.
