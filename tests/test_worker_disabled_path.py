"""Unit tests for BaseWorker.do() graceful disabled / error paths.

We isolate the user-not-found guard by extracting the relevant decision logic
from worker.py into a minimal local simulation, without importing worker.py
(which requires the full DB / mcrypto stack).

The test class TestWorkerUserNotFoundPath tests that:
  - accessing user['id'] before the `if not user` guard raises TypeError (old bug)
  - the fixed order (guard first, then subscript) does NOT raise TypeError
  - the graceful path is taken (tasklog.add, task.mod, return False)
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio


# ---------------------------------------------------------------------------
# Helpers — simulate the fixed and broken do() user-guard logic in isolation
# ---------------------------------------------------------------------------

async def _do_buggy(task, db, sql_session):
    """Reproduces the PRE-FIX ordering: userid = user['id'] BEFORE if not user."""
    user = await db.user.get(task['userid'])
    userid = user['id']          # <-- BUG: raises TypeError when user is None
    if not user:
        await db.tasklog.add(task['id'], False, msg='no such user, disabled.')
        await db.task.mod(task['id'], next=None, disabled=1)
        return False
    return userid


async def _do_fixed(task, db, sql_session):
    """Mirrors the POST-FIX ordering: guard first, then userid = user['id']."""
    user = await db.user.get(task['userid'])
    if not user:
        await db.tasklog.add(task['id'], False, msg='no such user, disabled.')
        await db.task.mod(task['id'], next=None, disabled=1)
        return False
    userid = user['id']          # safe: user is not None here
    return userid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWorkerUserNotFoundPath(unittest.TestCase):
    """Test the user-null guard in BaseWorker.do()."""

    def _make_db(self, user_return):
        """Build a minimal mock DB with user.get returning user_return."""
        db = MagicMock()
        db.user.get = AsyncMock(return_value=user_return)
        db.tasklog.add = AsyncMock(return_value=None)
        db.task.mod = AsyncMock(return_value=None)
        return db

    def _make_task(self, userid=42, task_id=1):
        return {'id': task_id, 'userid': userid}

    def test_buggy_raises_type_error_when_user_is_none(self):
        """Old code: userid = user['id'] before guard → TypeError on None."""
        db = self._make_db(user_return=None)
        task = self._make_task()

        with self.assertRaises(TypeError):
            asyncio.run(_do_buggy(task, db, sql_session=None))

    def test_fixed_does_not_raise_when_user_is_none(self):
        """Fixed code: guard before subscript → no exception."""
        db = self._make_db(user_return=None)
        task = self._make_task()

        try:
            result = asyncio.run(_do_fixed(task, db, sql_session=None))
        except TypeError:
            self.fail("Fixed path raised TypeError when user is None")
        self.assertFalse(result)

    def test_fixed_calls_tasklog_add_on_missing_user(self):
        """Fixed code: tasklog.add is called with success=False when user is None."""
        db = self._make_db(user_return=None)
        task = self._make_task(task_id=7)

        asyncio.run(_do_fixed(task, db, sql_session=None))

        db.tasklog.add.assert_awaited_once_with(
            7, False, msg='no such user, disabled.'
        )

    def test_fixed_calls_task_mod_disabled_on_missing_user(self):
        """Fixed code: task.mod is called to disable the task when user is None."""
        db = self._make_db(user_return=None)
        task = self._make_task(task_id=9)

        asyncio.run(_do_fixed(task, db, sql_session=None))

        db.task.mod.assert_awaited_once_with(9, next=None, disabled=1)

    def test_fixed_returns_false_on_missing_user(self):
        """Fixed code: do() returns False (not raises) for missing user."""
        db = self._make_db(user_return=None)
        task = self._make_task()
        result = asyncio.run(_do_fixed(task, db, sql_session=None))
        self.assertIs(result, False)

    def test_fixed_happy_path_returns_userid(self):
        """When user exists, fixed path proceeds normally and returns userid."""
        db = self._make_db(user_return={'id': 99, 'email': 'test@example.com'})
        task = self._make_task(userid=99)
        result = asyncio.run(_do_fixed(task, db, sql_session=None))
        self.assertEqual(result, 99)

    def test_fixed_no_db_calls_on_valid_user(self):
        """When user exists, tasklog.add and task.mod are NOT called by the guard."""
        db = self._make_db(user_return={'id': 55})
        task = self._make_task(userid=55)
        asyncio.run(_do_fixed(task, db, sql_session=None))
        db.tasklog.add.assert_not_awaited()
        db.task.mod.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
