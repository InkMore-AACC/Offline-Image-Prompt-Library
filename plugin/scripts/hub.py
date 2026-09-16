from video_media import serve_media, is_video, playback_file, PreviewPending
"""Standalone local folder library, HTTP and MCP. No Eagle calls."""
import argparse,json,mimetypes,os,secrets,subprocess,sys,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs,urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from storage import Store,atomic
import browse_backend
from bundled_assets import import_bundle

ROOT=Path(__file__).resolve().parents[1]
STATE=Path(os.environ.get('OFFLINE_LIBRARY_STATE',str(Path.home()/'Documents/Codex/OfflineLibraryData')))
PORT=int(os.environ.get('OFFLINE_LIBRARY_PORT','18766'));BASE=f'http://127.0.0.1:{PORT}'
STORE=None;CONFIG_LOCK=threading.RLock()

def current():
    if STORE is None:raise ValueError('请点击“选择图库文件夹”，选择本地图片目录')
    return STORE

def configure(path):
    global STORE
    with CONFIG_LOCK:
        root=Path(path).expanduser().resolve()
        if STORE is not None and STORE.root==root:return {'path':str(root)}
        store=Store(root);atomic(STATE/'config.json',{'path':str(store.root)},False);STORE=store
        browse_backend._cache=None
        return {'path':str(store.root)}

def library():return {'library':{'path':str(current().root),'name':current().root.name}}
def all_items():return current().items()

def monitor():
    while True:
        try:
            with CONFIG_LOCK:
                if STORE:STORE.scan()
        except Exception as e:
            if STORE:STORE.error=str(e)
        time.sleep(2)

def picker():
    import tkinter as tk
    from tkinter import filedialog
    app=tk.Tk();app.withdraw();app.attributes('-topmost',True)
    try:return filedialog.askdirectory(parent=app,title='选择离线图片图库文件夹')
    finally:app.destroy()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,value,status=200,ctype='application/json; charset=utf-8'):
        raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
        try:
            if self.command!='HEAD':self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    def valid(self):return self.headers.get('Host')==f'127.0.0.1:{PORT}'
    def do_HEAD(self):
     self.do_GET()
    def do_GET(self):
        if urlparse(self.path).path!='/media':
            with CONFIG_LOCK:self.get_locked()
            return
        try:
            if not self.valid():return self.send({'error':'Host rejected'},403)
            q={k:v[0] for k,v in parse_qs(urlparse(self.path).query).items()}
            with CONFIG_LOCK:
                store=current();i=store.item(q['id']);p=store.cache/(i['id']+'.jpg') if q.get('thumb')=='1' else store.path(i)
                if not p.exists():p=store.path(i)
                cache=store.cache/'video-playback'
            if q.get('play')=='1' and q.get('thumb')!='1':p=playback_file(p,cache,prepare=self.command!='HEAD')
            return serve_media(self,p)
        except PreviewPending:
            self.send_response(202);self.send_header('Content-Length','0');self.send_header('Retry-After','2');self.end_headers()
        except Exception as e:self.send({'error':str(e)},400)
    def get_locked(self):
        try:
            if not self.valid():return self.send({'error':'Host rejected'},403)
            u=urlparse(self.path);q={k:v[0] for k,v in parse_qs(u.query).items()}
            if u.path=='/':return self.send((ROOT/'web/index.html').read_bytes(),ctype='text/html; charset=utf-8')
            if u.path=='/health':return self.send({'app':'offline-image-library'})
            if u.path=='/api/status':return self.send({'csrf':self.server.csrf,'folders':STORE.folders() if STORE else [],'referenceFolderId':'','path':str(STORE.root) if STORE else '', 'revision':STORE.revision if STORE else 0,'warning':STORE.error if STORE else ''})
            if u.path=='/api/revision':return self.send({'revision':STORE.revision if STORE else 0,'warning':STORE.error if STORE else '', 'libraryId':STORE.db['libraryId'] if STORE else ''})
            if u.path=='/api/library':return self.send(library())
            if u.path=='/api/references':return self.send(current().selected(q.get('task')))
            if u.path=='/api/browse':return self.send(browse_backend.browse(sys.modules[__name__],q))
            if u.path=='/api/item':return self.send(current().item(q['id']))
            return self.send({'error':'not found'},404)
        except PreviewPending:
         self.send_response(202);self.send_header('Content-Length','0');self.send_header('Retry-After','2');self.end_headers()
        except Exception as e:self.send({'error':str(e)},400)
    def do_POST(self):
        try:
            if not self.valid() or self.headers.get('Origin') not in (None,BASE) or self.headers.get('X-Library-Token')!=self.server.csrf:return self.send({'error':'请求来源无效'},403)
            n=int(self.headers.get('Content-Length',0))
            if not 0<n<=64*1024*1024:return self.send({'error':'请求内容过大'},413)
            a=json.loads(self.rfile.read(n))
            if self.path=='/api/configure':
                p=a.get('path') or picker()
                return self.send(configure(p) if p else {'cancelled':True})
            with CONFIG_LOCK:
                if self.path=='/api/select':return self.send(current().select(a.get('taskId'),a['ids']))
                if self.path=='/api/edit':return self.send(current().edit(a))
                if self.path=='/api/import-bundled':return self.send(import_bundle(current(),ROOT/'assets/image2.5参考',a.get('libraryPath','')))
                if self.path=='/api/edit-many':return self.send(current().edit_many(a))
                if self.path=='/api/save-analysis':return self.send(current().save_analysis(a))
            return self.send({'error':'not found'},404)
        except Exception as e:self.send({'error':str(e)},400)

