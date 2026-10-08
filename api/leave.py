"""POST /api/leave — leave a chunk of groups/channels (keep-list respected)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)
api_common.json_errors(app)


@app.route("/", methods=["POST"])
@app.route("/api/leave", methods=["POST"])
def leave():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_leave(request.get_json(silent=True) or {},
                                   request.headers)


if __name__ == "__main__":
    app.run()
