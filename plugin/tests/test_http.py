"""Isolated service integration: never changes the user's gallery or Eagle."""
import json,os,socket,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from PIL import Image

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name);cls.gallery=cls.root/'images';cls.gallery.mkdir();state=cls.root/'state';state.mkdir()
        (state/'config.json').write_text(json.dumps({'path':str(cls.gallery)}))
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        cls.base=f'http://127.0.0.1:{port}';env=os.environ.copy();env.update(OFFLINE_LIBRARY_PORT=str(port),OFFLINE_LIBRARY_STATE=str(state))
        cls.proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/hub.py'),'serve'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                with urlopen(cls.base+'/api/status') as r:cls.token=json.load(r)['csrf']
                break
            except OSError:time.sleep(.1)
        else:raise RuntimeError('service failed')
    @classmethod
    def tearDownClass(cls):cls.proc.terminate();cls.proc.wait();cls.tmp.cleanup()
    def get(self,p):
        with urlopen(self.base+p) as r:return json.load(r)
    def post(self,p,body,token=None):
        r=Request(self.base+p,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Library-Token':self.token if token is None else token})
        with urlopen(r) as response:return json.load(response)
    def test_live_folder_pipeline(self):
        for n in range(20):Image.new('RGB',(48,72),'red').save(self.gallery/(str(n)+'.png'))
        for _ in range(80):
            data=self.get('/api/browse')
            if data['total']==20:break
            time.sleep(.1)
        self.assertEqual(data['total'],20);ids=[i['id'] for i in data['items']]
        self.post('/api/select',{'taskId':'test-task-123','ids':ids})
        self.assertEqual(len(self.get('/api/references?task=test-task-123')['items']),20)
        r=self.get('/api/item?id='+ids[0]);text='长提示词\n'*200000
        self.post('/api/edit',{'id':r['id'],'field':'annotation','expected':'','value':text})
        self.assertEqual(self.get('/api/item?id='+r['id'])['annotation'],text)
        with urlopen(self.base+'/media?thumb=1&id='+r['id']) as response:self.assertTrue(response.read().startswith(b'\xff\xd8'))
        with self.assertRaises(HTTPError):self.post('/api/select',{'taskId':'test-task-123','ids':[]},token='wrong')
        self.assertEqual(self.get('/api/references?task=other-task-123')['items'],[])
        self.assertTrue(self.get('/api/revision')['revision']>0)

if __name__=='__main__':unittest.main()
