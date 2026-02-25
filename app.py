from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from pywebpush import WebPushException, webpush
from sqlalchemy import text
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "pocketkid.db"
LOCALES_DIR = BASE_DIR / "locales"

SUPPORTED_LANGUAGES = ("en", "it")

app = Flask(__name__)
app.config["SECRET_KEY"] = "pocketkid-secret-key-change-me"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # parent | child
    preferred_language = db.Column(db.String(5), nullable=True)

    def verify_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Wallet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    balance = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    child = db.relationship("User")


class Challenge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))


class OperationRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_type = db.Column(db.String(20), nullable=False)  # reward | withdrawal | deposit
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending | approved | rejected
    child_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    child = db.relationship("User", foreign_keys=[child_id])
    challenge = db.relationship("Challenge")
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    kind = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    child = db.relationship("User", foreign_keys=[child_id])
    actor = db.relationship("User", foreign_keys=[created_by])


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    kind = db.Column(db.String(40), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))

    user = db.relationship("User")


class RecurringMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    movement = db.Column(db.String(20), nullable=False)  # deposit | withdraw
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    frequency = db.Column(db.String(20), nullable=False)  # daily|weekly|biweekly|monthly
    description = db.Column(db.String(255), nullable=False)
    deposit_mode = db.Column(db.String(20), nullable=False, default="free")  # free | challenge
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=True)
    next_run_at = db.Column(db.DateTime, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    child = db.relationship("User", foreign_keys=[child_id])
    challenge = db.relationship("Challenge")
    creator = db.relationship("User", foreign_keys=[created_by])


class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    endpoint = db.Column(db.String(512), nullable=False, unique=True)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))
    last_seen_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))

    user = db.relationship("User")


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


@app.template_filter("eur")
def eur_filter(value: Decimal) -> str:
    return f"€ {Decimal(value):.2f}"


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


