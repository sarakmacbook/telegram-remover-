"""GET /api — serve the web UI (fallback if the static index.html is not
served at the root by the platform)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response

app = Flask(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@app.route("/", methods=["GET"])
@app.route("/api", methods=["GET"])
@app.route("/api/index", methods=["GET"])
def index():
    try:
        with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    except OSError:
        return Response(
            "<h1>telegram-remover</h1><p>Web UI file not found. "
            "The API endpoints live under /api/*.</p>",
            mimetype="text/html",
        )


if __name__ == "__main__":
    app.run()
