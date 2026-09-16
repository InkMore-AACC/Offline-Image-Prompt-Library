"""Folder-owned metadata; one service is the only writer. No Eagle dependency."""
import copy, hashlib, json, os, re, shutil, threading, time, uuid
from collections import Counter
from pathlib import Path
from PIL import Image, ImageOps

from video_media import VIDEO_FORMATS, is_video, video_picture

FORMATS=VIDEO_FORMATS|{'.jpg','.jpeg','.png','.webp','.gif','.bmp','.tif','.tiff','.ico','.avif'}
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

def analysis_values(body):
    if body.get('confirmed') is not True:raise ValueError('请先获得用户对本次图片和目标图库的明确保存确认')
    if not isinstance(body.get('libraryPath'),str) or not Path(body['libraryPath']).is_absolute():raise ValueError('请传入用户确认的目标图库绝对路径 libraryPath')
    request_id=body.get('requestId')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request_id):raise ValueError('requestId须为8至100位唯一请求ID；重试请保持不变')
    if bool(body.get('itemId'))==bool(body.get('imagePath')):raise ValueError('itemId与imagePath必须且只能提供一个')
    name=body.get('name');tags=body.get('tags');prompt=body.get('prompt')
    if not isinstance(name,str):raise ValueError('名称必须是文字')
    name=name.strip()
    if not name or len(name)>180 or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or name.endswith(('.', ' ')) or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',name):raise ValueError('名称不是有效文件名')
    if not isinstance(tags,list) or not tags or not all(isinstance(t,str) and t.strip() for t in tags):raise ValueError('请提供非空分类标签列表')
    if not isinstance(prompt,str) or not prompt.strip():raise ValueError('请提供已完成的图片反推提示词')
    return request_id,{'name':name,'tags':list(dict.fromkeys(t.strip() for t in tags)),'annotation':prompt}

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()

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
                    media={}
                    if is_video(p):
                        picture,media=video_picture(p);w,h=media['width'],media['height']
                    else:
                        with Image.open(p) as raw:picture=ImageOps.exif_transpose(raw).convert('RGB')
                        w,h=picture.size;media={'mediaType':'image'}
                    picture.thumbnail((600,600));small=picture.copy();small.thumbnail((80,80))
                    quant=small.quantize(colors=12);palette=quant.getpalette();colors=quant.getcolors() or []
                    palettes=[{'color':palette[k*3:k*3+3],'ratio':n/(small.width*small.height)*100} for n,k in colors]
                    if (p.stat().st_size,p.stat().st_mtime_ns)!=sig:continue
                except (OSError,ValueError) as e:
                    if is_video(p):self.error=str(e)
                    continue
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
                row.update(media)
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

    def edit_many(self,body):
        """Atomic prompt/tag edits for an explicitly targeted import batch."""
        with self.lock:
            if Path(body.get('libraryPath','')).resolve()!=self.root:raise ValueError('目标图库已切换')
            entries=body.get('items')
            if not isinstance(entries,list) or not 1<=len(entries)<=100:raise ValueError('每批须为1至100条')
            ids=[e['id'] for e in entries]
            if len(set(ids))!=len(ids):raise ValueError('重复素材ID')
            updates=[]
            for e in entries:
                row=self.item(e['id']);expected=e.get('expected',{})
                if any(row.get(k)!=expected.get(k) or k not in expected for k in ('name','tags','annotation','path','hash')):raise ValueError('资料已修改，请重新读取')
                if not isinstance(e.get('annotation'),str) or not isinstance(e.get('tags'),list) or not all(isinstance(t,str) and t.strip() for t in e['tags']):raise ValueError('提示词和标签格式无效')
                updates.append((e['id'],{'annotation':e['annotation'],'tags':list(dict.fromkeys(t.strip() for t in e['tags']))}))
            before=copy.deepcopy(self.db)
            try:
                for id,value in updates:self.db['items'][id].update(value)
                self.save()
            except Exception:self.db=before;raise
            disk=json.loads(self.file.read_text(encoding='utf-8'))['items']
            if any(any(disk[id].get(k)!=v for k,v in value.items()) for id,value in updates):raise RuntimeError('磁盘回读不一致')
            return {'saved':True,'verified':True,'count':len(updates)}

    def brief(self,row):
        r=dict(row);parent=Path(r['path']).parent.as_posix();r['folders']=[] if parent=='.' else [parent]
        r['localVideo' if is_video(self.path(row)) else 'localImage']=str(self.path(row));return r

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

    def _analysis_result(self,receipt,replayed=False):
        # Read the committed file, not just the in-memory row, before claiming success.
        disk=json.loads(self.file.read_text(encoding='utf-8'))
        row=disk['items'].get(receipt['itemId'])
        if disk.get('libraryId')!=self.db['libraryId'] or row is None or row.get('missingSince'):
            raise ValueError('保存后的资料不可读取；未确认保存成功，请保持同一requestId重试')
        actual={k:row.get(k) for k in ('name','tags','annotation','path','hash')}
        if fingerprint(actual)!=receipt['resultHash']:
            raise ValueError('本请求已保存，但资料随后有变化；未重复写入，请重新读取')
        if digest(self.path(row))!=row['hash']:raise ValueError('图片内容已变化，未确认保存成功')
        return {'saved':True,'verified':True,'replayed':replayed,'imported':receipt['imported'],
                'item':self.brief(row),'libraryPath':str(self.root),'metadataPath':str(self.file)}

    def save_analysis(self,body):
        request_id,values=analysis_values(body)
        body_hash=fingerprint(body)
        with self.lock:
            if Path(body['libraryPath']).resolve()!=self.root:raise ValueError('当前图库与用户确认的目标不同，未写入；请重新确认目标图库')
            if not self.root.is_dir() or not self.file.is_file():raise ValueError('图库暂时不可访问，未保存')
            receipt=self.db.get('analysisSaves',{}).get(request_id)
            if receipt:
                if receipt['bodyHash']!=body_hash:raise ValueError('requestId已用于不同内容，请勿改变重试参数')
                return self._analysis_result(receipt,True)
            before=copy.deepcopy(self.db);oldpath=None;newpath=None;created=False;renamed=False;thumb=None
            try:
                if body.get('itemId'):
                    row=self.item(body['itemId']);expected=body.get('expected')
                    if ('.'+row['ext'].lower()) in VIDEO_FORMATS:raise ValueError('图片反推保存不接受视频；视频提示词请使用资料编辑')
                    fields=('name','tags','annotation','path','hash')
                    if not isinstance(expected,dict) or any(k not in expected or expected[k]!=row.get(k) for k in fields):
                        raise ValueError('资料或图片已变化；expected须包含最新读取的name、tags、annotation、path、hash')
                    oldpath=self.path(row)
                    if digest(oldpath)!=row['hash']:raise ValueError('图片内容已变化，请重新读取并分析')
                    newpath=oldpath.with_name(values['name']+'.'+row['ext'])
                    if newpath!=oldpath:
                        if newpath.exists():raise ValueError('同名文件已存在，不覆盖')
                        oldpath.rename(newpath);renamed=True
                    row['path']=newpath.relative_to(self.root).as_posix()
                else:
                    if body.get('expected') is not None:raise ValueError('外部图片导入不接受expected；更新库内素材请使用itemId')
                    source=Path(body['imagePath']).expanduser()
                    if not source.is_absolute() or source.is_symlink():raise ValueError('imagePath须为外部原图的绝对本地路径，不接受符号链接')
                    source=source.resolve()
                    if source.is_relative_to(self.root):raise ValueError('图片已在当前图库内，请读取素材后使用itemId更新')
                    if not source.is_file() or source.suffix.lower() not in (FORMATS-VIDEO_FORMATS):raise ValueError('外部图片不存在或格式不支持')
                    sig=source.stat();sha=digest(source)
                    duplicates=[r['id'] for r in self.db['items'].values() if r.get('hash')==sha]
                    if duplicates:raise ValueError('相同图片已有资料，请读取并确认更新itemId：'+','.join(duplicates))
                    # Also catch a manually copied image before the background scan indexes it.
                    for folder,dirs,files in os.walk(self.root,followlinks=False):
                        dirs[:]=[d for d in dirs if (Path(folder)/d)!=self.meta and not (Path(folder)/d).is_symlink() and not getattr(Path(folder)/d,'is_junction',lambda:False)()]
                        for filename in files:
                            p=Path(folder)/filename
                            if not p.is_symlink() and p.suffix.lower() in FORMATS and p.stat().st_size==sig.st_size and digest(p)==sha:
                                raise ValueError('图库中已有相同图片，等待扫描后用itemId更新：'+str(p))
                    newpath=self.root/(values['name']+source.suffix.lower())
                    with newpath.open('xb') as dest:
                        created=True
                        with source.open('rb') as src:shutil.copyfileobj(src,dest)
                        dest.flush();os.fsync(dest.fileno())
                    after=source.stat()
                    if (sig.st_size,sig.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or digest(newpath)!=sha:
                        raise ValueError('原图在复制期间发生变化，未导入')
                    with Image.open(newpath) as raw:
                        picture=ImageOps.exif_transpose(raw).convert('RGB');w,h=picture.size
                        picture.thumbnail((600,600));small=picture.copy();small.thumbnail((80,80))
                        quant=small.quantize(colors=12);palette=quant.getpalette()
                        palettes=[{'color':palette[k*3:k*3+3],'ratio':n/(small.width*small.height)*100} for n,k in quant.getcolors() or []]
                    st=newpath.stat()
                    row={'id':uuid.uuid4().hex,'path':newpath.relative_to(self.root).as_posix(),'ext':newpath.suffix[1:],
                         'width':w,'height':h,'size':st.st_size,'signature':[st.st_size,st.st_mtime_ns],
                         'hash':sha,'palettes':palettes,'modificationTime':st.st_mtime*1000,'url':''}
                    thumb=self.cache/(row['id']+'.jpg');picture.save(thumb,format='JPEG',quality=85)
                row.update(values)
                # UI-only fields from item() do not belong in the persistent index.
                row.pop('folders',None);row.pop('localImage',None)
                self.db['items'][row['id']]=row
                receipt={'bodyHash':body_hash,'itemId':row['id'],'imported':created,
                         'resultHash':fingerprint({k:row.get(k) for k in ('name','tags','annotation','path','hash')})}
                self.db.setdefault('analysisSaves',{})[request_id]=receipt
                self.save()
            except Exception:
                self.db=before
                if thumb:thumb.unlink(missing_ok=True)
                if created and newpath:newpath.unlink(missing_ok=True)
                elif renamed and newpath.exists():
                    if oldpath.exists():raise RuntimeError('保存失败且原路径已被占用，请保留两份文件后检查')
                    newpath.rename(oldpath)
                raise
            # Once committed, verification errors must not undo or repeat the saved write.
            return self._analysis_result(receipt)
