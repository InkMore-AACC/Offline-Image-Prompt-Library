import json,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from PIL import Image
from storage import Store,DATA

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'gallery';self.root.mkdir();self.s=Store(self.root,grace=.01)
    def tearDown(self):self.tmp.cleanup()
    def image(self,name,color='red'):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(40,80),color).save(p);return p
    def scan(self):self.s.scan();self.s.scan()
    def test_new_files_stability_palette(self):
        self.image('one.png');self.s.scan();self.assertEqual(self.s.items(),[]);self.s.scan();r=self.s.items()[0]
        self.assertEqual((r['width'],r['height']),(40,80));self.assertTrue(r['palettes']);self.assertEqual(r['annotation'],'')
    def test_long_prompt_restart_backup_and_conflict(self):
        self.image('one.png');self.scan();r=self.s.items()[0];text='中文长提示词\n'*150000
        self.s.edit({'id':r['id'],'field':'annotation','expected':'','value':text})
        self.assertEqual(Store(self.root).item(r['id'])['annotation'],text)
        self.assertTrue((self.s.meta/'元数据备份/素材信息.json.previous').exists())
        with self.assertRaises(ValueError):self.s.edit({'id':r['id'],'field':'annotation','expected':'','value':'stale'})
    def test_external_move_keeps_identity(self):
        p=self.image('one.png');self.scan();r=self.s.items()[0]
        self.s.edit({'id':r['id'],'field':'tags','expected':[],'value':['动物']})
        (self.root/'folder').mkdir();p.rename(self.root/'folder/two.png');self.scan()
        self.assertEqual(self.s.item(r['id'])['tags'],['动物']);self.assertEqual(self.s.item(r['id'])['path'],'folder/two.png')
    def test_delete_prunes_cache_and_selection(self):
        p=self.image('one.png');self.scan();r=self.s.items()[0];self.s.select('test-task-123',[r['id']]);p.unlink();self.s.scan()
        self.assertIn(r['id'],self.s.db['items']);time.sleep(.02);self.s.scan()
        self.assertNotIn(r['id'],self.s.db['items']);self.assertEqual(self.s.selected('test-task-123')['items'],[]);self.assertFalse((self.s.cache/(r['id']+'.jpg')).exists())
    def test_missing_root_does_not_delete(self):
        self.image('one.png');self.scan();other=self.root.with_name('offline');self.root.rename(other)
        self.s.scan();time.sleep(.02);self.s.scan();self.assertEqual(len(self.s.db['items']),1);other.rename(self.root)
    def test_twenty_selections_isolated(self):
        for n in range(20):self.image(str(n)+'.png')
        self.scan();ids=[r['id'] for r in self.s.items()];self.s.select('test-task-123',ids)
        self.assertEqual([r['id'] for r in self.s.selected('test-task-123')['items']],ids);self.assertEqual(self.s.selected('other-task-123')['items'],[])
    def test_rename_collision_and_metadata_exclusion(self):
        self.image('one.png');self.image('two.png','blue');self.scan();r=next(i for i in self.s.items() if i['name']=='one')
        with self.assertRaises(ValueError):self.s.edit({'id':r['id'],'field':'name','expected':'one','value':'two'})
        self.s.edit({'id':r['id'],'field':'name','expected':'one','value':'renamed'})
        self.assertTrue((self.root/'renamed.png').exists());self.scan();self.assertEqual(len(self.s.items()),2)
    def test_ambiguous_duplicates_do_not_steal_prompts(self):
        p=self.image('one.png');self.image('two.png');self.scan();r=next(i for i in self.s.items() if i['name']=='one')
        self.s.edit({'id':r['id'],'field':'annotation','expected':'','value':'identity'})
        p.rename(self.root/'third.png');self.scan()
        new=next(i for i in self.s.items() if i['name']=='third');self.assertEqual(new['annotation'],'');self.assertTrue(self.s.db['items'][r['id']]['ambiguous'])
    def test_move_and_replacement_keep_correct_prompt(self):
        p=self.image('one.png');self.scan();r=self.s.items()[0]
        self.s.edit({'id':r['id'],'field':'annotation','expected':'','value':'red only'})
        p.rename(self.root/'moved.png');self.image('one.png','blue');self.scan()
        self.assertEqual(self.s.item(r['id'])['path'],'moved.png')
        self.assertEqual(next(i for i in self.s.items() if i['path']=='one.png')['annotation'],'')
    def test_one_old_two_new_is_ambiguous(self):
        p=self.image('one.png');self.scan();r=self.s.items()[0]
        self.s.edit({'id':r['id'],'field':'annotation','expected':'','value':'original'})
        p.rename(self.root/'a.png');self.image('b.png');self.scan()
        self.assertTrue(self.s.db['items'][r['id']]['ambiguous'])
        self.assertTrue(all(i['annotation']=='' for i in self.s.items()))
    def test_restored_path_keeps_metadata_and_selection(self):
        p=self.image('one.png');self.scan();r=self.s.items()[0]
        self.s.edit({'id':r['id'],'field':'annotation','expected':'','value':'keep prompt'})
        self.s.edit({'id':r['id'],'field':'tags','expected':[],'value':['keep tag']})
        self.s.select('test-task-123',[r['id']])
        away=self.root.parent/'away.png';p.rename(away);self.s.scan();away.rename(p);self.scan()
        self.assertEqual(len(self.s.items()),1)
        restored=self.s.items()[0];self.assertEqual(restored['id'],r['id'])
        self.assertEqual(restored['annotation'],'keep prompt');self.assertEqual(restored['tags'],['keep tag'])
        self.assertEqual(self.s.selected('test-task-123')['items'][0]['id'],r['id'])
    def test_scan_save_failure_is_retried(self):
        from unittest.mock import patch
        self.image('one.png');self.s.scan()
        with patch('storage.atomic',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.s.scan()
        self.assertEqual(self.s.items(),[]);self.s.scan()
        self.assertEqual(len(Store(self.root).items()),1)
    def test_same_root_configure_reuses_store(self):
        import hub
        old=hub.STORE
        try:
            hub.STORE=self.s;hub.configure(str(self.root));self.assertIs(hub.STORE,self.s)
        finally:hub.STORE=old
    def test_corrupt_index_does_not_overwrite(self):
        self.s.file.write_text('broken',encoding='utf-8')
        with self.assertRaises(ValueError):Store(self.root)
        self.assertEqual(self.s.file.read_text(),'broken')

if __name__=='__main__':unittest.main()
