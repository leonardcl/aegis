"""Application configuration."""
import os
import secrets

from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
instance_dir = os.path.join(basedir, "instance")

# Load the project .env into the environment BEFORE the Config class reads it.
# Without this, HERMES_API_URL (and the rest) are only set when the launching
# shell happened to export them — so ticking "hermes" silently fell back to the
# deterministic path under plain `gunicorn run:app`, on reboot, or under systemd.
# Existing real env vars win over .env (override=False) so deployments can still
# override file values.
load_dotenv(os.path.join(basedir, ".env"), override=False)


class Config:
    # Never ship a known constant key. If SECRET_KEY is unset we generate a
    # random per-process key (sessions simply don't survive a restart) — strictly
    # safer than a hardcoded default that an attacker could use to forge sessions.
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    SECRET_KEY_IS_EPHEMERAL = not bool(os.environ.get("SECRET_KEY"))

    # --- Development guardrail bypass ------------------------------------- #
    # GUARDRAILS_DISABLED=1 makes the spend gate return ALLOW ("dev_bypass") and
    # skips output screening, for frictionless development. Defaults OFF; it lives
    # nowhere in .env, surfaces a loud "dev_bypass" rule in the ledger/approvals
    # UI, and logs a startup warning — so an enabled bypass can never hide. The
    # self-modification BLOCK (edit_policy/raise_cap/...) stays denied even here.
    GUARDRAILS_DISABLED = os.environ.get("GUARDRAILS_DISABLED", "").lower() in (
        "1", "true", "yes", "on")

    # Optional shared-credential HTTP Basic Auth over the whole UI, for guarding a
    # publicly-exposed demo. Format "user:password" (or just "password"). OFF when
    # unset — so it never adds friction to local dev or the test client.
    BASIC_AUTH = os.environ.get("AEGIS_BASIC_AUTH", "")

    _db_url = os.environ.get("DATABASE_URL")
    if _db_url:
        SQLALCHEMY_DATABASE_URI = _db_url
    else:
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(instance_dir, "aegis.sqlite")

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Hermes Agent integration.
    # HERMES_API_URL: OpenAI-compatible base, e.g. http://localhost:8642/v1
    # When empty, the deterministic local reasoner/council is used instead.
    HERMES_API_URL = os.environ.get("HERMES_API_URL", "")
    HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "")
    HERMES_MODEL = os.environ.get("HERMES_MODEL", "nvidia/nemotron-3-super-120b-a12b")
    HERMES_TIMEOUT = int(os.environ.get("HERMES_TIMEOUT", "90"))
    # Max tokens per Hermes reasoning turn (tool-augmented mode).
    HERMES_MAX_TOKENS = int(os.environ.get("HERMES_MAX_TOKENS", "400"))

    # Audit council deliberation strategy:
    #   "oneshot"    — one Hermes call produces all five voices (~5x fewer calls,
    #                  ~5x faster; the model server serialises requests so extra
    #                  calls only add latency). Default when running live.
    #   "sequential" — the classic one-call-per-persona loop (kept for A/B and as
    #                  the offline path where the local reasoner is instant).
    #   "auto"       — oneshot when live, sequential when offline.
    HERMES_COUNCIL_STRATEGY = os.environ.get("HERMES_COUNCIL_STRATEGY", "auto")
    # Token budget for the single one-shot council call (five sections need room).
    HERMES_COUNCIL_MAX_TOKENS = int(
        os.environ.get("HERMES_COUNCIL_MAX_TOKENS", "1600"))
    # The one-shot call does the work of five persona calls, so it needs a larger
    # timeout than a single turn. Safe: the council runs in a background thread
    # (the HTTP request already returned), bounded only by this value.
    HERMES_COUNCIL_TIMEOUT = int(os.environ.get("HERMES_COUNCIL_TIMEOUT", "300"))
    # This Hermes build hangs on the OpenAI `tools` param; keep native off and use
    # tool-augmented mode (run tools server-side, inject results, real reasoning).
    HERMES_NATIVE_TOOLS = os.environ.get("HERMES_NATIVE_TOOLS", "")

    # Optional bearer token guarding the /hermes/tools/* HTTP surface.
    HERMES_TOOL_TOKEN = os.environ.get("HERMES_TOOL_TOKEN", "")

    # --- On-demand procurement -------------------------------------------- #
    # INTAKE: use the (slow, single-threaded) real agent to parse needs.
    # Off by default — the deterministic parser handles intake instantly.
    PROCUREMENT_INTAKE_HERMES = os.environ.get(
        "PROCUREMENT_INTAKE_HERMES", "").lower() in ("1", "true", "yes")
    # DISCOVER: "seed" (curated, deterministic, demo-safe) or "live"
    # (model-suggested candidates, falls back to seed on any failure).
    PROCUREMENT_DISCOVERY_MODE = os.environ.get("PROCUREMENT_DISCOVERY_MODE", "seed")
    PROCUREMENT_DISCOVERY_LIMIT = int(os.environ.get("PROCUREMENT_DISCOVERY_LIMIT", "4"))
    # EVALUATE: let Hermes write the recommendation narrative (deterministic
    # comparative narrative is the default + fallback).
    PROCUREMENT_EVAL_HERMES = os.environ.get(
        "PROCUREMENT_EVAL_HERMES", "").lower() in ("1", "true", "yes")
