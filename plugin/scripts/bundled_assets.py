"""Portable reference assets. Import only through the current library service."""
import copy, hashlib, json, re, shutil
from pathlib import Path

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def validate_bundle(bundle):
    bundle=Path(bundle).resolve()
    manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!=1 or manifest.get('folder')!='image2.5参考' or not isinstance(manifest.get('items'),list):raise ValueError('随附素材目录不完整，请下载完整仓库安装包')
    names=set()
    for i in manifest['items']:
        name=i.get('file','');p=bundle/name
        if not name or name in {'.','..'} or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or name.endswith(('.', ' ')) or name.casefold() in names:raise ValueError('随附素材文件名无效或重复')
        names.add(name.casefold())
        if p.is_symlink() or p.resolve().parent!=bundle or not p.is_file() or p.stat().st_size!=i.get('size') or sha(p)!=i.get('sha256'):raise ValueError('随附素材校验失败：'+name)
        if not isinstance(i.get('name'),str) or not isinstance(i.get('annotation'),str) or not isinstance(i.get('tags'),list) or not all(isinstance(t,str) for t in i['tags']):raise ValueError('随附提示词或标签无效')
    return manifest

def import_bundle(store,bundle,library_path):
    if not isinstance(library_path,str) or not Path(library_path).is_absolute() or Path(library_path).resolve()!=store.root:raise ValueError('目标图库已切换，请重新选择')
    manifest=validate_bundle(bundle);bundle=Path(bundle).resolve()
    with store.lock:
        store.scan();store.scan()
        before=copy.deepcopy(store.db)
        ledger=store.db.setdefault('bundledReferences',{})
        live={r['hash'] for r in store.db['items'].values() if not r.get('missingSince')}
        jobs=[];skipped=0;conflicts=0
        target=store.root/'image2.5参考'
        if target.is_symlink() or getattr(target,'is_junction',lambda:False)() or target.resolve().parent!=store.root:raise ValueError('参考目录不能指向图库之外')
        used=set()
        for i in manifest['items']:
            key=i['file']+'|'+i['sha256'];record=ledger.get(key)
            if record and record.get('done'):skipped+=1;continue
            if not record:
                if i['sha256'] in live:skipped+=1;continue
                p=target/i['file'];n=0
                while p.exists() or p.name.casefold() in used:
                    n+=1;p=target/(Path(i['file']).stem+'__'+i['sha256'][:8]+('_'+str(n) if n>1 else '')+Path(i['file']).suffix)
                record={'path':p.relative_to(store.root).as_posix(),'done':False};ledger[key]=record
            p=(store.root/record['path']).resolve()
            if p.parent!=target.resolve():raise ValueError('随附素材导入记录路径无效')
            used.add(p.name.casefold());jobs.append((i,record,p))
        # Persist reservations before copying: interrupted imports can resume without overwriting edits.
        try:store.save()
        except Exception:store.db=before;raise
        if jobs:target.mkdir(exist_ok=True)
        for i,record,p in jobs:
            if p.exists():
                if sha(p)!=i['sha256']:raise ValueError('导入预留文件已变化，未覆盖：'+p.name)
            else:
                with (bundle/i['file']).open('rb') as source,p.open('xb') as dest:shutil.copyfileobj(source,dest)
            if sha(p)!=i['sha256']:raise RuntimeError('原图复制校验失败：'+p.name)
        store.scan();store.scan()
        by_path={r['path']:r for r in store.db['items'].values() if not r.get('missingSince')}
        before=copy.deepcopy(store.db);imported=0
        try:
            for i,record,p in jobs:
                row=by_path.get(record['path'])
                if not row or row['hash']!=i['sha256']:raise RuntimeError('图片索引未完成：'+p.name)
                desired={'name':i['name'],'tags':i['tags'],'annotation':i['annotation']}
                defaults={'name':p.stem,'tags':[],'annotation':''}
                if any(row.get(k) not in (defaults[k],desired[k]) for k in desired):conflicts+=1
                else:row.update(desired);imported+=1
                record['done']=True
            store.save()
        except Exception:store.db=before;raise
        disk=json.loads(store.file.read_text(encoding='utf-8'))
        if disk!=store.db:raise RuntimeError('随附素材资料回读不一致')
        return {'verified':True,'imported':imported,'skipped':skipped,'preservedEdits':conflicts,'folder':'image2.5参考'}
