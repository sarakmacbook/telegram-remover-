"""POST /api/auth_finish — confirm the login code (and 2FA if enabled).

Returns the Telethon StringSession, which the browser keeps in localStorage
and sends back as X-Tg-Session on every request.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)
api_common.json_errors(app)


@app.route("/", methods=["POST"])
@app.route("/api/auth_finish", methods=["POST"])
def auth_finish():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_auth_finish(request.get_json(silent=True) or {})


if __name__ == "__main__":
    app.run()
