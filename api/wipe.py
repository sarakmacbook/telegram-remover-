"""POST /api/wipe — wipe a chunk of the full history of one chat."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)


@app.route("/", methods=["POST"])
@app.route("/api/wipe", methods=["POST"])
def wipe():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_wipe(request.get_json(silent=True) or {},
                                  request.headers)


if __name__ == "__main__":
    app.run()
