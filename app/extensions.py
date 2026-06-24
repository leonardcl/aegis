"""Shared Flask extensions, instantiated once and initialised in the app factory."""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