@app.context_processor
def inject_context():
    locale = get_locale()
    return {
        "user": current_user(),
        "_": tr,
        "current_locale": locale,
        "available_languages": SUPPORTED_LANGUAGES,
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


def create_notification(*, user_id: int, kind: str, message: str):
    db.session.add(Notification(user_id=user_id, kind=kind, message=message, is_read=False))
    send_web_push_notification(user_id=user_id, title="PocketKid", message=message)


def send_web_push_notification(*, user_id: int, title: str, message: str, url: str = "/dashboard"):
    subscriptions = PushSubscription.query.filter_by(user_id=user_id, is_active=True).all()
    if not subscriptions:
        return

    payload = json.dumps({"title": title, "body": message, "url": url})

    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {
                "p256dh": sub.p256dh,
                "auth": sub.auth,
            },
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
                message=tr("notif_recurring_failed", child=item.child.username, amount=f"{amount:.2f}"),
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


@app.before_request
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


@app.route("/")
def index():
    if not has_parent():
        return redirect(url_for("setup"))
    user = current_user()
    if user:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if has_parent():
        return redirect(url_for("login"))

    if request.method == "POST":
        parent_username = request.form.get("parent_username", "").strip()
        parent_password = request.form.get("parent_password", "")
        parent_lang = request.form.get("parent_language", "en")

        if not parent_username or len(parent_password) < 4:
            flash(tr("invalid_username_password"), "error")
            return redirect(url_for("setup"))
        if User.query.filter_by(username=parent_username).first():
            flash(tr("username_exists"), "error")
            return redirect(url_for("setup"))

        parent = User(
            username=parent_username,
            password_hash=generate_password_hash(parent_password),
            role="parent",
            preferred_language=parent_lang if parent_lang in SUPPORTED_LANGUAGES else "en",
        )
        db.session.add(parent)
        db.session.flush()

        create_child = request.form.get("create_first_child") == "1"
        if create_child:
            child_username = request.form.get("child_username", "").strip()
            child_password = request.form.get("child_password", "")
            child_balance = parse_amount(request.form.get("child_balance")) or Decimal("0.00")
            child_lang = request.form.get("child_language", "en")

            if child_username and len(child_password) >= 4 and not User.query.filter_by(username=child_username).first():
                child = User(
                    username=child_username,
                    password_hash=generate_password_hash(child_password),
                    role="child",
                    preferred_language=child_lang if child_lang in SUPPORTED_LANGUAGES else "en",
                )
                db.session.add(child)
                db.session.flush()
                db.session.add(Wallet(child_id=child.id, balance=child_balance))
                if child_balance > 0:
                    register_transaction(
                        child_id=child.id,
                        kind="parent_deposit",
                        amount=child_balance,
                        description=tr("initial_balance"),
                        created_by=parent.id,
                    )

        db.session.commit()
        flash(tr("setup_completed"), "success")
        return redirect(url_for("login"))

    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_parent():
        return redirect(url_for("setup"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if not user or not user.verify_password(password):
            flash(tr("invalid_credentials"), "error")
            return render_template("login.html")
        session["user_id"] = user.id
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required()
def dashboard():
    user = current_user()
    if user.role == "parent":
        children = User.query.filter_by(role="child").order_by(User.username.asc()).all()
        child_rows = []
        for child in children:
            wallet = get_wallet_by_child(child.id)
            pending_count = OperationRequest.query.filter_by(child_id=child.id, status="pending").count()
            child_rows.append({"child": child, "wallet": wallet, "pending_count": pending_count})

        pending_requests = (
            OperationRequest.query.filter_by(status="pending")
            .order_by(OperationRequest.created_at.desc())
            .all()
        )
        return render_template("parent_dashboard.html", child_rows=child_rows, pending_requests=pending_requests)

    wallet = get_wallet_by_child(user.id)
    challenges = Challenge.query.filter_by(active=True).order_by(Challenge.name.asc()).all()
    requests = (
        OperationRequest.query.filter_by(child_id=user.id)
        .order_by(OperationRequest.created_at.desc())
        .limit(25)
        .all()
    )
    transactions = (
        Transaction.query.filter_by(child_id=user.id)
        .order_by(Transaction.created_at.desc())
        .limit(40)
        .all()
    )
    return render_template(
        "child_dashboard.html",
        wallet=wallet,
        challenges=challenges,
        requests=requests,
        transactions=transactions,
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required()
def settings():
    user = current_user()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "language":
            lang = request.form.get("preferred_language", "en")
            if lang in SUPPORTED_LANGUAGES:
                user.preferred_language = lang
                db.session.commit()
                flash(tr("language_updated"), "success")
            return redirect(url_for("settings"))

        if action == "password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            if not user.verify_password(current_password):
                flash(tr("current_password_invalid"), "error")
                return redirect(url_for("settings"))
            if len(new_password) < 4:
                flash(tr("password_too_short"), "error")
                return redirect(url_for("settings"))
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash(tr("password_updated"), "success")
            return redirect(url_for("settings"))

    return render_template("settings.html")


@app.route("/child/request/reward", methods=["POST"])
@login_required(role="child")
def child_request_reward():
    user = current_user()
    challenge_id = request.form.get("challenge_id")
    challenge = db.session.get(Challenge, challenge_id) if challenge_id else None

    if not challenge or not challenge.active:
        flash(tr("challenge_invalid"), "error")
        return redirect(url_for("dashboard"))

    description = request.form.get("description", "").strip() or tr("completed_challenge", challenge=challenge.name)

    db.session.add(
        OperationRequest(
            request_type="reward",
            status="pending",
            child_id=user.id,
            challenge_id=challenge.id,
            amount=challenge.amount,
            description=description,
        )
    )
    notify_all_parents(
        kind="approval_required",
        message=tr("notif_reward_request", child=user.username, challenge=challenge.name, amount=f"{Decimal(challenge.amount):.2f}"),
    )
    db.session.commit()
    flash(tr("reward_request_sent"), "success")
    return redirect(url_for("dashboard"))


@app.route("/child/request/withdrawal", methods=["POST"])
@login_required(role="child")
def child_request_withdrawal():
    user = current_user()
    amount = parse_amount(request.form.get("amount"))
    description = request.form.get("description", "").strip() or tr("withdrawal_request")

    if amount is None:
        flash(tr("invalid_amount"), "error")
        return redirect(url_for("dashboard"))

    db.session.add(
        OperationRequest(
            request_type="withdrawal",
            status="pending",
            child_id=user.id,
            amount=amount,
            description=description,
        )
    )
    notify_all_parents(kind="approval_required", message=tr("notif_withdraw_request", child=user.username, amount=f"{amount:.2f}"))
    db.session.commit()
    flash(tr("withdraw_request_sent"), "success")
    return redirect(url_for("dashboard"))


@app.route("/child/request/deposit", methods=["POST"])
@login_required(role="child")
def child_request_deposit():
    user = current_user()
    amount = parse_amount(request.form.get("amount"))
    description = request.form.get("description", "").strip() or tr("generic_deposit_request")

    if amount is None:
        flash(tr("invalid_amount"), "error")
        return redirect(url_for("dashboard"))

    db.session.add(
        OperationRequest(
            request_type="deposit",
            status="pending",
            child_id=user.id,
            amount=amount,
            description=description,
        )
    )
    notify_all_parents(kind="approval_required", message=tr("notif_deposit_request", child=user.username, amount=f"{amount:.2f}"))
    db.session.commit()
    flash(tr("deposit_request_sent"), "success")
    return redirect(url_for("dashboard"))


@app.route("/parent/request/<int:request_id>/decision", methods=["POST"])
@login_required(role="parent")
def parent_decide_request(request_id: int):
    user = current_user()
    operation_request = db.session.get(OperationRequest, request_id)
    if not operation_request or operation_request.status != "pending":
        flash(tr("request_not_found"), "error")
        return redirect(url_for("dashboard"))

    decision = request.form.get("decision")
    if decision not in {"approve", "reject"}:
        flash(tr("invalid_action"), "error")
        return redirect(url_for("dashboard"))

    operation_request.reviewed_at = datetime.now(UTC)
    operation_request.reviewed_by = user.id

    if decision == "reject":
        operation_request.status = "rejected"
        create_notification(user_id=operation_request.child_id, kind="request_rejected", message=tr("notif_request_rejected", description=operation_request.description))
        db.session.commit()
        flash(tr("request_rejected"), "success")
        return redirect(url_for("dashboard"))

    wallet = get_wallet_by_child(operation_request.child_id)
    amount = Decimal(operation_request.amount)

    if operation_request.request_type in {"reward", "deposit"}:
        wallet.balance = Decimal(wallet.balance) + amount
        operation_request.status = "approved"
        register_transaction(
            child_id=operation_request.child_id,
            kind="reward" if operation_request.request_type == "reward" else "requested_deposit",
            amount=amount,
            description=operation_request.description,
            created_by=user.id,
        )
        create_notification(user_id=operation_request.child_id, kind="wallet_credit", message=tr("notif_wallet_credit", amount=f"{amount:.2f}"))
        db.session.commit()
        flash(tr("request_approved_credit"), "success")
        return redirect(url_for("dashboard"))

    if operation_request.request_type == "withdrawal":
        if Decimal(wallet.balance) < amount:
            flash(tr("insufficient_balance"), "error")
            return redirect(url_for("dashboard"))
        wallet.balance = Decimal(wallet.balance) - amount
        operation_request.status = "approved"
        register_transaction(
            child_id=operation_request.child_id,
            kind="withdrawal",
            amount=-amount,
            description=operation_request.description,
            created_by=user.id,
        )
        create_notification(user_id=operation_request.child_id, kind="wallet_debit", message=tr("notif_wallet_debit", amount=f"{amount:.2f}"))
        db.session.commit()
        flash(tr("request_approved_debit"), "success")
        return redirect(url_for("dashboard"))

    flash(tr("unsupported_request"), "error")
    return redirect(url_for("dashboard"))


@app.route("/parent/child/<int:child_id>", methods=["GET"])
@login_required(role="parent")
def parent_child_wallet(child_id: int):
    child = db.session.get(User, child_id)
    if not child or child.role != "child":
        flash(tr("child_not_found"), "error")
        return redirect(url_for("dashboard"))

    wallet = get_wallet_by_child(child_id)
    transactions = Transaction.query.filter_by(child_id=child_id).order_by(Transaction.created_at.desc()).all()
    challenges = Challenge.query.filter_by(active=True).order_by(Challenge.name.asc()).all()
    return render_template("parent_child_wallet.html", child=child, wallet=wallet, transactions=transactions, challenges=challenges)


@app.route("/parent/child/<int:child_id>/manual", methods=["POST"])
@login_required(role="parent")
def parent_manual_movement(child_id: int):
    user = current_user()
    child = db.session.get(User, child_id)
    if not child or child.role != "child":
        flash(tr("child_not_found"), "error")
        return redirect(url_for("dashboard"))

    movement = request.form.get("movement")
    amount = parse_amount(request.form.get("amount"))
    deposit_mode = request.form.get("deposit_mode", "free")
    challenge_id = request.form.get("challenge_id")
    description = request.form.get("description", "").strip()

    if movement not in {"deposit", "withdraw"}:
        flash(tr("invalid_operation"), "error")
        return redirect(url_for("parent_child_wallet", child_id=child_id))

    if amount is None:
        flash(tr("invalid_amount"), "error")
        return redirect(url_for("parent_child_wallet", child_id=child_id))

    if deposit_mode not in {"free", "challenge"}:
        flash(tr("invalid_action"), "error")
        return redirect(url_for("parent_child_wallet", child_id=child_id))

    if movement != "deposit" and (deposit_mode != "free" or challenge_id):
        flash(tr("invalid_action"), "error")
        return redirect(url_for("parent_child_wallet", child_id=child_id))

    if movement == "deposit" and deposit_mode == "free" and challenge_id:
        flash(tr("invalid_action"), "error")
        return redirect(url_for("parent_child_wallet", child_id=child_id))

    challenge = None
    if movement == "deposit" and deposit_mode == "challenge":
        challenge = db.session.get(Challenge, challenge_id) if challenge_id else None
        if not challenge:
            flash(tr("challenge_invalid"), "error")
            return redirect(url_for("parent_child_wallet", child_id=child_id))

    if not description:
        if movement == "deposit" and challenge:
            description = tr("deposit_linked_challenge", challenge=challenge.name)
        else:
            description = tr("manual_parent_movement")

    wallet = get_wallet_by_child(child_id)
    if movement == "withdraw" and Decimal(wallet.balance) < amount:
        flash(tr("insufficient_balance"), "error")
        return redirect(url_for("parent_child_wallet", child_id=child_id))

    if movement == "deposit":
        wallet.balance = Decimal(wallet.balance) + amount
        signed_amount = amount
        kind = "parent_deposit_challenge" if challenge else "parent_deposit"
    else:
        wallet.balance = Decimal(wallet.balance) - amount
        signed_amount = -amount
        kind = "parent_withdrawal"

    register_transaction(child_id=child_id, kind=kind, amount=signed_amount, description=description, created_by=user.id)
    create_notification(
        user_id=child_id,
        kind="wallet_credit" if signed_amount >= 0 else "wallet_debit",
        message=tr("notif_parent_movement", amount=f"{signed_amount:.2f}", description=description),
    )
    db.session.commit()
    flash(tr("movement_saved"), "success")
    return redirect(url_for("parent_child_wallet", child_id=child_id))


@app.route("/parent/child/<int:child_id>/reset-password", methods=["POST"])
@login_required(role="parent")
def parent_reset_child_password(child_id: int):
    child = db.session.get(User, child_id)
    if not child or child.role != "child":
        flash(tr("child_not_found"), "error")
        return redirect(url_for("dashboard"))

    new_password = request.form.get("new_password", "")
    if len(new_password) < 4:
        flash(tr("password_too_short"), "error")
        return redirect(url_for("parent_child_wallet", child_id=child_id))

    child.password_hash = generate_password_hash(new_password)
    db.session.commit()
    flash(tr("child_password_reset_ok"), "success")
    return redirect(url_for("parent_child_wallet", child_id=child_id))


@app.route("/parent/child/<int:child_id>/delete", methods=["POST"])
@login_required(role="parent")
def delete_child(child_id: int):
    if request.form.get("double_confirmed") != "1":
        flash(tr("double_confirm_required"), "error")
        return redirect(url_for("parent_children"))

    child = db.session.get(User, child_id)
    if not child or child.role != "child":
        flash(tr("child_not_found"), "error")
        return redirect(url_for("parent_children"))

    Wallet.query.filter_by(child_id=child_id).delete()
    OperationRequest.query.filter_by(child_id=child_id).delete()
    Transaction.query.filter_by(child_id=child_id).delete()
    Notification.query.filter_by(user_id=child_id).delete()
    RecurringMovement.query.filter_by(child_id=child_id).delete()
    db.session.delete(child)
    db.session.commit()
    flash(tr("child_deleted"), "success")
    return redirect(url_for("parent_children"))


@app.route("/parent/challenges", methods=["GET", "POST"])
@login_required(role="parent")
def parent_challenges():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        amount = parse_amount(request.form.get("amount"))
        if not name or amount is None:
            flash(tr("invalid_name_or_amount"), "error")
            return redirect(url_for("parent_challenges"))
        db.session.add(Challenge(name=name, amount=amount, active=True))
        db.session.commit()
        flash(tr("challenge_created"), "success")
        return redirect(url_for("parent_challenges"))

    challenges = Challenge.query.order_by(Challenge.created_at.desc()).all()
    return render_template("parent_challenges.html", challenges=challenges)


@app.route("/parent/challenges/<int:challenge_id>/toggle", methods=["POST"])
@login_required(role="parent")
def toggle_challenge(challenge_id: int):
    challenge = db.session.get(Challenge, challenge_id)
    if not challenge:
        flash(tr("challenge_not_found"), "error")
        return redirect(url_for("parent_challenges"))
    challenge.active = not challenge.active
    db.session.commit()
    flash(tr("challenge_updated"), "success")
    return redirect(url_for("parent_challenges"))


@app.route("/parent/children", methods=["GET", "POST"])
@login_required(role="parent")
def parent_children():
    actor = current_user()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        language = request.form.get("preferred_language", "en")
        initial_balance = parse_amount(request.form.get("initial_balance")) or Decimal("0.00")

        if not username or len(password) < 4:
            flash(tr("invalid_username_password"), "error")
            return redirect(url_for("parent_children"))
        if User.query.filter_by(username=username).first():
            flash(tr("username_exists"), "error")
            return redirect(url_for("parent_children"))

        child = User(
            username=username,
            password_hash=generate_password_hash(password),
            role="child",
            preferred_language=language if language in SUPPORTED_LANGUAGES else "en",
        )
        db.session.add(child)
        db.session.flush()
        db.session.add(Wallet(child_id=child.id, balance=initial_balance))

        if initial_balance > 0:
            register_transaction(
                child_id=child.id,
                kind="parent_deposit",
                amount=initial_balance,
                description=tr("initial_balance"),
                created_by=actor.id,
            )

        db.session.commit()
        flash(tr("child_created"), "success")
        return redirect(url_for("parent_children"))

    children = User.query.filter_by(role="child").order_by(User.username.asc()).all()
    rows = [{"child": child, "wallet": get_wallet_by_child(child.id)} for child in children]
    return render_template("parent_children.html", child_rows=rows)


@app.route("/parent/parents", methods=["GET", "POST"])
@login_required(role="parent")
def parent_parents():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        language = request.form.get("preferred_language", "en")

        if not username or len(password) < 4:
            flash(tr("invalid_username_password"), "error")
            return redirect(url_for("parent_parents"))
        if User.query.filter_by(username=username).first():
            flash(tr("username_exists"), "error")
            return redirect(url_for("parent_parents"))

        db.session.add(
            User(
                username=username,
                password_hash=generate_password_hash(password),
                role="parent",
                preferred_language=language if language in SUPPORTED_LANGUAGES else "en",
            )
        )
        db.session.commit()
        flash(tr("parent_created"), "success")
        return redirect(url_for("parent_parents"))

    parents = User.query.filter_by(role="parent").order_by(User.username.asc()).all()
    return render_template("parent_parents.html", parents=parents)


@app.route("/parent/parent/<int:parent_id>/delete", methods=["POST"])
@login_required(role="parent")
def delete_parent(parent_id: int):
    actor = current_user()
    if request.form.get("double_confirmed") != "1":
        flash(tr("double_confirm_required"), "error")
        return redirect(url_for("parent_parents"))

    target = db.session.get(User, parent_id)
    if not target or target.role != "parent":
        flash(tr("parent_not_found"), "error")
        return redirect(url_for("parent_parents"))

    if target.id == actor.id:
        flash(tr("cannot_delete_self"), "error")
        return redirect(url_for("parent_parents"))

    parent_count = User.query.filter_by(role="parent").count()
    if parent_count <= 1:
        flash(tr("at_least_one_parent"), "error")
        return redirect(url_for("parent_parents"))

    Notification.query.filter_by(user_id=target.id).delete()
    db.session.delete(target)
    db.session.commit()
    flash(tr("parent_deleted"), "success")
    return redirect(url_for("parent_parents"))


@app.route("/parent/recurring", methods=["GET", "POST"])
@login_required(role="parent")
def parent_recurring():
    actor = current_user()

    if request.method == "POST":
        child_id = request.form.get("child_id", type=int)
        movement = request.form.get("movement", "deposit")
        amount = parse_amount(request.form.get("amount"))
        frequency = request.form.get("frequency", "weekly")
        deposit_mode = request.form.get("deposit_mode", "free")
        challenge_id = request.form.get("challenge_id")
        description = request.form.get("description", "").strip() or tr("recurring_movement")
        start_date = request.form.get("start_date", "").strip()

        child = db.session.get(User, child_id)
        if not child or child.role != "child":
            flash(tr("child_not_found"), "error")
            return redirect(url_for("parent_recurring"))
        if movement not in {"deposit", "withdraw"} or frequency not in {"daily", "weekly", "biweekly", "monthly"}:
            flash(tr("invalid_operation"), "error")
            return redirect(url_for("parent_recurring"))
        if amount is None:
            flash(tr("invalid_amount"), "error")
            return redirect(url_for("parent_recurring"))
        if deposit_mode not in {"free", "challenge"}:
            flash(tr("invalid_action"), "error")
            return redirect(url_for("parent_recurring"))
        if movement != "deposit" and (deposit_mode != "free" or challenge_id):
            flash(tr("invalid_action"), "error")
            return redirect(url_for("parent_recurring"))
        if movement == "deposit" and deposit_mode == "free" and challenge_id:
            flash(tr("invalid_action"), "error")
            return redirect(url_for("parent_recurring"))

        challenge = None
        if movement == "deposit" and deposit_mode == "challenge":
            challenge = db.session.get(Challenge, challenge_id) if challenge_id else None
            if not challenge:
                flash(tr("challenge_invalid"), "error")
                return redirect(url_for("parent_recurring"))
            if not description:
                description = tr("deposit_linked_challenge", challenge=challenge.name)

        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date).replace(tzinfo=UTC)
            except ValueError:
                flash(tr("invalid_date"), "error")
                return redirect(url_for("parent_recurring"))
        else:
            start_dt = datetime.now(UTC)

        db.session.add(
            RecurringMovement(
                child_id=child.id,
                movement=movement,
                amount=amount,
                frequency=frequency,
                description=description,
                deposit_mode=deposit_mode,
                challenge_id=challenge.id if challenge else None,
                next_run_at=start_dt,
                active=True,
                created_by=actor.id,
            )
        )
        db.session.commit()
        flash(tr("recurring_created"), "success")
        return redirect(url_for("parent_recurring"))

    children = User.query.filter_by(role="child").order_by(User.username.asc()).all()
    challenges = Challenge.query.filter_by(active=True).order_by(Challenge.name.asc()).all()
    recurring = RecurringMovement.query.order_by(RecurringMovement.next_run_at.asc()).all()
    return render_template("parent_recurring.html", children=children, challenges=challenges, recurring=recurring)


@app.route("/parent/recurring/<int:item_id>/toggle", methods=["POST"])
@login_required(role="parent")
def toggle_recurring(item_id: int):
    item = db.session.get(RecurringMovement, item_id)
    if not item:
        flash(tr("recurring_not_found"), "error")
        return redirect(url_for("parent_recurring"))
    item.active = not item.active
    db.session.commit()
    flash(tr("recurring_updated"), "success")
    return redirect(url_for("parent_recurring"))


@app.route("/api/notifications", methods=["GET"])
@login_required()
def notifications_feed():
    user = current_user()
    mark_read = request.args.get("mark_read") == "1"
    unread = (
        Notification.query.filter_by(user_id=user.id, is_read=False)
        .order_by(Notification.created_at.asc())
        .limit(30)
        .all()
    )
    items = [
        {
            "id": n.id,
            "kind": n.kind,
            "message": n.message,
            "created_at": normalize_dt(n.created_at).strftime("%d/%m/%Y %H:%M"),
        }
        for n in unread
    ]

    if mark_read:
        for n in unread:
            n.is_read = True
        db.session.commit()

    return {"items": items}


@app.route("/api/push/public-key", methods=["GET"])
@login_required()
def push_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.route("/api/push/subscribe", methods=["POST"])
@login_required()
def push_subscribe():
    user = current_user()
    payload = request.get_json(silent=True) or {}

    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return {"ok": False, "error": "invalid_subscription"}, 400

    subscription = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if subscription is None:
        subscription = PushSubscription(user_id=user.id, endpoint=endpoint, p256dh=p256dh, auth=auth, is_active=True)
        db.session.add(subscription)
    else:
        subscription.user_id = user.id
        subscription.p256dh = p256dh
        subscription.auth = auth
        subscription.is_active = True
        subscription.last_seen_at = datetime.now(UTC)

    db.session.commit()
    return {"ok": True}


@app.route("/api/push/unsubscribe", methods=["POST"])
@login_required()
def push_unsubscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint")
    if not endpoint:
        return {"ok": False, "error": "missing_endpoint"}, 400

    subscription = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if subscription:
        subscription.is_active = False
        db.session.commit()
    return {"ok": True}


with app.app_context():
    db.create_all()
    ensure_schema_updates()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
