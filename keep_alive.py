"""
keep_alive.py
Simple Flask keep-alive helper. Call start_keep_alive() to run the server in a daemon thread.
"""

import threading
import os

def start_keep_alive(host: str = "0.0.0.0", port: int = None):
    try:
        from flask import Flask
    except Exception:
        return None

    if port is None:
        try:
            port = int(os.getenv("PORT", "8080"))
        except Exception:
            port = 8080

    app = Flask("tansen_keep_alive")

    @app.route("/")
    def index():
        return "Tansen bot is alive."

    def run_app():
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    t = threading.Thread(target=run_app, daemon=True)
    t.start()
    return t

