# Open questions

In rough order of how much they block.

1. ~~**How do we command the chair?**~~ **Decided:** an Arduino UNO Q fakes the joystick's MLX90393 magnet sensor over I2C; code calls `Wheelchair().set(x, y)` over Tailscale. See [hardware/README.md](../hardware/README.md). Still open: the stick percent that gives full speed and full turn rate, measured on the ground (placeholders in `ml/fsd_ml/chair_out.py`).
2. **Which base model?** Decided: OmniVLA for everything, on a Modal GPU, with FlowPilot as an outdoor fallback. See [model.md](model.md). Confirm by running the candidates on recorded chair footage. The phone stream can now record that footage (see [running](running.md)).
3. **Is one camera enough?** A phone camera gives no depth and a narrow view. May need a wide lens or a second camera for curbs and close obstacles. The capture page picks the iPhone's ultra-wide lens if Safari offers it; the device check reports whether it does.
4. **How do we navigate indoors?** GPS does not work inside, so there are no waypoints to follow. Options: drive a route once and replay it from camera images, give a language instruction ("go to the kitchen"), or build a map of the space. Also: how the chair hands off between GPS and indoor mode at a doorway.
5. **Network latency.** With compute off the chair, every command crosses Wi-Fi or cellular. Need to measure the delay and set the top speed to match. The viewer now shows capture-to-service latency (p50/p95) and round trip. First indoor run on the iPhone over Tailscale (2026-11-14): capture to service 38–47 ms median, 46–98 ms 95th percentile, 8–11 ms round trip. Numbers from a sidewalk run on cellular still go here.
6. ~~**How does the phone stream?**~~ **Decided:** a Safari page on the iPhone, over Tailscale. See [phone protocol](phone-protocol.md). Revisit with a native app if odometry (7) or Safari's limits (screen must stay on, page in front) get in the way.
7. **Where do trajectory labels come from?** Fine-tuning needs the chair's actual short-range path per frame. Phone GPS (±3–5 m, 1 Hz, nothing indoors) is too coarse and a browser has no motion tracking. Options: a native ARKit app sending `pose` over the same protocol, or visual odometry run offline on recordings (IMU is recorded to help). The same source would give the driver model the live speed it expects as input; see [model.md](model.md).
8. **How does the service know the data comes from the chair's phone?** Today the tailnet is the only boundary: any device on it can open `/ws/phone`, and the newest connection wins, so it can take over the stream and send its own frames and GPS. That is fine for recording, but a pairing token (or other authentication) for `/ws/phone` is required before any driving loop trusts the data. Needs: how the phone gets the token, and what the chair does when the stream is taken over mid-drive.

## Device check results

Run **Device check** on the mounted iPhone and record what it found. First run 2026-11-14: iPhone (non-Pro) held by hand indoors, streaming the capture page over Tailscale.

| | result |
|---|---|
| Ultra-wide lens selectable in Safari | **Yes**: Safari lists "Back Ultra Wide Camera" and the capture page picks it |
| Resolution / frame rate achieved | Camera 720×1280; sent as 360×640 JPEG at a steady 10 fps (1 frame missed in ~800) |
| Frame capture time exposed | not yet checked |
| Compass matches real direction when mounted upright | not yet checked |
| GPS fix rate and accuracy | At rest indoors: a fix about every 5 s, 11–19 m. Moving: not yet measured. Needs Safari Websites location set to While Using |
| Screen wake lock held | not yet checked |
