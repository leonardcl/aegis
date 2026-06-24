"""Aegis CFO application factory."""
import os
import re
import secrets

from flask import Flask, Response, request
from markupsafe import Markup, escape

from .config import Config
from .extensions import db


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    # Ensure the instance folder exists (for the SQLite file)
    os.makedirs(app.instance_path, exist_ok=True)

    # --- Operational warnings (loud, so unsafe/dev states can't hide) -------- #
    if app.config.get("GUARDRAILS_DISABLED"):
        app.logger.warning(
            "GUARDRAILS_DISABLED is set — spend gate returns ALLOW (dev_bypass) "
            "and reply screening is off. DEVELOPMENT ONLY; unset for the demo.")
    if app.config.get("SECRET_KEY_IS_EPHEMERAL"):
        app.logger.warning(
            "SECRET_KEY not set — using a random per-process key (sessions reset "
            "on restart). Set SECRET_KEY in the environment for stable sessions.")

    _install_basic_auth(app)

    db.init_app(app)

    # Register blueprints
    from .routes.dashboard import bp as dashboard_bp
    from .routes.procurement import bp as procurement_bp
    from .routes.approvals import bp as approvals_bp
    from .routes.audit import bp as audit_bp
    from .routes.agent import bp as agent_bp
    from .routes.hermes_api import bp as hermes_api_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(procurement_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(agent_bp)
    app.register_blueprint(hermes_api_bp)

    # Template helpers
    @app.template_filter("money")
    def money(value):
        try:
            return f"${value:,.0f}"
        except (TypeError, ValueError):
            return "$0"

    @app.template_filter("mdbold")
    def mdbold(value):
        """Render the limited markdown the agent emits (**bold** + newlines).

        HTML-escapes first so agent/LLM text can never inject markup, then
        applies bold and line breaks. Safe to mark as Markup afterwards.
        """
        if not value:
            return ""
        safe = str(escape(value))
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        safe = safe.replace("\n", "<br>")
        return Markup(safe)

    @app.context_processor
    def inject_globals():
        return {"app_name": "Aegis CFO"}

    # Create tables on first run
    with app.app_context():
        db.create_all()

    return app


def _install_basic_auth(app):
    """Optional shared-credential HTTP Basic Auth over the whole app.

    Enabled only when AEGIS_BASIC_AUTH is set (Config.BASIC_AUTH), so it never
    interferes with local dev or the test client. Format: "user:password" or
    just "password" (any username accepted). Guards a publicly-exposed demo so
    an anonymous visitor cannot approve/reject spend.
    """
    creds = app.config.get("BASIC_AUTH", "")
    if not creds:
        return
    if ":" in creds:
        want_user, want_pass = creds.split(":", 1)
    else:
        want_user, want_pass = "", creds

    def _ok(auth):
        if not auth:
            return False
        user_ok = secrets.compare_digest(auth.username or "", want_user) if want_user else True
        pass_ok = secrets.compare_digest(auth.password or "", want_pass)
        return user_ok and pass_ok

    @app.before_request
    def _require_basic_auth():
        if _ok(request.authorization):
            return None
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="Aegis CFO"'},
        )
