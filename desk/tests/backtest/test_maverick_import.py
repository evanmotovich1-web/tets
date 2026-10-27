"""Synthetic import contracts. The separate live receipt is not a unit fixture."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest

from backtest.contracts import Rejected, canonical, sealed, sha, sha_bytes, strategy_spec
from backtest.engine import validate_v2_rows
from backtest.maverick_import import build_snapshot, freeze_capture, maverick_manifest, STATE
from backtest.storage import load_dataset
from backtest.walkforward import CampaignRegistry, provider_manifest, evaluate_rows


def capture():
    start=datetime(2024,1,8,tzinfo=timezone.utc)
    labels=[];calendar=[];values=[]
    for price in [100,101,99,102,100,104,98,100]:
        while start.weekday()>4:start+=timedelta(days=1)
        day=start.date().isoformat();labels.append(day+'T00:00:00.000')
        calendar.append({'date':day,'bar_open_at':day+'T14:30:00.000Z','bar_close_at':day+'T21:00:00.000Z'})
        values.append([price,price+1,price-1,price,1000]);start+=timedelta(days=1)
    raw=json.dumps({'ticker':'SPY','status':'success','columns':['Open','High','Low','Close','Volume'],'index':labels,'data':values,'record_count':len(labels)})
    meta={'provider_version':'1.2.1','source_revision':'a'*40,'source_files_sha256':{'fixture.py':'b'*64},'source_dirty':False,
          'tool':'market_data_get_price_history','request':{'ticker':'SPY','start_date':'2024-01-08','end_date':'2024-01-18'},
          'retrieved_at':'2024-02-01T00:00:00.000Z','yfinance_version':'fixture','calendar_version':'fixture',
          'calendar_library':'pandas_market_calendars','calendar_name':'NYSE','timezone':'America/New_York','timezone_source':'stripped_naive_session_date',
          'adjustment_policy':'yfinance_auto_adjust_split_dividend','adjustment_as_of':'2024-02-01T00:00:00.000Z',
          'cache_policy':'fresh_isolated_empty_database','raw_response_sha256':sha_bytes(raw.encode()),'raw_format':'mcp_text_json',
          'price_rounding':'half_even_6_decimal_places','license_reference':'Synthetic contract fixture; contains no actual prices.'}
    return {'raw_response_json':raw,'calendar':calendar,'metadata':meta}


def with_payload(value,payload):
    value=deepcopy(value);value['raw_response_json']=json.dumps(payload);value['metadata']['raw_response_sha256']=sha_bytes(value['raw_response_json'].encode());return value


class MaverickImportTests(unittest.TestCase):
    def setUp(self):
        STATE.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='maverick-contract-',dir=STATE);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.capture=capture()

    def test_manifest_matches_saved_bytes_and_campaign_registry_provider_path(self):
        receipt=freeze_capture(self.capture,self.root)
        manifest=receipt['manifest'];raw=Path(receipt['data_path']).read_bytes()
        self.assertEqual(manifest['data_sha256'],sha_bytes(raw));self.assertEqual(manifest['row_count'],len(raw.splitlines()))
        self.assertEqual(manifest['classification'],'historical_unverified');self.assertEqual(manifest['partition'],'development')
        self.assertEqual(manifest['price_basis'],'split_dividend_adjusted');self.assertFalse(manifest['provenance']['point_in_time_verified'])
        self.assertEqual(manifest['source']['retrieved_at'],self.capture['metadata']['retrieved_at'])
        store=CampaignRegistry(receipt['campaign_registry_path'])
        try:self.assertEqual(store.dataset(manifest['manifest_sha256'])['manifest'],manifest)
        finally:store.close()
        loaded,rows=load_dataset(receipt['registry_path'],manifest['dataset_id'],strategy_version=None)
        self.assertEqual(loaded,manifest);self.assertTrue(all(r['available_at']==manifest['source']['retrieved_at'] for r in rows))
        self.assertTrue(all(r['available_at']>r['bar_close_at'] for r in rows))

    def test_raw_checksum_mismatch_rejects_before_publication(self):
        self.capture['raw_response_json']+=' '
        with self.assertRaisesRegex(Rejected,'hash_mismatch'):freeze_capture(self.capture,self.root)
        self.assertFalse(list(self.root.iterdir()))

    def test_snapshot_checksum_mismatch_rejects(self):
        snapshot=build_snapshot(self.capture);snapshot['rows'][0]['close']='999.00'
        with self.assertRaisesRegex(Rejected,'hash_mismatch'):provider_manifest(snapshot,'development')

    def test_future_available_at_rejects(self):
        payload=json.loads(self.capture['raw_response_json'])
        payload['available_at']=['2024-02-02T00:00:00.000Z']*len(payload['data'])
        with self.assertRaisesRegex(Rejected,'future_data'):freeze_capture(with_payload(self.capture,payload),self.root)
        self.assertFalse(list(self.root.iterdir()))

    def test_data_file_tampering_rejects(self):
        r=freeze_capture(self.capture,self.root);p=Path(r['data_path']);p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaisesRegex(Rejected,'hash_mismatch'):load_dataset(r['registry_path'],r['manifest']['dataset_id'],strategy_version=None)

    def test_validation_requires_development_reference_and_holdout_stays_denied(self):
        snapshot=build_snapshot(self.capture)
        with self.assertRaisesRegex(Rejected,'development_reference_required'):provider_manifest(snapshot,'validation')
        for partition in ['holdout','unknown']:
            with self.assertRaisesRegex(Rejected,'dataset_access_denied'):provider_manifest(snapshot,partition)

    def test_classification_and_qualification_flags_cannot_be_promoted(self):
        snapshot=build_snapshot(self.capture)
        for change in [{'classification':'historical_research'},{'point_in_time_verified':True},{'historical_eligible':True}]:
            body={k:v for k,v in snapshot.items() if k!='snapshot_sha256'};body.update(change)
            with self.assertRaisesRegex(Rejected,'provider_snapshot_mismatch'):provider_manifest(sealed(body,'snapshot_sha256'),'development')

    def test_modified_rows_with_resealed_hashes_still_disagree_with_raw_capture(self):
        snapshot=build_snapshot(self.capture);body={k:v for k,v in snapshot.items() if k!='snapshot_sha256'}
        body['rows'][0]['close']='100.500000';body['data_sha256']=sha_bytes(b''.join(canonical(r)+b'\n' for r in body['rows']))
        with self.assertRaisesRegex(Rejected,'provider_snapshot_mismatch'):provider_manifest(sealed(body,'snapshot_sha256'),'development')

    def test_early_close_is_counted_and_excluded(self):
        self.capture['calendar'][0]['bar_close_at']='2024-01-08T18:00:00.000Z'
        s=build_snapshot(self.capture)
        self.assertEqual(s['counts']['excluded']['early_close'],1);self.assertEqual(s['counts']['raw_rows'],8);self.assertEqual(s['counts']['returned_rows'],7)

    def test_invalid_ohlc_is_counted_and_excluded(self):
        payload=json.loads(self.capture['raw_response_json']);payload['data'][0][1]=1
        s=build_snapshot(with_payload(self.capture,payload));self.assertEqual(s['counts']['excluded']['invalid_ohlc'],1)

    def test_request_and_tool_identity_are_enforced(self):
        for key,value in [('tool','backtesting_run_backtest'),('cache_policy','shared_cache')]:
            c=deepcopy(self.capture);c['metadata'][key]=value
            with self.assertRaisesRegex(Rejected,'invalid_provenance'):build_snapshot(c)

    def test_data_outside_ignored_state_refuses(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(Rejected,'gitignored_state_required'):freeze_capture(self.capture,path)

    def test_provider_path_cannot_store_real_data_in_an_unignored_registry(self):
        with tempfile.TemporaryDirectory() as path:
            store=CampaignRegistry(Path(path)/'campaign.sqlite')
            try:
                with self.assertRaisesRegex(Rejected,'gitignored_state_required'):
                    store.import_provider(build_snapshot(self.capture),'development')
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM datasets').fetchone()[0],0)
            finally:store.close()

    def test_identical_capture_is_idempotent_and_incompatible_rewrite_is_not_silent(self):
        self.assertEqual(freeze_capture(self.capture,self.root),freeze_capture(self.capture,self.root))

    def test_hindsight_replay_is_explicit_and_strict_default_timing_is_unchanged(self):
        snapshot=build_snapshot(self.capture);manifest,rows=provider_manifest(snapshot,'development')
        with self.assertRaisesRegex(Rejected,'future_data'):validate_v2_rows(rows,'1d')
        spec=strategy_spec({'schema_version':2,'symbol':'SPY','family':'sma_cross_long','parameters':{'fast_window':1,'slow_window':2},'position':{'quantity':'1'},'rationale':'Synthetic hindsight import contract.'})
        record={'cost_model':{'commission_bps':'0','slippage_bps_per_side':'5','regulatory_fee_bps_on_sells':'0.3','rounding':'half_even'},
                'pass_criteria':{'bootstrap':{'block_bars':2,'resamples':1000,'seed':0}}}
        result=evaluate_rows(spec,record,{'initial_cash':'1000.00','walkforward':{'initial_train_bars':3,'test_bars':2}},manifest,rows)
        self.assertEqual(result['evaluated_rows'],5);self.assertGreater(len(result['orders']),0)
        self.assertEqual(result['availability_policy'],'hindsight_development_only')
        self.assertFalse(result['point_in_time_verified'])

    def test_worker_replays_development_without_backdating_availability(self):
        from backtest.worker import Worker
        receipt=freeze_capture(self.capture,self.root)
        request={'op':'run_backtest','strategy':{'schema_version':2,'symbol':'SPY','family':'sma_cross_long',
                 'parameters':{'fast_window':1,'slow_window':2},'position':{'quantity':'1'},'rationale':'Synthetic imported-data worker contract.'},
                 'dataset_id':receipt['manifest']['dataset_id'],'evaluation_window':{
                   'start':'2024-01-08T14:30:00.000Z','end_exclusive':'2024-01-18T00:00:00.000Z'},
                 'initial_cash':'1000.00','cost_model':{'commission_bps':'0','slippage_bps_per_side':'5'}}
        reply=Worker(receipt['registry_path'],self.root/'worker-artifacts').handle_raw(canonical(request))
        self.assertTrue(reply['ok'],reply)
        self.assertIn('historical_unverified_hindsight',reply['result']['reason_codes'])
        self.assertEqual(reply['result']['qualification'],'research_only')
        self.assertFalse(reply['result']['promoting'])


if __name__=='__main__':unittest.main()
