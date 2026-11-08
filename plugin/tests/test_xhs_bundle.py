import json,sys,tempfile,unittest
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from storage import Store
from bundled_assets import bundle_path,validate_bundle,import_bundle,sha

class XhsBundleTests(unittest.TestCase):
 def test_xhs_import_retains_public_source_and_user_edit_after_restart(self):
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);bundle=base/'小红书提示词库';bundle.mkdir();root=base/'library';root.mkdir()
   p=bundle/'作品.png';Image.new('RGB',(16,16),'green').save(p)
   item={'file':p.name,'name':p.stem,'tags':['画风'],'annotation':'这是作者公开的完整绘图指令','sha256':sha(p),'size':p.stat().st_size,'source_url':'https://www.xiaohongshu.com/explore/example','source_title':'作品','author':'作者','prompt_note':'需参考照片','prompt_origin':'post_text','note_id':'example'}
   (bundle/'manifest.json').write_text(json.dumps({'format':1,'folder':bundle.name,'items':[item]},ensure_ascii=False),encoding='utf-8')
   store=Store(root);result=import_bundle(store,bundle,str(root))
   self.assertEqual((result['folder'],result['imported']),('小红书提示词库',1));self.assertFalse((root/'image2.5参考').exists())
   store=Store(root);row=next(iter(store.db['items'].values()))
   for k in ['annotation','tags','source_url','author','prompt_note']:self.assertEqual(row[k],item[k])
   row['annotation']='朋友自己的修改';store.save();result=import_bundle(store,bundle,str(root))
   self.assertEqual(result['skipped'],1);self.assertEqual(row['annotation'],'朋友自己的修改')
   self.assertTrue(next(iter(store.db['bundledReferences'])).startswith('小红书提示词库|'))
 def test_only_packaged_folders_are_addressable(self):
  for folder in ['../outside','C:/outside','','图库资料']:
   with self.assertRaises(ValueError):bundle_path(Path('assets'),folder)
  self.assertEqual(bundle_path(Path('assets')),Path('assets/image2.5参考'))
  self.assertEqual(bundle_path(Path('assets'),'小红书提示词库'),Path('assets/小红书提示词库'))

if __name__=='__main__':unittest.main()
