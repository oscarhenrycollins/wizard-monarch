"""Regression for current official Monarch intent and offset contract. No real data."""
import importlib.util
import datetime as dt
from pathlib import Path

spec = importlib.util.spec_from_file_location('monarch', Path(__file__).resolve().parents[1] / 'app/server.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert m._tool_args('GetAccounts', {}) == {'intent': 'organizing_finances'}
assert m._tool_args('GetBudget', {}) == {'intent': 'budgeting'}
assert m._tool_args('GetAccounts', {'intent': 'debt_management'})['intent'] == 'debt_management'
for kind in m.SNAPSHOTS:
    a, missing = m._fill_args({'properties': {'intent': {}, 'start_date': {}, 'end_date': {}},
                             'required': ['intent', 'start_date', 'end_date']}, kind)
    assert not missing and a['intent'] != 'other'

calls = []
rows = [{'id': str(i)} for i in range(251)]
def call(name, args):
    assert name == 'GetTransactions' and args['limit'] <= 100
    offset = args['offset']
    calls.append(offset)
    batch = rows[offset:offset + args['limit']]
    return {'total_count': len(rows), 'transactions': batch,
            'next_offset': offset + len(batch) if offset + len(batch) < len(rows) else None}
m.call_tool = call
saved = []
m._save_transactions = lambda rows, start, end, complete: saved.append((rows, complete)) or len(rows)
today = dt.date.today()
assert m._pull(today, today) == 251
assert calls == [0, 100, 200] and saved[0][1]
assert len({r['id'] for r in saved[0][0]}) == 251
# A bad provider cursor must not replace the local window.
m.call_tool = lambda *a: {'total_count': 251, 'transactions': rows[:100], 'next_offset': 0}
try:
    m._pull(today, today)
    raise AssertionError('bad cursor accepted')
except m.MonarchError as e:
    assert e.code == 'pagination'
assert len(saved) == 1
print('PASS: intents, explicit-intent preservation, snapshot args, 251-row paging, invalid-cursor refusal')
