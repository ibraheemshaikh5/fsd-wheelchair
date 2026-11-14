# Chair link: Linux side of the UNO Q app.
#
# Connects to the driving service (/ws/chair), forwards drive commands to the
# microcontroller over the Bridge and reports chair status back. All logic is in
# chair_link/; this file only wires it to the App Lab runtime.

import logging
from pathlib import Path

from arduino.app_utils import App, Bridge

from chair_link.runner import build_link, config_path, run_link

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

link = build_link(config_path(Path(__file__)), Bridge.call)

# App.run calls the user loop again if it ever returns; run_link only returns
# if the event loop dies, and the chair is stopped before that happens.
App.run(user_loop=lambda: run_link(link))
