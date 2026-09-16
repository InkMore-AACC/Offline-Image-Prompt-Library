"""Media requests must release the configuration lock before expensive work."""
import json,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hub
from video_media import PreviewPending
class VideoRequestTests(unittest.TestCase):
 def test_stream_and_conversion_release_lock_and_head_does_not_prepare(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p=root/'clip.avi';p.write_bytes(b'fixture')
   class Store:
    cache=root
    def item(self,id):return {'id':id}
    def path(self,row):return p
   server=ThreadingHTTPServer(('127.0.0.1',0),hub.Handler);server.csrf='test'
   entered=threading.Event();release=threading.Event()
   def preview(path,cache,prepare=True):
    if not prepare:raise PreviewPending('pending')
    entered.set();release.wait(3);return path
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   try:
    with patch.object(hub,'PORT',server.server_port),patch.object(hub,'STORE',Store()),patch.object(hub,'playback_file',side_effect=preview):
     url=f'http://127.0.0.1:{server.server_port}/media?play=1&id=clip'
     with urlopen(Request(url,method='HEAD')) as r:self.assertEqual(r.status,202);self.assertEqual(r.read(),b'')
     result=[]
     def fetch():
      with urlopen(url) as r:result.append(r.read())
     client=threading.Thread(target=fetch);client.start();self.assertTrue(entered.wait(2))
     self.assertTrue(hub.CONFIG_LOCK.acquire(blocking=False));hub.CONFIG_LOCK.release()
     with urlopen(f'http://127.0.0.1:{server.server_port}/health',timeout=1) as r:self.assertEqual(json.load(r)['app'],'offline-image-library')
     release.set();client.join(3);self.assertEqual(result,[b'fixture'])
   finally:release.set();server.shutdown();server.server_close();thread.join()
if __name__=='__main__':unittest.main()
