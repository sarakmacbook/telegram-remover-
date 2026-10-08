"""POST /api/clean — delete a chunk of the user's own messages in one chat."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)


@app.route("/", methods=["POST"])
@app.route("/api/clean", methods=["POST"])
def clean():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_clean(request.get_json(silent=True) or {},
                                   request.headers)


if __name__ == "__main__":
    app.run()
