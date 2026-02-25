from __future__ import annotations

from flask import Flask

from .config import Settings
from .extensions import db
from .routes import register_routes
from .services import ensure_schema_updates, register_common_handlers


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(Settings)

    db.init_app(app)
    register_common_handlers(app)
    register_routes(app)

    with app.app_context():
        db.create_all()
        ensure_schema_updates()

    return app
