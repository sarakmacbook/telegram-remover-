"""GET /api/dialogs — list the user's chats."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)


@app.route("/", methods=["GET"])
@app.route("/api/dialogs", methods=["GET"])
def dialogs():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_dialogs(request.headers, request.args)


if __name__ == "__main__":
    app.run()
