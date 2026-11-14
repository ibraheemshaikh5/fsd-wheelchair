# Open questions

In rough order of how much they block.

1. **How do we command the chair?** The WOLF's controller and joystick protocol are unknown. Options: emulate the joystick's electrical signal, tap a controller bus if there is one, or physically move the joystick with servos. Needs the chair opened up to answer.
2. **Which base model?** See [model.md](model.md). Decide after running the candidates on recorded chair footage.
3. **Is one camera enough?** A phone camera gives no depth and a narrow view. May need a wide lens or a second camera for curbs and close obstacles.
4. **How do we navigate indoors?** GPS does not work inside, so there are no waypoints to follow. Options: drive a route once and replay it from camera images, give a language instruction ("go to the kitchen"), or build a map of the space. Also: how the chair hands off between GPS and indoor mode at a doorway.
5. **Network latency.** With compute off the chair, every command crosses Wi-Fi or cellular. Need to measure the delay and set the top speed to match.
6. **How does the phone stream?** Native app or a browser page. Determines video latency and access to GPS and compass.
