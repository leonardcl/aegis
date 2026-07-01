"""Entry point for the Aegis CFO application.

Run configuration is environment-driven so the dev server is never accidentally
shipped with the interactive debugger exposed:

    FLASK_DEBUG=1   enable the Werkzeug debugger (LOCAL DEV ONLY — it is a remote
                    code-execution surface; never set it on a public host).
    HOST=127.0.0.1  bind address (default 0.0.0.0 to preserve tunnel/demo access).
    PORT=5000       port.

For anything resembling production use a real WSGI server:
    gunicorn -w 1 run:app          (one worker — the Hermes model serializes calls)
"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes", "on")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    if host == "0.0.0.0" and not os.environ.get("AEGIS_BASIC_AUTH"):
        print("\n[aegis] WARNING: binding 0.0.0.0 (public) without AEGIS_BASIC_AUTH "
              "set — anyone who can reach this host can approve/reject spend.\n")
    app.run(debug=debug, host=host, port=port)