def serve():
    global STORE
    STATE.mkdir(parents=True,exist_ok=True)
    if (STATE/'config.json').exists():
        try:STORE=Store(json.loads((STATE/'config.json').read_text(encoding='utf-8'))['path'])
        except Exception as e:print(str(e),file=sys.stderr)
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler);server.csrf=secrets.token_urlsafe(32)
    threading.Thread(target=monitor,daemon=True).start();server.serve_forever()

def ensure_server():
    def alive():
        with urlopen(BASE+'/health',timeout=2) as r:
            if json.load(r).get('app')!='offline-image-library':raise RuntimeError('离线图库端口被其他程序占用')
    try:alive();return BASE
    except OSError:pass
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'server.log').open('ab') as log:
        subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'serve'],stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    for _ in range(40):
        time.sleep(.15)
        try:alive();return BASE
        except OSError:pass
    raise RuntimeError('离线图库启动失败，请查看server.log')

def request(path,body=None):
    try:
        ensure_server();headers={}
        if body is not None:
            with urlopen(BASE+'/api/status',timeout=120) as r:headers['X-Library-Token']=json.load(r)['csrf']
            headers['Content-Type']='application/json'
        req=Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with urlopen(req,timeout=120) as r:return json.load(r)
    except HTTPError as error:
        message=f'HTTP {error.code}: {error.reason}'
        try:
            payload=json.load(error)
            if isinstance(payload,dict) and isinstance(payload.get('error'),str):message=payload['error']
        except (ValueError,OSError):pass
        finally:error.close()
        raise RuntimeError(message) from error

