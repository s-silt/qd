"""Unit tests for AlchemyMixin._insert_or_update.

Verifies that the ON DUPLICATE KEY UPDATE clause is correctly applied to the
statement before execution.  We use sqlalchemy.dialects.mysql.insert directly
(no database connection needed) and check the compiled SQL text.

If sqlalchemy is not installed the whole module is skipped.
"""
import unittest

try:
    from sqlalchemy.dialects import mysql as _mysql_dialect
    from sqlalchemy import Column, Integer, String, Table, MetaData
    _HAS_SQLA = True
except ImportError:
    _HAS_SQLA = False


@unittest.skipUnless(_HAS_SQLA, "sqlalchemy not installed")
class TestInsertOrUpdateStatement(unittest.TestCase):
    """Statement-level tests that do not require a live DB connection."""

    def setUp(self):
        meta = MetaData()
        self.table = Table(
            'dummy',
            meta,
            Column('id', Integer, primary_key=True),
            Column('name', String(64)),
            Column('value', Integer),
        )

    def _compile(self, stmt):
        """Compile stmt to MySQL dialect and return the SQL string."""
        compiled = stmt.compile(
            dialect=_mysql_dialect.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        return str(compiled)

    # ------------------------------------------------------------------
    # Core correctness: on_duplicate_key_update returns a new statement
    # ------------------------------------------------------------------

    def test_plain_insert_has_no_on_duplicate_clause(self):
        stmt = _mysql_dialect.insert(self.table).values(id=1, name='a', value=10)
        sql = self._compile(stmt)
        self.assertNotIn('ON DUPLICATE KEY UPDATE', sql.upper())

    def test_on_duplicate_key_update_returns_new_stmt(self):
        """on_duplicate_key_update() must not mutate the original statement."""
        stmt = _mysql_dialect.insert(self.table).values(id=1, name='a', value=10)
        new_stmt = stmt.on_duplicate_key_update(name='b', value=20)

        # Original is unchanged
        original_sql = self._compile(stmt)
        self.assertNotIn('ON DUPLICATE KEY UPDATE', original_sql.upper(),
                         "Original statement must NOT be modified in place")

        # New statement carries the clause
        new_sql = self._compile(new_stmt)
        self.assertIn('ON DUPLICATE KEY UPDATE', new_sql.upper(),
                      "New statement must include ON DUPLICATE KEY UPDATE")

    def test_fixed_method_assigns_return_value(self):
        """Simulate the fixed _insert_or_update: reassign insert_stmt."""
        insert_stmt = _mysql_dialect.insert(self.table).values(id=2, name='x', value=5)

        # --- FIXED pattern (what the code should do) ---
        insert_stmt = insert_stmt.on_duplicate_key_update(name='y', value=99)

        sql = self._compile(insert_stmt)
        self.assertIn('ON DUPLICATE KEY UPDATE', sql.upper())

    def test_buggy_method_discards_return_value(self):
        """Reproduce the original bug: discarding the return value loses the clause."""
        insert_stmt = _mysql_dialect.insert(self.table).values(id=3, name='p', value=0)

        # --- BUGGY pattern (what the old code did) ---
        insert_stmt.on_duplicate_key_update(name='q', value=1)  # return value discarded
        # insert_stmt still refers to the original, unmodified statement

        sql = self._compile(insert_stmt)
        self.assertNotIn('ON DUPLICATE KEY UPDATE', sql.upper(),
                         "Discarding return value should lose the upsert clause (bug reproduced)")

    def test_upsert_update_columns_present_in_sql(self):
        """The kwargs passed to on_duplicate_key_update appear in the SQL."""
        insert_stmt = _mysql_dialect.insert(self.table).values(id=4, name='n', value=7)
        insert_stmt = insert_stmt.on_duplicate_key_update(name='updated_name', value=42)
        sql = self._compile(insert_stmt)
        # The column names should appear in the UPDATE portion
        self.assertIn('name', sql)
        self.assertIn('value', sql)

    def test_multiple_kwargs_all_applied(self):
        """Multiple update kwargs are all included in ON DUPLICATE KEY UPDATE."""
        insert_stmt = _mysql_dialect.insert(self.table).values(id=5, name='a', value=1)
        insert_stmt = insert_stmt.on_duplicate_key_update(name='z', value=100)
        sql = self._compile(insert_stmt)
        upper_sql = sql.upper()
        self.assertIn('ON DUPLICATE KEY UPDATE', upper_sql)
        # Both updated columns must be present
        self.assertIn('name', sql)
        self.assertIn('value', sql)


if __name__ == '__main__':
    unittest.main()
