"""Hermes Agent chatbot blueprint."""
from flask import Blueprint, jsonify, request

from ..extensions import db
from ..models import AgentMessage
from ..services.hermes_service import ask_hermes_agent

bp = Blueprint("agent", __name__, url_prefix="/agent")


@bp.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    page_context = data.get("context") or ""

    if not message:
        return jsonify({"reply": "Please type a message for Hermes."}), 400

    # Persist the user message
    db.session.add(
        AgentMessage(role="user", content=message, page_context=page_context)
    )

    result = ask_hermes_agent(message, context={"page": page_context})
    reply = result.get("reply", "")

    db.session.add(
        AgentMessage(role="assistant", content=reply, page_context=page_context)
    )
    db.session.commit()

    return jsonify({"reply": reply, "engine": result.get("engine", "local")})
