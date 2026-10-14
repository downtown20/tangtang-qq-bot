# -*- coding: utf-8 -*-
'''T22 regression: bubble-count telemetry for 消息密度.

2026-10-14. The user named three symptoms: 回复长度不稳定 / 消息密度不合理 / 语言机械.

The first and third got instrumentation earlier (T17). The second was
UNMEASURABLE: reply_metrics recorded reply_len (how many characters) but not how
many bubbles the reply was actually delivered as. Density is precisely
'same amount of text, how many messages' - without the count there was no way
to see a density problem at all.

The data already existed: ReplyPipeline.last_delivered_parts is populated on
every send and handler.py already copies it to _last_checked_send_parts before
the telemetry write. Nobody wrote the number down.

Two things this file pins:
  1. The column exists AND is migrated onto pre-existing databases -
     'CREATE TABLE IF NOT EXISTS' does NOT add columns to a table that already
     exists, so without an explicit ALTER an old DB would fail every INSERT.
  2. bubble_count defaults to 0 and old call sites keep working.
'''
import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.store import Store


_LEGACY_SCHEMA = (
    'CREATE TABLE reply_metrics ('
    ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
    ' ts TEXT DEFAULT (datetime(\'now\',\'localtime\')),'
    ' target_key TEXT, target_type TEXT, reply_len INTEGER, meow INTEGER,'
    ' bracket_action INTEGER, question_end INTEGER, assistant_tell INTEGER,'
    ' hard_turn INTEGER)'
)


def _legacy_db(tmp_path):
    '''A reply_metrics table as it existed BEFORE T22 (no bubble_count).'''
    db = str(tmp_path / 'legacy.db')
    conn = sqlite3.connect(db)
    conn.execute(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO reply_metrics (target_key, target_type, reply_len) "
        "VALUES ('old', 'private', 42)")
    conn.commit()
    conn.close()
    return db


def _columns(db, table='reply_metrics'):
    conn = sqlite3.connect(db)
    try:
        return [r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)]
    finally:
        conn.close()


class TestMigration:
    '''An existing database must gain the column, or every INSERT breaks.'''

    def test_legacy_db_lacks_the_column(self, tmp_path):
        db = _legacy_db(tmp_path)
        assert 'bubble_count' not in _columns(db), (
            'fixture is wrong - this must start WITHOUT bubble_count')

    def test_store_init_adds_the_column(self, tmp_path):
        db = _legacy_db(tmp_path)
        Store(db)
        assert 'bubble_count' in _columns(db), (
            'CREATE TABLE IF NOT EXISTS does not add columns to an existing table '
            '- the explicit ALTER migration is missing or broken')

    def test_legacy_rows_survive_with_null(self, tmp_path):
        db = _legacy_db(tmp_path)
        Store(db)
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT reply_len, bubble_count FROM reply_metrics WHERE target_key='old'"
        ).fetchone()
        conn.close()
        assert row[0] == 42, 'migration lost existing data'
        assert row[1] is None, (
            'a pre-existing row must stay NULL - we genuinely do not know how many'
            ' bubbles it was sent as; inventing a value would fabricate history')

    def test_migration_is_idempotent(self, tmp_path):
        db = _legacy_db(tmp_path)
        Store(db)
        Store(db)  # second open must not raise
        assert _columns(db).count('bubble_count') == 1


class TestWriter:
    def test_record_accepts_bubble_count(self, tmp_path):
        st = Store(str(tmp_path / 'a.db'))
        st.record_reply_metric(target_key='k', target_type='private',
                               reply_len=80, bubble_count=3)
        conn = sqlite3.connect(str(tmp_path / 'a.db'))
        got = conn.execute(
            'SELECT reply_len, bubble_count FROM reply_metrics').fetchone()
        conn.close()
        assert got == (80, 3)

    def test_old_call_sites_still_work(self, tmp_path):
        '''Default 0 keeps every existing caller source-compatible.'''
        st = Store(str(tmp_path / 'b.db'))
        st.record_reply_metric(target_key='k', target_type='group', reply_len=10)
        conn = sqlite3.connect(str(tmp_path / 'b.db'))
        got = conn.execute('SELECT bubble_count FROM reply_metrics').fetchone()
        conn.close()
        assert got[0] == 0

    def test_signature_declares_bubble_count(self):
        sig = inspect.signature(Store.record_reply_metric)
        assert 'bubble_count' in sig.parameters
        assert sig.parameters['bubble_count'].default == 0

    def test_create_table_declares_the_column(self):
        src = inspect.getsource(Store)
        assert 'bubble_count INTEGER' in src, (
            'fresh databases must get the column from CREATE TABLE too')


class TestHandlerWiring:
    '''The value must actually come from the delivered parts, not a placeholder.'''

    def test_handler_records_delivered_parts_count(self):
        import io
        src = io.open(str(Path(__file__).parent.parent / 'agent' / 'handler.py'),
                      encoding='utf-8', errors='ignore').read()
        idx = src.find('record_reply_metric')
        assert idx > 0, 'the telemetry call site is gone'
        window = src[idx:idx + 2000]
        assert 'bubble_count=' in window, (
            'handler does not pass bubble_count - the column would always be 0')
        assert '_last_checked_send_parts' in window, (
            'bubble_count must come from the ACTUALLY DELIVERED parts,'
            ' not from the intended split')
