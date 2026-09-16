import json,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from storage import Store
class VideoStorageTests(unittest.TestCase):
 @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'requires FFmpeg')
 def test_formats_metadata_move_restart_and_atomic_conflict(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);folder=root/'seedance';folder.mkdir()
   for ext,codec in [('mp4','libx264'),('webm','libvpx-vp9')]:
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=96x64:r=12','-t','1','-c:v',codec,'-pix_fmt','yuv420p',str(folder/('clip.'+ext))],check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   s=Store(root);s.scan();s.scan();rows=s.items();self.assertEqual(len(rows),2)
   for r in rows:self.assertEqual(r['mediaType'],'video');self.assertGreater(r['duration'],0);self.assertIn('localVideo',r);self.assertTrue((s.cache/(r['id']+'.jpg')).is_file())
   for r in rows:
    before=json.loads(s.file.read_text(encoding='utf-8'))
    body={'requestId':'video-reject-123','confirmed':True,'libraryPath':str(root),'name':'Changed','prompt':'analysis','tags':['video'],'itemId':r['id'],'expected':{k:r[k] for k in ['name','tags','annotation','path','hash']}}
    with self.assertRaisesRegex(ValueError,'不接受视频'):s.save_analysis(body)
    self.assertEqual(json.loads(s.file.read_text(encoding='utf-8')),before)
    self.assertTrue(Path(r['localVideo']).exists())
   def entry(r):return {'id':r['id'],'expected':{k:r[k] for k in ['name','tags','annotation','path','hash']},'tags':['Seedance','动作'],'annotation':'仅原始提示词\n第二行'}
   body={'libraryPath':str(root),'items':[entry(r) for r in rows]}
   self.assertTrue(s.edit_many(body)['verified'])
   with self.assertRaises(ValueError):s.edit_many(body)
   self.assertTrue(all(r['annotation']=='仅原始提示词\n第二行' for r in Store(root).items()))
   r=s.item(rows[0]['id']);dest=root/'other';dest.mkdir();Path(r['localVideo']).rename(dest/('renamed.'+r['ext']));s.scan();s.scan()
   self.assertEqual(s.item(r['id'])['tags'],['Seedance','动作']);self.assertIn('other/',s.item(r['id'])['path'])
   fresh=[s.item(x['id']) for x in rows];bad={'libraryPath':str(root),'items':[entry(x) for x in fresh]};bad['items'][1]['expected']['annotation']='stale';bad['items'][0]['annotation']='must not save'
   with self.assertRaises(ValueError):s.edit_many(bad)
   self.assertEqual(s.item(rows[0]['id'])['annotation'],'仅原始提示词\n第二行')
if __name__=='__main__':unittest.main()
