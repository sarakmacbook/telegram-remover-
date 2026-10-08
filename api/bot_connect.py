"""POST /api/bot_connect — verify a bot and send a test alert."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)


@app.route("/", methods=["POST"])
@app.route("/api/bot_connect", methods=["POST"])
def bot_connect():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_bot_connect(request.get_json(silent=True) or {})


if __name__ == "__main__":
    app.run()
