#!/usr/bin/env python3
"""Run the web UI locally, without Vercel.

    python dev_server.py                 # http://127.0.0.1:8000
    PORT=9000 HOST=0.0.0.0 python dev_server.py

It serves ``index.html`` at ``/`` and mounts every function in ``api/*.py``
under the same ``/api/<name>`` URLs the Vercel deployment uses, so what you
test here is what you deploy. Your Telegram session still lives only in the
browser (localStorage) — this server stores nothing.
"""

import importlib.util
import os
from pathlib import Path

from flask import Flask, Response

ROOT = Path(__file__).resolve().parent

ENDPOINTS = ("auth_start", "auth_finish", "dialogs", "clean", "wipe", "leave",
             "guard", "delete_account", "events", "index")

app = Flask(__name__)


def load_endpoint(name):
    """Import api/<name>.py the same way Vercel does."""
    spec = importlib.util.spec_from_file_location(f"api_{name}",
                                                  ROOT / "api" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mount(module, name):
    """Copy every route of a serverless function onto this app."""
    for rule in module.app.url_map.iter_rules():
        if rule.rule == "/":       # the dev server serves index.html itself
            continue
        view = module.app.view_functions[rule.endpoint]
        methods = [m for m in rule.methods if m in ("GET", "POST")]
        app.add_url_rule(rule.rule, endpoint=f"{name}:{rule.endpoint}",
                         view_func=view, methods=methods)


@app.route("/", methods=["GET"])
@app.route("/index.html", methods=["GET"])
def home():
    return Response((ROOT / "index.html").read_text(encoding="utf-8"),
                    mimetype="text/html")


for _name in ENDPOINTS:
    mount(load_endpoint(_name), _name)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"telegram-remover web UI on http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False)
