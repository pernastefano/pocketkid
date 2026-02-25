from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from flask import flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from .config import SUPPORTED_LANGUAGES
from .extensions import db
from .models import (
    Challenge,
    Notification,
    OperationRequest,
    PushSubscription,
    RecurringMovement,
    Transaction,
    User,
    Wallet,
)
from .services import (
    VAPID_PUBLIC_KEY,
    create_notification,
    current_user,
    get_wallet_by_child,
    has_parent,
    login_required,
    normalize_dt,
    notify_all_parents,
    parse_amount,
    register_transaction,
    tr,
)


def register_routes(app):
    PAGE_SIZE = 10

    def safe_page(param_name: str) -> int:
        page = request.args.get(param_name, 1, type=int)
        return page if page and page > 0 else 1

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
            children_page = safe_page("children_page")
            pending_page = safe_page("pending_page")

            children_pagination = (
                User.query.filter_by(role="child")
                .order_by(User.username.asc())
                .paginate(page=children_page, per_page=PAGE_SIZE, error_out=False)
            )
            child_rows = []
            for child in children_pagination.items:
                wallet = get_wallet_by_child(child.id)
                pending_count = OperationRequest.query.filter_by(child_id=child.id, status="pending").count()
                child_rows.append({"child": child, "wallet": wallet, "pending_count": pending_count})

            pending_pagination = (
                OperationRequest.query.filter_by(status="pending")
                .order_by(OperationRequest.created_at.desc())
                .paginate(page=pending_page, per_page=PAGE_SIZE, error_out=False)
            )
            return render_template(
                "parent_dashboard.html",
                child_rows=child_rows,
                pending_requests=pending_pagination.items,
                children_pagination=children_pagination,
                pending_pagination=pending_pagination,
            )

        wallet = get_wallet_by_child(user.id)
        challenges = Challenge.query.filter_by(active=True, hidden=False).order_by(Challenge.name.asc()).all()
        requests_page = safe_page("requests_page")
        transactions_page = safe_page("transactions_page")

        requests_pagination = (
            OperationRequest.query.filter_by(child_id=user.id)
            .order_by(OperationRequest.created_at.desc())
            .paginate(page=requests_page, per_page=PAGE_SIZE, error_out=False)
        )
        transactions_pagination = (
            Transaction.query.filter_by(child_id=user.id)
            .order_by(Transaction.created_at.desc())
            .paginate(page=transactions_page, per_page=PAGE_SIZE, error_out=False)
        )
        return render_template(
            "child_dashboard.html",
            wallet=wallet,
            challenges=challenges,
            requests=requests_pagination.items,
            transactions=transactions_pagination.items,
            requests_pagination=requests_pagination,
            transactions_pagination=transactions_pagination,
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

        if not challenge or not challenge.active or challenge.hidden:
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
        page = safe_page("page")
        tx_pagination = (
            Transaction.query.filter_by(child_id=child_id)
            .order_by(Transaction.created_at.desc())
            .paginate(page=page, per_page=PAGE_SIZE, error_out=False)
        )
        challenges = Challenge.query.filter_by(active=True, hidden=False).order_by(Challenge.name.asc()).all()
        return render_template(
            "parent_child_wallet.html",
            child=child,
            wallet=wallet,
            transactions=tx_pagination.items,
            challenges=challenges,
            transactions_pagination=tx_pagination,
        )

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
            if not challenge or challenge.hidden:
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

        page = safe_page("page")
        challenges_pagination = (
            Challenge.query.filter_by(hidden=False)
            .order_by(Challenge.created_at.desc())
            .paginate(page=page, per_page=PAGE_SIZE, error_out=False)
        )
        return render_template("parent_challenges.html", challenges=challenges_pagination.items, challenges_pagination=challenges_pagination)

    @app.route("/parent/challenges/<int:challenge_id>/toggle", methods=["POST"])
    @login_required(role="parent")
    def toggle_challenge(challenge_id: int):
        challenge = db.session.get(Challenge, challenge_id)
        if not challenge or challenge.hidden:
            flash(tr("challenge_not_found"), "error")
            return redirect(url_for("parent_challenges"))
        challenge.active = not challenge.active
        db.session.commit()
        flash(tr("challenge_updated"), "success")
        return redirect(url_for("parent_challenges"))

    @app.route("/parent/challenges/<int:challenge_id>/delete", methods=["POST"])
    @login_required(role="parent")
    def delete_challenge(challenge_id: int):
        challenge = db.session.get(Challenge, challenge_id)
        if not challenge or challenge.hidden:
            flash(tr("challenge_not_found"), "error")
            return redirect(url_for("parent_challenges"))

        try:
            db.session.delete(challenge)
            db.session.commit()
            flash(tr("challenge_deleted"), "success")
        except SQLAlchemyError:
            db.session.rollback()
            fallback = db.session.get(Challenge, challenge_id)
            if fallback:
                fallback.active = False
                fallback.hidden = True
                db.session.commit()
            flash(tr("challenge_hidden"), "success")
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

        page = safe_page("page")
        children_pagination = (
            User.query.filter_by(role="child")
            .order_by(User.username.asc())
            .paginate(page=page, per_page=PAGE_SIZE, error_out=False)
        )
        rows = [{"child": child, "wallet": get_wallet_by_child(child.id)} for child in children_pagination.items]
        return render_template("parent_children.html", child_rows=rows, children_pagination=children_pagination)

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

        page = safe_page("page")
        parents_total = User.query.filter_by(role="parent").count()
        parents_pagination = (
            User.query.filter_by(role="parent")
            .order_by(User.username.asc())
            .paginate(page=page, per_page=PAGE_SIZE, error_out=False)
        )
        return render_template(
            "parent_parents.html",
            parents=parents_pagination.items,
            parents_pagination=parents_pagination,
            parents_total=parents_total,
        )

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
                if not challenge or challenge.hidden:
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
        challenges = Challenge.query.filter_by(active=True, hidden=False).order_by(Challenge.name.asc()).all()
        page = safe_page("page")
        recurring_pagination = (
            RecurringMovement.query.filter_by(hidden=False)
            .order_by(RecurringMovement.next_run_at.asc())
            .paginate(page=page, per_page=PAGE_SIZE, error_out=False)
        )
        return render_template(
            "parent_recurring.html",
            children=children,
            challenges=challenges,
            recurring=recurring_pagination.items,
            recurring_pagination=recurring_pagination,
        )

    @app.route("/parent/recurring/<int:item_id>/toggle", methods=["POST"])
    @login_required(role="parent")
    def toggle_recurring(item_id: int):
        item = db.session.get(RecurringMovement, item_id)
        if not item or item.hidden:
            flash(tr("recurring_not_found"), "error")
            return redirect(url_for("parent_recurring"))
        item.active = not item.active
        db.session.commit()
        flash(tr("recurring_updated"), "success")
        return redirect(url_for("parent_recurring"))

    @app.route("/parent/recurring/<int:item_id>/delete", methods=["POST"])
    @login_required(role="parent")
    def delete_recurring(item_id: int):
        item = db.session.get(RecurringMovement, item_id)
        if not item or item.hidden:
            flash(tr("recurring_not_found"), "error")
            return redirect(url_for("parent_recurring"))

        try:
            db.session.delete(item)
            db.session.commit()
            flash(tr("recurring_deleted"), "success")
        except SQLAlchemyError:
            db.session.rollback()
            fallback = db.session.get(RecurringMovement, item_id)
            if fallback:
                fallback.active = False
                fallback.hidden = True
                db.session.commit()
            flash(tr("recurring_hidden"), "success")
        return redirect(url_for("parent_recurring"))

    @app.route("/api/notifications", methods=["GET"])
    @login_required()
    def notifications_feed():
        user = current_user()
        mark_read = request.args.get("mark_read") == "1"
        unread = (
            Notification.query.filter_by(user_id=user.id, is_read=False)
            .order_by(Notification.created_at.asc())
            .limit(PAGE_SIZE)
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
