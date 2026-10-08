"""GET /api/events — history from the SQL database.

Returns the guard events and cleanup runs stored server-side when the
deployment has ``DATABASE_URL`` set (SQLite / PostgreSQL / MySQL — see
db.py), plus database stats and the bot pause flag. Without a database it
answers ``{"enabled": false}`` so the UI can say so.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)
api_common.json_errors(app)


@app.route("/", methods=["GET"])
@app.route("/api/events", methods=["GET"])
def events():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_events(request.args)


if __name__ == "__main__":
    app.run()
