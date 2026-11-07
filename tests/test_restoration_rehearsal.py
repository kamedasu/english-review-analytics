from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests
from src.models import FetchState
from src.notion_parser import parse_review
from src.notion_client import NotionReviewBlock
from src.review_identity import sync_review_blocks
from src.review_migration import prepare_restoration, snapshot_diff, validate_snapshot
from src.migration_workspace import WorkspaceStorage, file_inventory, require_workspace
from src.storage.base import StorageError
from scripts.rehearse_review_migration import run_rehearsal
from scripts.fetch_review_snapshot import fetch_snapshot
from tests.test_review_identity import block

EMPTY = '40438d68-8a87-807f-b6fb-d9e622d2f378'


def snapshot(pages):
    from src.utils.hashing import content_hash
    return {'schema_version':1,'pages':[{'page_id':pid,'title':title,'last_edited_time':None,
        'markdown':'\n'.join(b.markdown for b in blocks),
        'reviews':[{'source_block_id':b.source_block_id,'markdown':b.markdown,'content_hash':content_hash(b.markdown)} for b in blocks]}
        for pid,title,blocks in pages]}


class RestorationTests(unittest.TestCase):
    def setUp(self):
        self.a = block('a','Original session')
        self.b = block('b','Another session')
        self.empty = NotionReviewBlock(EMPTY,'# 2026-10-25 Review')
        self.baseline = snapshot([('nov','2026-11',[self.a,self.b]),('oct','2026-10',[self.empty])])
        self.original = [parse_review(self.a.markdown,'nov','2026-11'),parse_review(self.empty.markdown,'oct','2026-10')]
        self.latest = deepcopy(self.baseline)
        self.state = FetchState()

    def test_restore_bind_exclude_and_repeated_execution_keep_identity(self):
        result = prepare_restoration(self.original,self.original,self.state,self.baseline,self.latest)
        self.assertEqual(result['counts'],{'bound':1,'added':1,'excluded':1})
        self.assertEqual(len(result['reviews']),2)
        self.assertEqual({r.review_id for r in result['reviews']},{self.original[0].review_id,'notion:b'})
        self.assertIn(EMPTY,result['state'].excluded_sources)
        before = [r.model_dump() for r in result['reviews']]
        repeat = prepare_restoration(self.original,result['reviews'],result['state'],self.baseline,self.latest)
        self.assertEqual([r.model_dump() for r in repeat['reviews']],before)
        self.assertEqual(repeat['counts'].get('added',0),0)
        self.assertEqual(repeat['counts'].get('excluded',0),0)
        blocks = (self.a,self.b,self.empty)
        synced = sync_review_blocks('nov','2026-11',blocks,result['reviews'],result['state'],[])
        self.assertEqual(len(synced.reviews),2)
        self.assertEqual(synced.held[0]['reason'],'excluded_source')

    def test_changed_deleted_and_new_sources_are_held_without_replacing_saved_content(self):
        changed = block('a','Edited original')
        new = block('new','New session after baseline',day='2026-11-02')
        latest = snapshot([('nov','2026-11',[changed,new]),('oct','2026-10',[self.empty])])
        result = prepare_restoration(self.original,self.original,self.state,self.baseline,latest)
        self.assertEqual({e['reason'] for e in result['held']},{'changed_since_baseline','deleted_since_baseline','new_since_baseline_requires_review'})
        self.assertEqual(result['reviews'][0].review_id,self.original[0].review_id)
        self.assertEqual(result['reviews'][0].topic,'Original session')
        self.assertEqual(len(result['snapshot_diff']['added']),1)
        self.assertEqual(len(result['snapshot_diff']['changed']),1)
        self.assertEqual(len(result['snapshot_diff']['deleted']),1)

    def test_edited_migration_held_source_does_not_silently_register_on_normal_sync(self):
        latest = snapshot([('nov','2026-11',[self.a,self.b,block('new','New session')]),('oct','2026-10',[self.empty])])
        result = prepare_restoration(self.original,self.original,self.state,self.baseline,latest)
        synced = sync_review_blocks('nov','2026-11',(self.a,self.b,block('new','Edited new session')),result['reviews'],result['state'],[])
        self.assertEqual(len(synced.reviews),2)
        self.assertEqual(synced.held[0]['reason'],'new_since_baseline_requires_review')

    def test_restored_review_supports_edit_and_a_b_a_with_stable_id(self):
        result = prepare_restoration(self.original,self.original,self.state,self.baseline,self.latest)
        ids = {r.review_id for r in result['reviews']}
        edited = block('b','Edited session')
        changed = sync_review_blocks('nov','2026-11',(self.a,edited),result['reviews'],result['state'],[])
        reverted = sync_review_blocks('nov','2026-11',(self.a,self.b),changed.reviews,result['state'],[])
        self.assertEqual({r.review_id for r in reverted.reviews},ids)
        self.assertEqual(reverted.updated_count,1)
        self.assertEqual(next(r.topic for r in reverted.reviews if r.source_block_id=='b'),'Another session')

    def test_duplicate_sources_or_hashes_and_same_topic_are_not_added(self):
        corrupt = deepcopy(self.baseline)
        corrupt['pages'][0]['reviews'][1]['source_block_id']='a'
        with self.assertRaises(ValueError): validate_snapshot(corrupt)
        b = block('b','Original session')
        baseline = snapshot([('nov','2026-11',[self.a,b]),('oct','2026-10',[self.empty])])
        result = prepare_restoration(self.original,self.original,self.state,baseline,baseline)
        self.assertEqual(result['counts'].get('added',0),0)
        self.assertTrue(result['held'])

    def test_outside_month_is_unchanged(self):
        other = parse_review(block('aug','Past',day='2026-08-01').markdown,'aug','2026-08')
        result = prepare_restoration(self.original+[other],self.original+[other],self.state,self.baseline,self.latest)
        kept = next(r for r in result['reviews'] if r.review_id==other.review_id)
        self.assertEqual(kept.model_dump(),other.model_dump())


