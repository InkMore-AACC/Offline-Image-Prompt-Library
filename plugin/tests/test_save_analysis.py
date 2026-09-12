"""Save-analysis contract using temporary images only; no live service or gallery."""
import copy,io,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from PIL import Image
from storage import Store,digest
import hub

class SaveAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.root=self.base/'gallery';self.root.mkdir();self.store=Store(self.root)
        self.source=self.base/'source.png';Image.new('RGB',(33,55),'red').save(self.source)
    def tearDown(self):self.tmp.cleanup()
    def imported_body(self,**changes):
        body={'confirmed':True,'requestId':'analysis-request-123','name':'红色方块','tags':['色彩','极简'],
              'prompt':'原图反推提示词\n保留换行。','imagePath':str(self.source),'libraryPath':str(self.root)}
        body.update(changes);return body
    def existing_body(self):
        path=self.root/'original.png';Image.new('RGB',(33,55),'blue').save(path)
        self.store.scan();self.store.scan();row=self.store.items()[0]
        body=self.imported_body(itemId=row['id'],expected={k:row[k] for k in ('name','tags','annotation','path','hash')})
        del body['imagePath'];return body,row
    def test_strict_consent_and_source_exclusivity_before_request(self):
        for consent in (None,False,1,'true'):
            with self.subTest(consent=consent),patch('hub.request') as request:
                with self.assertRaises(ValueError):hub.call('offline_library_save_analysis',self.imported_body(confirmed=consent))
                request.assert_not_called()
        for body in (self.imported_body(itemId='both'),self.imported_body(imagePath='')):
            with self.assertRaises(ValueError):self.store.save_analysis(body)
        self.assertEqual(self.store.items(),[])
    def test_import_long_prompt_copy_restart_and_thumbnail(self):
        prompt='中文反推提示词\n'*150000;body=self.imported_body(prompt=prompt)
        result=self.store.save_analysis(body);row=result['item']
        self.assertTrue(result['saved'] and result['verified'] and result['imported'])
        self.assertEqual(row['annotation'],prompt);self.assertEqual(row['tags'],['色彩','极简'])
        self.assertTrue(self.source.exists());self.assertEqual(digest(self.source),digest(Path(row['localImage'])))
        self.assertTrue((self.store.cache/(row['id']+'.jpg')).is_file())
        self.assertEqual(Store(self.root).item(row['id'])['annotation'],prompt)
        self.assertEqual(Path(result['metadataPath']).resolve(),(self.root/'图库资料'/'素材信息.json').resolve())
        self.store.scan();self.assertEqual(len(self.store.items()),1)
    def test_changed_library_rejected(self):
        with self.assertRaisesRegex(ValueError,'目标不同'):
            self.store.save_analysis(self.imported_body(libraryPath=str(self.base)))
        self.assertEqual(self.store.items(),[])
    def test_mcp_returns_actionable_save_error(self):
        from urllib.error import HTTPError
        error=HTTPError('http://127.0.0.1',400,'Bad Request',{},io.BytesIO(json.dumps({'error':'目标图库已切换'},ensure_ascii=False).encode()))
        with patch('hub.ensure_server'),patch('hub.urlopen',side_effect=error):
            with self.assertRaisesRegex(RuntimeError,'目标图库已切换'):hub.request('/api/item?id=test')
    def test_retry_survives_restart_without_duplicate_or_rewrite(self):
        body=self.imported_body();first=self.store.save_analysis(body);before=self.store.file.read_bytes()
        restarted=Store(self.root);second=restarted.save_analysis(body)
        self.assertTrue(second['replayed']);self.assertEqual(first['item']['id'],second['item']['id'])
        self.assertEqual(before,restarted.file.read_bytes());self.assertEqual(restarted.revision,0)
        self.assertEqual(len(list(self.root.glob('*.png'))),1)
        with self.assertRaises(ValueError):restarted.save_analysis(dict(body,prompt='different'))
    def test_replay_does_not_undo_later_user_edit(self):
        body=self.imported_body();row=self.store.save_analysis(body)['item']
        self.store.edit({'id':row['id'],'field':'annotation','expected':row['annotation'],'value':'用户新改稿'})
        with self.assertRaises(ValueError):self.store.save_analysis(body)
        self.assertEqual(self.store.item(row['id'])['annotation'],'用户新改稿')
    def test_existing_save_is_one_transaction_and_keeps_identity(self):
        body,old=self.existing_body();revision=self.store.revision
        result=self.store.save_analysis(body);row=result['item']
        self.assertFalse(result['imported']);self.assertEqual(row['id'],old['id'])
        self.assertEqual(row['name'],'红色方块');self.assertEqual(row['annotation'],body['prompt'])
        self.assertFalse((self.root/'original.png').exists());self.assertTrue(Path(row['localImage']).is_file())
        self.assertEqual(self.store.revision,revision+1)
        self.assertTrue(self.store.save_analysis(body)['replayed'])
    def test_stale_or_missing_expected_never_overwrites(self):
        body,old=self.existing_body();before=self.store.file.read_bytes()
        for field in ('name','tags','annotation','path','hash'):
            stale=copy.deepcopy(body);del stale['expected'][field]
            with self.assertRaises(ValueError):self.store.save_analysis(stale)
        body['expected']['annotation']='stale'
        with self.assertRaises(ValueError):self.store.save_analysis(body)
        self.assertEqual(self.store.file.read_bytes(),before);self.assertTrue((self.root/'original.png').exists())
    def test_replaced_original_requires_new_analysis(self):
        body,old=self.existing_body();Image.new('RGB',(33,55),'green').save(self.root/'original.png')
        with self.assertRaisesRegex(ValueError,'图片内容已变化'):self.store.save_analysis(body)
        self.assertEqual(self.store.item(old['id'])['annotation'],'')
    def test_existing_name_collision_keeps_both_files(self):
        body,old=self.existing_body();collision=self.root/'红色方块.png';Image.new('RGB',(4,4),'yellow').save(collision)
        original=digest(self.root/'original.png');other=digest(collision)
        with self.assertRaisesRegex(ValueError,'同名文件'):self.store.save_analysis(body)
        self.assertEqual(digest(self.root/'original.png'),original);self.assertEqual(digest(collision),other)
        self.assertEqual(self.store.item(old['id'])['name'],'original')
    def test_existing_save_failure_restores_filename_and_metadata(self):
        body,old=self.existing_body();before=self.store.file.read_bytes()
        with patch('storage.atomic',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.store.save_analysis(body)
        self.assertEqual(before,self.store.file.read_bytes());self.assertTrue((self.root/'original.png').exists())
        self.assertFalse((self.root/'红色方块.png').exists());self.assertEqual(self.store.item(old['id'])['annotation'],'')
        self.assertTrue(self.store.save_analysis(body)['saved'])
    def test_import_save_failure_removes_only_its_copy_and_can_retry(self):
        body=self.imported_body();before=self.store.file.read_bytes()
        with patch('storage.atomic',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.store.save_analysis(body)
        self.assertEqual(before,self.store.file.read_bytes());self.assertTrue(self.source.is_file())
        self.assertEqual(list(self.root.glob('*.png')),[]);self.assertEqual(list(self.store.cache.iterdir()),[])
        self.assertTrue(self.store.save_analysis(body)['saved'])
    def test_duplicate_import_keeps_existing_metadata(self):
        body=self.imported_body();row=self.store.save_analysis(body)['item']
        with self.assertRaisesRegex(ValueError,'相同图片已有资料'):
            self.store.save_analysis(self.imported_body(requestId='different-request',name='另一个名字',prompt='不能覆盖'))
        self.assertEqual(self.store.item(row['id'])['annotation'],body['prompt']);self.assertEqual(len(self.store.items()),1)
    def test_duplicate_unindexed_file_is_not_imported_twice(self):
        (self.root/'manual.png').write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(ValueError,'图库中已有相同图片'):self.store.save_analysis(self.imported_body())
        self.assertEqual(len(list(self.root.glob('*.png'))),1)
    def test_import_filename_collision_preserves_unrelated_file(self):
        target=self.root/'红色方块.png';Image.new('RGB',(4,4),'yellow').save(target);before=target.read_bytes()
        with self.assertRaises(FileExistsError):self.store.save_analysis(self.imported_body())
        self.assertEqual(target.read_bytes(),before);self.assertEqual(self.store.items(),[])
    def test_invalid_image_leaves_no_import_or_receipt(self):
        self.source.write_bytes(b'not an image')
        with self.assertRaises(OSError):self.store.save_analysis(self.imported_body())
        self.assertEqual(self.store.items(),[]);self.assertNotIn('analysisSaves',self.store.db)
        self.assertEqual(list(self.root.glob('*.png')),[]);self.assertEqual(self.source.read_bytes(),b'not an image')
    def test_committed_verification_failure_keeps_save_for_retry(self):
        body=self.imported_body()
        with patch.object(self.store,'_analysis_result',side_effect=OSError('temporary read failure')):
            with self.assertRaises(OSError):self.store.save_analysis(body)
        self.assertEqual(len(self.store.items()),1)
        self.assertTrue(self.store.save_analysis(body)['replayed'])
    def test_http_route_and_mcp_dispatch_without_server(self):
        body=self.imported_body();raw=json.dumps(body).encode()
        handler=object.__new__(hub.Handler);handler.path='/api/save-analysis';handler.rfile=io.BytesIO(raw)
        handler.server=SimpleNamespace(csrf='isolated-test-token')
        handler.headers={'Host':f'127.0.0.1:{hub.PORT}','X-Library-Token':'isolated-test-token','Content-Length':str(len(raw))}
        output=[];handler.send=lambda value,status=200:output.append((value,status))
        with patch.object(hub,'STORE',self.store):handler.do_POST()
        self.assertEqual(output[0][1],200);self.assertTrue(output[0][0]['verified'])
        with patch('hub.request',return_value={'verified':True}) as request:
            self.assertTrue(hub.call('offline_library_save_analysis',body)['verified'])
            request.assert_called_once_with('/api/save-analysis',body)
        with patch('hub.request',return_value=output[0][0]['item']) as request:
            hub.call('offline_library_item',{'itemId':output[0][0]['item']['id']})
            self.assertIn('/api/item?id=',request.call_args.args[0])

if __name__=='__main__':unittest.main()
