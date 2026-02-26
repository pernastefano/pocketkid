from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import flash, g, redirect, request, session, url_for
from pywebpush import WebPushException, webpush
from sqlalchemy import text

from .config import APP_CREDITS, APP_REPO_URL, APP_VERSION, LOCALES_DIR, SUPPORTED_LANGUAGES
from .extensions import db
from .models import Notification, PushSubscription, RecurringMovement, Transaction, User, Wallet


def load_vapid_keys() -> tuple[str, str]:
    public_key = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip().replace("\\n", "\n")

    private_key_b64 = os.getenv("VAPID_PRIVATE_KEY_B64", "").strip()
    if not private_key and private_key_b64:
        private_key = base64.b64decode(private_key_b64).decode("utf-8")

    if not public_key or not private_key:
        raise RuntimeError(
            "Missing VAPID keys. Create a .env from .env.example and set VAPID_PUBLIC_KEY with either "
            "VAPID_PRIVATE_KEY or VAPID_PRIVATE_KEY_B64."
        )
    return public_key, private_key


VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY = load_vapid_keys()
VAPID_CLAIMS = {"sub": os.getenv("VAPID_SUBJECT", "mailto:pocketkid@example.com")}


def load_translations() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for lang in SUPPORTED_LANGUAGES:
        file_path = LOCALES_DIR / f"{lang}.json"
        if file_path.exists():
            result[lang] = json.loads(file_path.read_text(encoding="utf-8"))
        else:
            result[lang] = {}
    return result


TRANSLATIONS = load_translations()


def eur_filter(value: Decimal) -> str:
    return f"€ {Decimal(value):.2f}"


def capitalize_name(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    return text[:1].upper() + text[1:]


def parse_amount(raw: str | None) -> Decimal | None:
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return None
    if amount <= 0:
        return None
    return amount


def normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def current_user() -> User | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def has_parent() -> bool:
    return User.query.filter_by(role="parent").first() is not None


def detect_request_language() -> str:
    chosen = request.accept_languages.best_match(list(SUPPORTED_LANGUAGES))
    return chosen if chosen in SUPPORTED_LANGUAGES else "en"


def get_locale() -> str:
    if hasattr(g, "locale"):
        return g.locale
    user = current_user()
    if user and user.preferred_language in SUPPORTED_LANGUAGES:
        g.locale = user.preferred_language
    else:
        g.locale = detect_request_language() or "en"
    return g.locale


def tr(key: str, **kwargs) -> str:
    locale = get_locale()
    template = TRANSLATIONS.get(locale, {}).get(key) or TRANSLATIONS.get("en", {}).get(key) or key
    return template.format(**kwargs)


def inject_context():
    locale = get_locale()
    return {
        "user": current_user(),
        "_": tr,
        "current_locale": locale,
        "available_languages": SUPPORTED_LANGUAGES,
        "app_version": APP_VERSION,
        "app_credits": APP_CREDITS,
        "app_repo_url": APP_REPO_URL,
    }


def login_required(role: str | None = None):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if role and user.role != role:
                flash(tr("permission_denied"), "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)

        return wrapped

    return decorator


def ensure_schema_updates():
    columns = [row[1] for row in db.session.execute(text("PRAGMA table_info(user)"))]
    if "preferred_language" not in columns:
        db.session.execute(text("ALTER TABLE user ADD COLUMN preferred_language VARCHAR(5)"))
        db.session.commit()

    challenge_columns = [row[1] for row in db.session.execute(text("PRAGMA table_info(challenge)"))]
    if "hidden" not in challenge_columns:
        db.session.execute(text("ALTER TABLE challenge ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT 0"))
        db.session.commit()

    recurring_columns = [row[1] for row in db.session.execute(text("PRAGMA table_info(recurring_movement)"))]
    if "hidden" not in recurring_columns:
        db.session.execute(text("ALTER TABLE recurring_movement ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT 0"))
        db.session.commit()


def get_wallet_by_child(child_id: int) -> Wallet:
    wallet = Wallet.query.filter_by(child_id=child_id).first()
    if wallet is None:
        wallet = Wallet(child_id=child_id, balance=Decimal("0.00"))
        db.session.add(wallet)
        db.session.commit()
    return wallet


def register_transaction(*, child_id: int, kind: str, amount: Decimal, description: str, created_by: int | None):
    db.session.add(
        Transaction(
            child_id=child_id,
            kind=kind,
            amount=amount,
            description=description,
            created_by=created_by,
        )
    )


def send_web_push_notification(*, user_id: int, title: str, message: str, url: str = "/dashboard"):
    subscriptions = PushSubscription.query.filter_by(user_id=user_id, is_active=True).all()
    if not subscriptions:
        return

    payload = json.dumps({"title": title, "body": message, "url": url})
    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
            )
            sub.last_seen_at = datetime.now(UTC)
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code in {404, 410}:
                sub.is_active = False


def create_notification(*, user_id: int, kind: str, message: str):
    db.session.add(Notification(user_id=user_id, kind=kind, message=message, is_read=False))
    send_web_push_notification(user_id=user_id, title="PocketKid", message=message)


def notify_all_parents(*, kind: str, message: str):
    for parent in User.query.filter_by(role="parent").all():
        create_notification(user_id=parent.id, kind=kind, message=message)


def next_run(current: datetime, frequency: str) -> datetime:
    base = normalize_dt(current)
    if frequency == "daily":
        return base + timedelta(days=1)
    if frequency == "weekly":
        return base + timedelta(weeks=1)
    if frequency == "biweekly":
        return base + timedelta(weeks=2)
    return base + timedelta(days=30)


def process_recurring_movements():
    now = datetime.now(UTC)
    due = (
        RecurringMovement.query.filter(RecurringMovement.active.is_(True), RecurringMovement.next_run_at <= now)
        .order_by(RecurringMovement.next_run_at.asc())
        .limit(100)
        .all()
    )
    if not due:
        return

    for item in due:
        wallet = get_wallet_by_child(item.child_id)
        amount = Decimal(item.amount)

        if item.movement == "withdraw" and Decimal(wallet.balance) < amount:
            notify_all_parents(
                kind="recurring_failed",
                message=tr("notif_recurring_failed", child=capitalize_name(item.child.username), amount=f"{amount:.2f}"),
            )
            item.next_run_at = next_run(item.next_run_at, item.frequency)
            continue

        signed = amount if item.movement == "deposit" else -amount
        wallet.balance = Decimal(wallet.balance) + signed
        register_transaction(
            child_id=item.child_id,
            kind=f"recurring_{item.movement}",
            amount=signed,
            description=item.description,
            created_by=item.created_by,
        )
        create_notification(
            user_id=item.child_id,
            kind="wallet_credit" if signed >= 0 else "wallet_debit",
            message=tr("notif_recurring_applied", amount=f"{signed:.2f}", description=item.description),
        )
        item.next_run_at = next_run(item.next_run_at, item.frequency)

    db.session.commit()


def app_guardrails():
    endpoint = request.endpoint or ""
    if endpoint.startswith("static"):
        return

    if not has_parent() and endpoint not in {"setup", "index"}:
        return redirect(url_for("setup"))

    if has_parent() and endpoint == "setup":
        return redirect(url_for("login"))

    if current_user():
        process_recurring_movements()


def register_common_handlers(app):
    app.template_filter("eur")(eur_filter)
    app.template_filter("name_cap")(capitalize_name)
    app.context_processor(inject_context)
    app.before_request(app_guardrails)
