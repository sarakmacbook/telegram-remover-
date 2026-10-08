"""POST /api/bot_connect — verify a bot and send a test alert.

GET the same URL for a credential-free diagnosis of the bot-alert path
(configured Bot API host, whether this server can reach it, what would stop
a connect attempt). Opening the URL in a browser is a quick deployment check.

If the deployment's bundle is incomplete, both verbs answer JSON naming the
missing piece instead of the platform's HTML error page — the web UI can only
report "something failed" from HTML, never what.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

try:
    import api_common
except Exception as e:      # noqa: BLE001 — a broken bundle must not hide this
    api_common = None
    IMPORT_ERROR = f"{type(e).__name__}: {e}"

app = Flask(__name__)
if api_common is not None:
    api_common.json_errors(app)


def _bundle_error():
    return {
        "error": "this deployment could not load its shared modules "
                 f"({IMPORT_ERROR}). Its serverless bundle is incomplete — "
                 "redeploy the whole repository so vercel.json's includeFiles "
                 "ships api_common.py, notify.py, db.py, guard_core.py and "
                 "remover_core.py.",
        "http_status": 500,
    }, 500


def _denied():
    """The access-token guard, or the bundle explanation, or None."""
    if api_common is None:
        return _bundle_error()
    return api_common.guard()


@app.route("/", methods=["GET"])
@app.route("/api/bot_connect", methods=["GET"])
def bot_connect_status():
    denied = _denied()
    if denied:
        return denied
    return api_common.handle_bot_diagnostics()


@app.route("/", methods=["POST"])
@app.route("/api/bot_connect", methods=["POST"])
def bot_connect():
    denied = _denied()
    if denied:
        return denied
    return api_common.handle_bot_connect(request.get_json(silent=True) or {})


if __name__ == "__main__":
    app.run()
