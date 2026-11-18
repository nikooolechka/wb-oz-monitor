import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import notify


class AlertQuietHoursTest(unittest.TestCase):
    def test_moscow_boundaries(self):
        msk = ZoneInfo("Europe/Moscow")
        cases = [
            (datetime(2026, 11, 16, 8, 59, 59, tzinfo=msk), True),
            (datetime(2026, 11, 16, 9, 0, 0, tzinfo=msk), False),
            (datetime(2026, 11, 16, 19, 59, 59, tzinfo=msk), False),
            (datetime(2026, 11, 16, 20, 0, 0, tzinfo=msk), True),
            (datetime(2026, 11, 20, 19, 59, 59, tzinfo=msk), False),
            (datetime(2026, 11, 20, 20, 0, 0, tzinfo=msk), True),
            (datetime(2026, 11, 21, 12, 0, 0, tzinfo=msk), True),
            (datetime(2026, 11, 22, 12, 0, 0, tzinfo=msk), True),
        ]
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(notify.automatic_alert_silent(moment), expected)

    def test_source_timezone_does_not_change_result(self):
        instant = datetime(2026, 11, 16, 6, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(notify.automatic_alert_silent(instant))  # 09:00 МСК
        los_angeles = instant.astimezone(ZoneInfo("America/Los_Angeles"))
        kiritimati = instant.astimezone(ZoneInfo("Pacific/Kiritimati"))
        self.assertFalse(notify.automatic_alert_silent(los_angeles))
        self.assertFalse(notify.automatic_alert_silent(kiritimati))

    def test_all_chunks_keep_one_frozen_flag(self):
        old_token, old_chat = notify.TOKEN, notify.CHAT_ID
        notify.TOKEN, notify.CHAT_ID = "test-token", "test-chat"
        calls = []

        class Response:
            def raise_for_status(self):
                return None

        def fake_post(_url, json, timeout):
            calls.append((json.copy(), timeout))
            return Response()

        try:
            with patch.object(notify, "automatic_alert_silent", return_value=True) as mode, \
                    patch.object(notify.requests, "post", side_effect=fake_post), \
                    patch.object(notify.time, "sleep", return_value=None):
                notify.send(("а" * 4096) + "\n" + ("б" * 4096))
            self.assertEqual(mode.call_count, 1)
            self.assertGreater(len(calls), 1)
            self.assertTrue(all(call[0]["disable_notification"] is True for call in calls))
        finally:
            notify.TOKEN, notify.CHAT_ID = old_token, old_chat


if __name__ == "__main__":
    unittest.main()
