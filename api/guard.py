"""POST /api/guard — the join guard: auto-mod of new members in one chat.

Each call looks at the messages newer than ``cursor`` (so it starts at "now"
and never touches old history), acts on the joins it finds and returns the
next cursor. Kick/ban/purge additionally require the typed confirmation
phrase in ``confirm`` — see ``guard_core.required_confirmation``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

import api_common

app = Flask(__name__)


@app.route("/", methods=["POST"])
@app.route("/api/guard", methods=["POST"])
def guard():
    denied = api_common.guard()
    if denied:
        return denied
    return api_common.handle_guard(request.get_json(silent=True) or {},
                                   request.headers)


if __name__ == "__main__":
    app.run()
