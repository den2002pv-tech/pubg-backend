import asyncio
import os
import threading
import uuid

import pytest
import psycopg
import database
import main
from scanner_diagnostics import ScannerDiagnosticStore


def test_save_keeps_event_loop_responsive_and_reports_failure(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    events = []
    monkeypatch.setattr(main, '_validate_telegram_init_data', lambda _: {'id': 1})
    monkeypatch.setattr(main, '_record_save_event', events.append)
    def persist(*args):
        entered.set()
        assert release.wait(2)
        raise RuntimeError('synthetic DB failure')
    monkeypatch.setattr(main, '_persist_collection', persist)
    async def exercise():
        task = asyncio.create_task(main.confirm_my_collection({'cards': [], 'save_id': 'save-test'}, 'test'))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert await asyncio.wait_for(main.health(), .1) == {'status': 'ok'}
        finally:
            release.set()
        with pytest.raises(RuntimeError):
            await task
    asyncio.run(exercise())
    assert events[0]['event'] == 'save_failed'
    assert events[0]['save_id'] == 'save-test'
    assert events[0]['save_api_ms'] >= events[0]['worker_wait_ms'] >= 0


def test_save_client_metrics_are_allowlisted_and_separate(monkeypatch, tmp_path):
    store = ScannerDiagnosticStore(directory=tmp_path / 'diagnostics')
    monkeypatch.setattr(main, 'SCANNER_DIAGNOSTICS', store)
    monkeypatch.setattr(main, '_validate_telegram_init_data', lambda _: {'id': 1})
    asyncio.run(main.record_save_client_result({'save_id': 'save-test', 'confirmed': True,
        'client_save_to_confirmation_ms': 123, 'client_save_request_ms': 100,
        'token': 'must-not-be-recorded', 'cards': ['private']}, 'test'))
    summary = store.summary()
    assert summary['scan_count'] == 0
    assert summary['save_client_timings']['client_save_to_confirmation_ms']['p95_ms'] == 123
    assert 'must-not-be-recorded' not in str(store.read_records())
    with pytest.raises(main.HTTPException):
        asyncio.run(main.record_save_client_result({'save_id': 'test', 'client_save_to_confirmation_ms': True}, 'test'))


@pytest.fixture
def isolated_database(monkeypatch):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL required for isolated PostgreSQL integration')
    schema = 'test_collection_' + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
    # A unique schema isolates every test even on a shared test server.
    monkeypatch.setenv('DATABASE_URL', psycopg.conninfo.make_conninfo(url, options=f'-c search_path={schema}'))
    database.init_db()
    database.sync_cards({'card_1': {'name': 'Synthetic 1'}, 'card_2': {'name': 'Synthetic 2'}})
    database.get_or_create_user(1)
    try:
        yield
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA {schema} CASCADE')


def test_postgres_repeat_quantity_wishlist_rollback_and_metrics(isolated_database):
    database.set_desired_cards(1, ['card_1', 'card_2'])
    metrics = {}
    with database.measure_database(metrics):
        database.set_user_card_quantities(1, [('card_1', 4), ('card_1', 2)])
    assert metrics['db_connection_count'] == 1
    assert metrics['db_sql_count'] == 2
    assert metrics['db_transaction_ms'] >= metrics['db_sql_ms'] >= 0
    database.set_user_card_quantities(1, [('card_1', 4)])
    assert database.get_user_cards(1)[0]['quantity'] == 4
    assert [c['id'] for c in database.get_desired_cards(1)] == ['card_2']
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        database.set_user_card_quantities(1, [('card_1', 9), ('unknown', 1)])
    assert database.get_user_cards(1)[0]['quantity'] == 4
    database.set_user_card_quantities(1, [('card_1', 0)])
    database.set_user_card_quantities(1, [])
    assert database.get_user_cards(1)[0]['quantity'] == 0
    assert [c['id'] for c in database.get_desired_cards(1)] == ['card_2']


def test_postgres_api_confirmation_is_idempotent(isolated_database, monkeypatch):
    events = []
    monkeypatch.setattr(main, '_validate_telegram_init_data', lambda _: {'id': 1})
    monkeypatch.setattr(main, '_record_save_event', events.append)
    payload = {'save_id': 'save-retry', 'cards': [{'id': 'card_1', 'quantity': 3}]}
    first = asyncio.run(main.confirm_my_collection(payload, 'test'))
    second = asyncio.run(main.confirm_my_collection(payload, 'test'))
    assert first == second
    assert second['cards'][0]['quantity'] == 3
    assert events[0]['db_connection_count'] == 3
    assert events[0]['db_sql_count'] == 4
    assert events[0]['event'] == 'save_completed'
    payload['cards'][0]['quantity'] = 7
    assert asyncio.run(main.confirm_my_collection(payload, 'test'))['cards'][0]['quantity'] == 7
