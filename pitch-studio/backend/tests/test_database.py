from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy.exc import OperationalError

from backend import database


def driver_error(message: str) -> OperationalError:
    return OperationalError("SELECT 1", {}, OSError(message))


class DisconnectDetectionTests(unittest.TestCase):
    def test_recognises_observed_ssl_timeout(self):
        error = OSError(
            "consuming input failed: could not receive data from server: "
            "Operation timed out\nSSL SYSCALL error: Operation timed out"
        )
        self.assertTrue(database._is_network_disconnect(error))

    def test_does_not_classify_query_error_as_disconnect(self):
        self.assertFalse(database._is_network_disconnect(ValueError("invalid input syntax")))


class SessionInvalidationTests(unittest.TestCase):
    def test_invalidates_session_when_request_raises_driver_error(self):
        session = mock.Mock()
        with mock.patch.object(database, "SessionLocal", return_value=session):
            dependency = database.get_db()
            self.assertIs(next(dependency), session)
            with self.assertRaises(OperationalError):
                dependency.throw(driver_error("connection reset by peer"))
        session.invalidate.assert_called_once()
        session.close.assert_called_once()

    def test_invalidates_when_rollback_during_close_fails(self):
        session = mock.Mock()
        session.close.side_effect = driver_error("SSL SYSCALL error: Operation timed out")
        with mock.patch.object(database, "SessionLocal", return_value=session):
            dependency = database.get_db()
            self.assertIs(next(dependency), session)
            with self.assertRaises(StopIteration):
                next(dependency)
        session.invalidate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
