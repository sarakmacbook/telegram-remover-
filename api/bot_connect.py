"""POST /api/bot_connect — verify a bot and send a test alert.

GET the same URL for a credential-free diagnosis of the bot-alert path
(configured Bot API host, whether this server can reach it, what would stop
a connect attempt). Opening the URL in a browser is a quick deployment check.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)
api_common.json_errors(app)


@app.route("/", methods=["GET"])
@app.route("/api/bot_connect", methods=["GET"])
def bot_connect_status():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_bot_diagnostics()


@app.route("/", methods=["POST"])
@app.route("/api/bot_connect", methods=["POST"])
def bot_connect():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_bot_connect(request.get_json(silent=True) or {})


if __name__ == "__main__":
    app.run()
