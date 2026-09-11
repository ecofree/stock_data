from pathlib import Path
import tempfile
import unittest

from tools.v2 import probe_money_gap_evidence as p
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import file_hash
from trade_system.v2.gap_evidence import read_json, write_json


class MoneyGapReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.folder = self.root/'receipts'; self.folder.mkdir()
        self.script = self.root/'capture.py'; self.script.write_text('# fixture')
        reg = dict(scope=p.SCOPE, requests=p.plan(), max_requests=8, automatic_retries=0,
            collection_manifest_id='collection', identity_manifest_id='identity', source_sha256=file_hash(self.script),
            received_after='2026-01-01T00:00:00+00:00', execution_ready=False, production_cutover=False)
        write_json(self.folder/'registration.json', reg)
        for i, request in enumerate(p.plan()):
            day = request['start']; money = request['api'] == 'moneyflow'
            if i in (5, 7):
                data = {'fields': [], 'items': []}
            elif i < 2:
                data = dict(thscode=request['code'], interval='1d', adjust='none', item=[
                    dict(date_ms=request['params']['start'], open_price=10, high_price=11, low_price=9,
                         close_price=10, volume=100, turnover=1000)])
            else:
                fields = p.old.API_FIELDS[request['api']]
                data = dict(fields=fields, items=[[request['code'], day.replace('-', ''),
                    *([1]*5 if money else [10, 11, 9, 10, 1, 1])]])
            write_json(self.folder/f'receipt-{i:02d}.json', dict(request=request, data=data,
                received_at='2026-01-01T00:00:01+00:00'))
            status = dict(index=i, request=request, receipt_sha256=file_hash(self.folder/f'receipt-{i:02d}.json'))
            status.update(dict(status='response_contract_rejected', error_type='ValueError', empty_payload=True)
                if i in (5, 7) else dict(status='observed', rows=1, dates=[day]))
            write_json(self.folder/f'status-{i:02d}.json', status)
        write_json(self.folder/'result.json', {'untrusted': 'derived results deliberately ignored'})
        seal(self.folder)

    def replay(self):
        return p.replay(self.folder, 'collection', 'identity', self.script)

    def mutate(self, name, change):
        path = self.folder/name; obj = read_json(path)[0]; change(obj)
        path.unlink(); write_json(path, obj)
        (self.folder/'completed.json').unlink(); seal(self.folder)

    def test_eight_raw_responses_replayed_without_derived_result(self):
        _, rows, empty = self.replay()
        self.assertEqual(empty, [5, 7]); self.assertEqual(len(rows), 8)

    def test_row_count_tampering_rejected_after_reseal(self):
        self.mutate('status-04.json', lambda x: x.update(rows=2))
        with self.assertRaises(ValueError): self.replay()

    def test_registration_manifest_tampering_rejected(self):
        self.mutate('registration.json', lambda x: x.update(collection_manifest_id='wrong'))
        with self.assertRaises(ValueError): self.replay()

    def test_identity_manifest_mixing_rejected(self):
        with self.assertRaises(ValueError):
            p.replay(self.folder, 'collection', 'another_identity', self.script)

    def test_status_dates_must_match_raw(self):
        self.mutate('status-04.json', lambda x: x.update(dates=['2024-01-03']))
        with self.assertRaises(ValueError): self.replay()

    def test_native_identity_echo_required(self):
        self.mutate('receipt-00.json', lambda x: x['data'].update(thscode='689009.SH'))
        self.mutate('status-00.json', lambda x: x.update(receipt_sha256=file_hash(self.folder/'receipt-00.json')))
        with self.assertRaises(ValueError): self.replay()

    def test_authority_numeric_zero_rejected(self):
        self.mutate('registration.json', lambda x: x.update(execution_ready=0))
        with self.assertRaises(ValueError): self.replay()

    def test_timestamp_and_receipt_hash_rebinding_rejected(self):
        self.mutate('receipt-00.json', lambda x: x.update(received_at='2025-01-01T00:00:00Z'))
        self.mutate('status-00.json', lambda x: x.update(receipt_sha256=file_hash(self.folder/'receipt-00.json')))
        with self.assertRaises(ValueError): self.replay()

    def test_nonempty_payload_cannot_be_called_empty(self):
        self.mutate('receipt-05.json', lambda x: x.update(data={'fields': [], 'items': [[1]]}))
        self.mutate('status-05.json', lambda x: x.update(receipt_sha256=file_hash(self.folder/'receipt-05.json')))
        with self.assertRaises(ValueError): self.replay()

    def test_script_fingerprint_required(self):
        self.script.write_text('# changed')
        with self.assertRaises(ValueError): self.replay()

    def test_boolean_request_index_rejected(self):
        self.mutate('status-00.json', lambda x: x.update(index=False))
        with self.assertRaises(ValueError): self.replay()

    def test_output_must_not_mutate_input_package(self):
        before = p.sealed(self.folder)
        with self.assertRaisesRegex(ValueError, 'new output namespace'):
            p.analyze(self.folder, self.root/'collection', self.root/'identity', self.script,
                      self.folder/'nested-result')
        self.assertEqual(before, p.sealed(self.folder))

    def test_money_units_and_nulls(self):
        fields = p.old.API_FIELDS['moneyflow'][2:]
        self.assertEqual(p.money_diff(dict.fromkeys(fields, '10000'), dict.fromkeys(fields, '1')), [])
        self.assertEqual(len(p.money_diff(dict.fromkeys(fields, None), dict.fromkeys(fields, '0'))), 5)

    def test_calculation_detects_missing_positive_and_identity_overlap(self):
        _, rows, _ = self.replay()
        rows[4] = {}; rows[6] = {}
        identities = {6: {}, 12: {'2025-02-17': {}}, 15: {'2025-02-17': {}}}
        result = p.calculate(['2024-01-02'], {}, rows, identities)
        self.assertEqual(result['summary']['missing_with_positive_volume_rows'], 2)
        self.assertEqual(result['transition']['overlap'], ['2025-02-17'])
        self.assertFalse(result['transition']['effective_date_boundary_matches'])


if __name__ == '__main__':
    unittest.main()