class WorkspaceTests(RestorationTests):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        parent = Path(self.tmp.name).resolve()
        self.source=parent/'production';self.backup=parent/'data_backup_test';self.work=parent/'data_migration_work_test'
        (self.source/'processed').mkdir(parents=True);(self.source/'state').mkdir();(self.source/'raw').mkdir()
        (self.source/'processed/reviews.json').write_text(json.dumps([r.model_dump(mode='json') for r in self.original]))
        (self.source/'state/state.json').write_text(self.state.model_dump_json())
        for p in self.baseline['pages']:(self.source/'raw'/f"{p['title']}_{p['page_id']}.md").write_text(p['markdown'])
        files=file_inventory(self.source)
        shutil.copytree(self.source,self.backup);shutil.copytree(self.source,self.work)
        (self.work/'.migration_workspace.json').write_text(json.dumps({'source':str(self.source),'backup':str(self.backup),'work':str(self.work),'files':files}))
        self.storage=WorkspaceStorage(self.work)
        self.storage.save_json('notion_20261103.json',self.baseline);self.storage.save_json('notion_latest.json',self.latest)

    def test_workspace_apply_twice_and_protected_source_and_raw_empty_preserved(self):
        protected=file_inventory(self.source);backup=file_inventory(self.backup)
        report=run_rehearsal(self.work,True)
        first={p:self.storage.load_text(p) for p in ['processed/reviews.json','state/state.json']}
        repeat=run_rehearsal(self.work,True)
        self.assertEqual(report['after_count'],2)
        self.assertEqual(repeat['after_count'],2)
        self.assertEqual({p:self.storage.load_text(p) for p in first},first)
        self.assertEqual(file_inventory(self.source),protected);self.assertEqual(file_inventory(self.backup),backup)
        self.assertIn('# 2026-10-25 Review',self.storage.load_text('raw/2026-10_oct.md'))
        self.assertEqual(len(self.storage.load_json('audit/excluded_reviews.json')),1)

    def test_save_failure_rolls_back_all_workspace_data(self):
        before=file_inventory(self.work);original=self.storage.save_text
        failed=False
        def write(path,text):
            nonlocal failed
            original(path,text)
            if path=='state/state.json' and not failed:
                failed=True;raise StorageError('after-write failure')
        with patch('scripts.rehearse_review_migration.WorkspaceStorage',return_value=self.storage),patch.object(self.storage,'save_text',side_effect=write):
            with self.assertRaises(StorageError):run_rehearsal(self.work,True)
        self.assertEqual(file_inventory(self.work),before)

    def test_production_backup_and_symlink_paths_refused(self):
        for path in [self.source,self.backup]:
            with self.assertRaises(ValueError):WorkspaceStorage(path)
        (self.work/'escape').symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(ValueError):WorkspaceStorage(self.work)

    def test_protected_source_change_aborts_before_workspace_write(self):
        before=file_inventory(self.work)
        (self.source/'processed/reviews.json').write_text('[]')
        with self.assertRaises(ValueError):run_rehearsal(self.work,True)
        self.assertEqual(file_inventory(self.work),before)


class LatestFetchTests(unittest.TestCase):
    def test_successful_snapshot_uses_get_pagination_and_actual_heading_id(self):
        from tests.test_notion_identity import node
        metadata={'properties':{'title':{'type':'title','title':[{'plain_text':'2026-11'}]}},'last_edited_time':'2026-11-01T00:00:00Z'}
        payloads=[metadata,{'results':[node('h','heading_1','2026-11-01 Review')],'has_more':True,'next_cursor':'next'},
                  {'results':[node('p','paragraph','Learning content')],'has_more':False},metadata]
        responses=[Mock(status_code=200,json=Mock(return_value=p)) for p in payloads]
        getter=Mock(side_effect=responses)
        result=fetch_snapshot({'page':'2026-11'},'test-key',get=getter,interval=0)
        self.assertEqual(result['request_count'],4)
        self.assertEqual(result['pages'][0]['reviews'][0]['source_block_id'],'h')
        self.assertEqual(result['pages'][0]['stats']['cursor_requests'],1)
        self.assertTrue(all(call.args[0].startswith('https://api.notion.com/v1/') for call in getter.call_args_list))

    def test_api_error_never_returns_a_stale_snapshot_or_prints_response_body(self):
        response=Mock(status_code=403,text='private response body')
        with self.assertRaisesRegex(RuntimeError,'HTTP 403'):
            fetch_snapshot({'page':'2026-11'},'test-key',get=Mock(return_value=response),interval=0)
        with self.assertRaisesRegex(RuntimeError,'network request failed'):
            fetch_snapshot({'page':'2026-11'},'test-key',get=Mock(side_effect=requests.ConnectionError('private')),interval=0)
