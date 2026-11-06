from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

os.environ.setdefault("VAPID_PUBLIC_KEY", "test-public-key")
os.environ.setdefault("VAPID_PRIVATE_KEY", "test-private-key")

from pocketkid import create_app
from pocketkid.config import Settings
from pocketkid.extensions import db
from pocketkid.models import Notification, User


class NotificationFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Settings.SQLALCHEMY_DATABASE_URI = "sqlite://"
        cls.app = create_app()
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        with self.client.session_transaction() as session:
            session.clear()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()

    def seed_notifications(self) -> tuple[int, int, list[int], int]:
        created_at = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
        with self.app.app_context():
            parent = User(
                username="parent",
                password_hash="unused",
                role="parent",
                preferred_language="en",
            )
            child = User(
                username="child",
                password_hash="unused",
                role="child",
                preferred_language="en",
            )
            db.session.add_all([parent, child])
            db.session.flush()

            read_history = Notification(
                user_id=parent.id,
                kind="history",
                message="Previously read",
                is_read=True,
                created_at=created_at,
            )
            parent_unread = [
                Notification(
                    user_id=parent.id,
                    kind="test",
                    message=f"Parent unread {index}",
                    is_read=False,
                    created_at=created_at + timedelta(minutes=index + 1),
                )
                for index in range(2)
            ]
            child_unread = Notification(
                user_id=child.id,
                kind="private",
                message="Child private notification",
                is_read=False,
                created_at=created_at + timedelta(minutes=3),
            )
            db.session.add_all([read_history, *parent_unread, child_unread])
            db.session.commit()
            return (
                parent.id,
                child.id,
                [notification.id for notification in parent_unread],
                child_unread.id,
            )

    def login_as(self, user_id: int) -> None:
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def test_mark_read_returns_unread_items_once_and_preserves_history(self):
        parent_id, _, parent_notification_ids, _ = self.seed_notifications()
        self.login_as(parent_id)

        initial = self.client.get("/api/notifications").get_json()
        marked = self.client.get("/api/notifications?mark_read=1").get_json()
        reopened = self.client.get("/api/notifications").get_json()

        self.assertEqual(initial["unreadCount"], 2)
        self.assertEqual(
            [item["id"] for item in initial["items"]],
            list(reversed(parent_notification_ids)),
        )
        self.assertTrue(all(not item["is_read"] for item in initial["items"]))
        self.assertNotIn("Previously read", [item["message"] for item in initial["items"]])

        self.assertEqual(marked["unreadCount"], 0)
        self.assertEqual(
            [item["id"] for item in marked["items"]],
            list(reversed(parent_notification_ids)),
        )
        self.assertTrue(all(item["is_read"] for item in marked["items"]))
        self.assertEqual(reopened, {"items": [], "unreadCount": 0})

        with self.app.app_context():
            parent_notifications = Notification.query.filter_by(user_id=parent_id).all()
            self.assertEqual(len(parent_notifications), 3)
            self.assertTrue(all(notification.is_read for notification in parent_notifications))

    def test_new_notifications_appear_once_after_empty_state(self):
        parent_id, _, _, _ = self.seed_notifications()
        self.login_as(parent_id)
        self.client.get("/api/notifications?mark_read=1")

        with self.app.app_context():
            notification = Notification(
                user_id=parent_id,
                kind="test",
                message="New notification",
                is_read=False,
            )
            db.session.add(notification)
            db.session.commit()
            notification_id = notification.id

        new_items = self.client.get("/api/notifications").get_json()
        marked = self.client.get("/api/notifications?mark_read=1").get_json()
        reopened = self.client.get("/api/notifications").get_json()

        self.assertEqual(new_items["unreadCount"], 1)
        self.assertEqual([item["id"] for item in new_items["items"]], [notification_id])
        self.assertEqual(marked["unreadCount"], 0)
        self.assertEqual([item["id"] for item in marked["items"]], [notification_id])
        self.assertEqual(reopened, {"items": [], "unreadCount": 0})

    def test_feed_is_scoped_to_authenticated_user(self):
        parent_id, child_id, parent_notification_ids, child_notification_id = self.seed_notifications()

        self.login_as(parent_id)
        parent_feed = self.client.get("/api/notifications").get_json()
        self.login_as(child_id)
        child_feed = self.client.get("/api/notifications").get_json()

        self.assertEqual(
            {item["id"] for item in parent_feed["items"]},
            set(parent_notification_ids),
        )
        self.assertEqual(
            [item["id"] for item in child_feed["items"]],
            [child_notification_id],
        )

    def test_signed_out_user_is_redirected_to_login(self):
        self.seed_notifications()

        response = self.client.get("/api/notifications")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")


if __name__ == "__main__":
    unittest.main()
