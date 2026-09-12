"""Folder-owned metadata; one service is the only writer. No Eagle dependency."""
import copy, hashlib, json, os, re, shutil, threading, time, uuid
from collections import Counter
from pathlib import Path
from PIL import Image, ImageOps

FORMATS={'.jpg','.jpeg','.png','.webp','.gif','.bmp','.tif','.tiff','.ico','.avif'}
DATA='图库资料'

def atomic(path,value,backup=True):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    if backup and path.exists():
        dest=path.parent/'元数据备份'/(path.name+'.previous')
        dest.parent.mkdir(exist_ok=True);shutil.copy2(path,dest)
    os.replace(tmp,path)

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

class Store:
    def __init__(self,root,grace=60):
        self.root=Path(root).expanduser().resolve()
        if not self.root.is_dir():raise ValueError('图库文件夹不存在或硬盘未连接')
        self.lock=threading.RLock();self.grace=grace;self.pending={};self.error='';self.revision=0
        self.meta=self.root/DATA;self.file=self.meta/'素材信息.json'
        if self.meta.exists() and not self.file.exists():raise ValueError('已有“图库资料”文件夹但缺少索引，请换用空目录或恢复索引，避免覆盖已有资料')
        if self.file.exists():
            try:self.db=json.loads(self.file.read_text(encoding='utf-8'))
            except (ValueError,OSError) as e:raise ValueError('素材信息无法读取，请从图库资料/元数据备份恢复；未覆盖原文件') from e
            if self.db.get('format')!=1 or not isinstance(self.db.get('items'),dict):raise ValueError('不支持的素材信息格式')
        else:
            self.db={'format':1,'libraryId':uuid.uuid4().hex,'items':{}}
            atomic(self.file,self.db,False)
        self.cache=self.meta/'缩略图缓存';self.cache.mkdir(exist_ok=True)
        self.sel=self.meta/'任务选择';self.sel.mkdir(exist_ok=True)

    def save(self):
        atomic(self.file,self.db);self.revision+=1

    def path(self,row):
        p=(self.root/row['path']).resolve()
        if not p.is_relative_to(self.root) or p.is_relative_to(self.meta):raise ValueError('图片路径不在图库内')
        return p

    def scan(self):
        with self.lock:
            before=copy.deepcopy(self.db);pending=self.pending.copy();revision=self.revision
            try:self._scan()
            except Exception:
                if self.revision==revision:self.db=before;self.pending=pending
                raise

    def _scan(self):
        with self.lock:
            if not self.root.is_dir() or not self.file.is_file():
                self.error='图库或元数据目录暂时不可访问，已暂停扫描和删除清理';return
            now=time.time();found={}
            try:
                def fail(e):raise e
                for folder,dirs,files in os.walk(self.root,onerror=fail,followlinks=False):
                    dirs[:]=[d for d in dirs if not (Path(folder)/d).is_symlink() and not getattr(Path(folder)/d,'is_junction',lambda:False)() and (Path(folder)/d)!=self.meta]
                    for name in files:
                        p=Path(folder)/name
                        if p.suffix.lower() not in FORMATS or p.is_symlink():continue
                        st=p.stat();found[p.relative_to(self.root).as_posix()]=(st.st_size,st.st_mtime_ns)
            except OSError:
                self.error='部分目录暂时不可读取，本轮不清理元数据';return
            self.error='';changed=False;rows=self.db['items']
            remove_thumbs=[]
            by_path={}
            for r in rows.values():
                previous=by_path.get(r['path'])
                if previous is None or (previous.get('missingSince') and not r.get('missingSince')):
                    by_path[r['path']]=r
            # Resolve identities against the whole stable scan, never directory iteration order.
            observed={};ready={}
            for rel,sig in found.items():
                row=by_path.get(rel)
                if row and row.get('signature')==list(sig):observed[rel]=row.get('hash');continue
                if self.pending.get(rel)!=sig:self.pending[rel]=sig;observed[rel]=None;continue
                try:
                    sha=digest(self.root/rel);st=(self.root/rel).stat()
                    if (st.st_size,st.st_mtime_ns)!=sig:observed[rel]=None;continue
                    observed[rel]=sha;ready[rel]=sha
                except OSError:observed[rel]=None
            live_counts=Counter(h for h in observed.values() if h)
            old_counts=Counter(r.get('hash') for r in rows.values())
            def occupies(r):return r['path'] in found and (observed.get(r['path']) is None or observed.get(r['path'])==r.get('hash'))
            for rel,sig in found.items():
                row=by_path.get(rel)
                if row and row.get('signature')==list(sig):
                    if row.pop('missingSince',None) is not None:changed=True
                    continue
                if rel not in ready:continue
                p=self.root/rel
                try:
                    sha=ready[rel]
                    with Image.open(p) as raw:
                        picture=ImageOps.exif_transpose(raw).convert('RGB');w,h=picture.size
                        picture.thumbnail((600,600));small=picture.copy();small.thumbnail((80,80))
                        quant=small.quantize(colors=12);palette=quant.getpalette();colors=quant.getcolors() or []
                        palettes=[{'color':palette[k*3:k*3+3],'ratio':n/(small.width*small.height)*100} for n,k in colors]
                    if (p.stat().st_size,p.stat().st_mtime_ns)!=sig:continue
                except (OSError,ValueError):continue
                if row and row.get('hash') and row['hash']!=sha:row=None
                if row is None:
                    candidates=[r for r in rows.values() if not occupies(r) and r.get('hash')==sha]
                    if len(candidates)==1 and old_counts[sha]==1 and live_counts[sha]==1:
                        row=candidates[0]
                    else:
                        row={'id':uuid.uuid4().hex,'tags':[],'annotation':'','url':''}
                        rows[row['id']]=row
                        if candidates:
                            for c in candidates:c['ambiguous']=True
                            self.error='存在相同内容的多张图片，旧资料已保留，未自动关联'
                row.update(path=rel,name=p.stem,ext=p.suffix[1:],width=w,height=h,size=sig[0],signature=list(sig),hash=sha,palettes=palettes,modificationTime=p.stat().st_mtime*1000)
                row.pop('missingSince',None)
                tmp=self.cache/(row['id']+'.tmp');picture.save(tmp,format='JPEG',quality=85);os.replace(tmp,self.cache/(row['id']+'.jpg'))
                self.pending.pop(rel,None);changed=True
            for id,row in list(rows.items()):
                if occupies(row):continue
                if 'missingSince' not in row:row['missingSince']=now;changed=True
                elif now-row['missingSince']>=self.grace and not row.get('ambiguous'):
                    # A same-size file still being copied may be the moved original.
                    if any(sig[0]==row.get('size') for rel,sig in found.items() if rel in self.pending):continue
                    del rows[id];remove_thumbs.append(self.cache/(id+'.jpg'));changed=True
            if any(r.get('ambiguous') and r.get('missingSince') for r in rows.values()):self.error='存在重名内容或重复图片的关联歧义，旧资料已保留，未自动关联'
            self.pending={k:v for k,v in self.pending.items() if k in found}
            if changed:
                self.save()
                for thumb in remove_thumbs:thumb.unlink(missing_ok=True)
                for p in self.sel.glob('*.json'):
                    try:
                        selected=json.loads(p.read_text(encoding='utf-8'));ids=[x for x in selected['ids'] if x in rows]
                        if ids!=selected['ids']:atomic(p,{'ids':ids},False)
                    except (OSError,ValueError,KeyError):pass

    def brief(self,row):
        r=dict(row);parent=Path(r['path']).parent.as_posix();r['folders']=[] if parent=='.' else [parent]
        r['localImage']=str(self.path(row));return r

    def items(self):
        with self.lock:return [self.brief(r) for r in self.db['items'].values() if not r.get('missingSince')]

    def item(self,id):
        with self.lock:
            r=self.db['items'].get(id)
            if r is None or r.get('missingSince'):raise ValueError('图片已删除或暂时不可用')
            if not self.path(r).is_file():raise ValueError('图片文件暂时不可用，等待扫描确认')
            return self.brief(r)

    def folders(self):
        paths=set()
        for r in self.items():
            p=Path(r['path']).parent
            while p.as_posix()!='.':paths.add(p.as_posix());p=p.parent
        def tree(parent='.'):
            return [{'id':p,'name':Path(p).name,'children':tree(p)} for p in sorted(paths) if Path(p).parent.as_posix()==parent]
        return tree()

    def selection_path(self,task):
        if not isinstance(task,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',task):raise ValueError('缺少当前任务ID，请在当前对话打开离线图库')
        return self.sel/(task+'.json')

    def selected(self,task):
        with self.lock:
            p=self.selection_path(task);ids=json.loads(p.read_text(encoding='utf-8'))['ids'] if p.exists() else []
            out=[];errors=[]
            for id in ids:
                try:out.append(self.item(id))
                except ValueError as e:errors.append({'id':id,'error':str(e)})
            return {'taskId':task,'items':out,'errors':errors}

    def select(self,task,ids):
        with self.lock:
            p=self.selection_path(task)
            if not isinstance(ids,list) or not all(isinstance(x,str) for x in ids) or len(ids)!=len(set(ids)):raise ValueError('选择格式不正确')
            for id in ids:self.item(id)
            atomic(p,{'ids':ids},False) if ids else p.unlink(missing_ok=True)
            return {'saved':True,'ids':ids}

    def edit(self,body):
        with self.lock:
            r=self.item(body['id']);field=body['field'];value=body['value']
            if field not in ('name','tags','annotation'):raise ValueError('不支持的字段')
            if r.get(field)!=body.get('expected'):raise ValueError('资料已修改，请重新打开后编辑')
            if field=='tags':
                if not isinstance(value,list) or not all(isinstance(t,str) and t.strip() for t in value):raise ValueError('标签格式不正确')
                value=list(dict.fromkeys(t.strip() for t in value))
            elif not isinstance(value,str):raise ValueError('内容必须是文字')
            oldpath=None;newpath=None
            if field=='name':
                value=value.strip()
                if not value or len(value)>180 or re.search(r'[<>:"/\\|?*\x00-\x1f]',value) or value.endswith(('.', ' ')) or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',value):raise ValueError('名称不是有效文件名')
                oldpath=self.path(r);newpath=oldpath.with_name(value+'.'+r['ext'])
                if newpath!=oldpath and newpath.exists():raise ValueError('同名文件已存在，不覆盖')
                oldpath.rename(newpath);r['path']=newpath.relative_to(self.root).as_posix()
            before=self.db['items'][r['id']].copy();self.db['items'][r['id']].update({field:value,'path':r['path']})
            try:self.save()
            except Exception:
                self.db['items'][r['id']]=before
                if oldpath and newpath and newpath!=oldpath:newpath.rename(oldpath)
                raise
            return self.item(r['id'])