TOOLS=[
 {'name':'offline_library_open','description':'打开当前任务的离线图片与提示词管理库；仅返回右侧浏览器网址，不读取全库。','inputSchema':{'type':'object','properties':{'taskId':{'type':'string'}},'required':['taskId']}},
 {'name':'offline_library_selected','description':'读取当前任务选中图片的本地路径、完整提示词、标签，按用户要求分析或创作。','inputSchema':{'type':'object','properties':{'taskId':{'type':'string'}},'required':['taskId']}},
 {'name':'offline_library_configure','description':'用户明确指定本地图片文件夹后连接它；创建可见的图库资料目录。','inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}},
 {'name':'offline_library_info','description':'只读取当前已配置离线图库的名称与文件夹路径，供保存确认时说明目标；不扫描全库或更换目录。','inputSchema':{'type':'object','properties':{}}},
 {'name':'offline_library_item','description':'按itemId读取当前离线图库单张素材的最新资料，用于检查分析结果保存前的expected。','inputSchema':{'type':'object','properties':{'itemId':{'type':'string'}},'required':['itemId']}},
 {'name':'offline_library_save_analysis','description':'仅在实际图片反推完成且用户明确确认本次图片与离线图库后，一次保存名称、分类标签及完整原图反推提示词。库内原条目更新；外部本地原图仅复制导入。不得保存仅为生图改写的创作提示词。requestId在重试时保持不变；返回磁盘回读验证结果。','inputSchema':{'type':'object','properties':{
     'confirmed':{'type':'boolean','const':True},'requestId':{'type':'string','minLength':8,'maxLength':100},
     'libraryPath':{'type':'string','description':'用户确认入库时由 offline_library_info 返回的目标图库绝对路径'},
     'name':{'type':'string','minLength':1},'tags':{'type':'array','minItems':1,'items':{'type':'string','minLength':1}},'prompt':{'type':'string','minLength':1},
     'itemId':{'type':'string','minLength':1},'imagePath':{'type':'string','minLength':1},
     'expected':{'type':'object','properties':{'name':{'type':'string'},'tags':{'type':'array','items':{'type':'string'}},'annotation':{'type':'string'},'path':{'type':'string'},'hash':{'type':'string'}},'required':['name','tags','annotation','path','hash']}},
     'required':['confirmed','requestId','libraryPath','name','tags','prompt'],
     'oneOf':[{'required':['itemId','expected'],'not':{'required':['imagePath']}},{'required':['imagePath'],'not':{'anyOf':[{'required':['itemId']},{'required':['expected']}]}}]}},
 {'name':'offline_library_edit','description':'用户授权后保存指定字段；expected必须是此前读取的原值。','inputSchema':{'type':'object','properties':{'id':{'type':'string'},'field':{'type':'string','enum':['name','tags','annotation']},'value':{},'expected':{}},'required':['id','field','value','expected']}}
]
def call(name,a):
    if name=='offline_library_open':
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',a.get('taskId','')):raise ValueError('必须传当前真实任务ID')
        return {'url':ensure_server()+'/?'+urlencode({'task':a['taskId']})}
    if name=='offline_library_selected':return request('/api/references?'+urlencode({'task':a['taskId']}))
    if name=='offline_library_configure':return request('/api/configure',a)
    if name=='offline_library_edit':return request('/api/edit',a)
    if name=='offline_library_item':return request('/api/item?'+urlencode({'id':a['itemId']}))
    if name=='offline_library_info':return request('/api/library')
    if name=='offline_library_save_analysis':
        # Reject missing/false consent before even connecting to or starting the service.
        from storage import analysis_values
        analysis_values(a)
        return request('/api/save-analysis',a)
    raise ValueError('未知工具')
def mcp():
    for line in sys.stdin:
        try:
            m=json.loads(line);rid=m.get('id');method=m.get('method');p=m.get('params',{})
            if rid is None:continue
            if method=='initialize':r={'protocolVersion':p.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'offline-image-library','version':'1.0.0'}}
            elif method=='tools/list':r={'tools':TOOLS}
            elif method=='ping':r={}
            elif method=='tools/call':
                try:r={'content':[{'type':'text','text':json.dumps(call(p['name'],p.get('arguments',{})),ensure_ascii=False)}]}
                except Exception as e:r={'content':[{'type':'text','text':str(e)}],'isError':True}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
            print(json.dumps({'jsonrpc':'2.0','id':rid,'result':r}),flush=True)
        except Exception as e:print(str(e),file=sys.stderr)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['serve','mcp','open']);a=p.parse_args()
    if a.mode=='serve':serve()
    elif a.mode=='mcp':mcp()
    else:print(ensure_server())
