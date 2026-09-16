import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from storage import Store
from bundled_assets import sha,validate_bundle,import_bundle

class BundledAssetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.bundle=self.base/'bundle';self.bundle.mkdir();self.root=self.base/'gallery';self.root.mkdir()
        Image.new('RGB',(24,24),'red').save(self.bundle/'案例.png')
        self.entry={'file':'案例.png','name':'案例','tags':['分类','标签'],'annotation':'纯提示词\n'+'完整正文'*30000,'sha256':sha(self.bundle/'案例.png'),'size':(self.bundle/'案例.png').stat().st_size}
        self.write_manifest();self.store=Store(self.root)
    def write_manifest(self):
        (self.bundle/'manifest.json').write_text(json.dumps({'format':1,'folder':'image2.5参考','items':[self.entry]},ensure_ascii=False),encoding='utf-8')
    def run_import(self):return import_bundle(self.store,self.bundle,str(self.root))
    def test_first_import_restart_and_existing_edits_survive(self):
        self.assertEqual(self.run_import()['imported'],1)
        self.store=Store(self.root);row=next(iter(self.store.db['items'].values()))
        self.assertEqual(row['annotation'],self.entry['annotation']);self.assertEqual(row['tags'],self.entry['tags'])
        row['annotation']='用户后来修改';row['tags']=['自己的标签'];self.store.save()
        self.assertEqual(self.run_import()['skipped'],1);self.assertEqual(row['annotation'],'用户后来修改');self.assertEqual(row['tags'],['自己的标签'])
    def test_same_bytes_elsewhere_skip_without_changing_metadata(self):
        (self.root/'已有.png').write_bytes((self.bundle/'案例.png').read_bytes());self.store.scan();self.store.scan()
        row=next(iter(self.store.db['items'].values()));row['annotation']='原提示词';self.store.save()
        self.assertEqual(self.run_import()['skipped'],1);self.assertEqual(row['annotation'],'原提示词');self.assertFalse((self.root/'image2.5参考').exists())
    def test_same_name_different_bytes_preserved(self):
        target=self.root/'image2.5参考';target.mkdir();Image.new('RGB',(24,24),'blue').save(target/'案例.png');original=sha(target/'案例.png')
        self.assertEqual(self.run_import()['imported'],1);self.assertEqual(sha(target/'案例.png'),original);self.assertEqual(len(list(target.glob('*.png'))),2)
    def test_validation_and_wrong_library_fail_before_copy(self):
        with self.assertRaises(ValueError):import_bundle(self.store,self.bundle,str(self.base))
        self.entry['file']='../outside.png';self.write_manifest()
        with self.assertRaises(ValueError):self.run_import()
        self.assertFalse((self.root/'image2.5参考').exists())
        self.entry['file']='案例.png';self.entry['sha256']='0'*64;self.write_manifest()
        with self.assertRaises(ValueError):self.run_import()
        self.assertFalse((self.root/'image2.5参考').exists())
    def test_identical_images_with_distinct_case_prompts_are_preserved(self):
        second=dict(self.entry,file='另一案例.png',name='另一案例',annotation='另一提示词')
        (self.bundle/second['file']).write_bytes((self.bundle/self.entry['file']).read_bytes())
        (self.bundle/'manifest.json').write_text(json.dumps({'format':1,'folder':'image2.5参考','items':[self.entry,second]},ensure_ascii=False),encoding='utf-8')
        self.assertEqual(self.run_import()['imported'],2)
        self.assertEqual(len(self.store.db['items']),2)
        self.assertEqual({r['annotation'] for r in self.store.db['items'].values()},{self.entry['annotation'],second['annotation']})
        self.assertEqual(self.run_import()['skipped'],2)
    def test_failed_metadata_save_resumes_after_restart(self):
        save=self.store.save
        def failing_save():
            if any(r.get('annotation')==self.entry['annotation'] for r in self.store.db['items'].values()):raise OSError('simulated disk failure')
            save()
        with patch.object(self.store,'save',side_effect=failing_save):
            with self.assertRaises(OSError):self.run_import()
        self.store=Store(self.root);self.assertEqual(self.run_import()['imported'],1);self.assertEqual(len(self.store.db['items']),1)
    def test_interrupted_import_preserves_independent_edit(self):
        with patch.object(self.store,'scan',side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):self.run_import()
        self.run_import();row=next(iter(self.store.db['items'].values()))
        self.store.db['bundledReferences'][self.entry['file']+'|'+self.entry['sha256']]['done']=False
        row['annotation']='独立修改';self.store.save()
        result=self.run_import();self.assertEqual(result['preservedEdits'],1);self.assertEqual(row['annotation'],'独立修改')

if __name__=='__main__':unittest.main()
