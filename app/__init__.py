"""Aegis CFO application factory."""
import os
import re

from flask import Flask
from markupsafe import Markup, escape

from .config import Config
from .extensions import db


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    # Ensure the instance folder exists (for the SQLite file)
    os.makedirs(app.instance_path, exist_ok=True)

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
