"""POST /api/auth_start — send a Telegram login code to a phone number."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)


@app.route("/", methods=["POST"])
@app.route("/api/auth_start", methods=["POST"])
def auth_start():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_auth_start(request.get_json(silent=True) or {})


if __name__ == "__main__":
    app.run()
