"""Anonymous TeraBox share resolver; no login cookie required."""
from __future__ import annotations
import asyncio, json, logging, re
from urllib.parse import parse_qs, urlparse
import aiohttp

log = logging.getLogger(__name__)

TERABOX_HOSTS = {
    "terabox.com","www.terabox.com","1024terabox.com","www.1024terabox.com",
    "teraboxapp.com","www.teraboxapp.com","terabox.app","www.terabox.app",
    "1024tera.com","www.1024tera.com","teraboxlink.com","www.teraboxlink.com",
    "terasharelink.com","www.terasharelink.com","terasharefile.com","www.terasharefile.com",
    "terafileshare.com","www.terafileshare.com","teraboxshare.com","www.teraboxshare.com",
}
APP_ID="250528"
UA=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT=aiohttp.ClientTimeout(total=20, connect=8)

def is_terabox_url(value:str)->bool:
    try: host=(urlparse(value.strip()).hostname or "").lower()
    except ValueError: return False
    return host in TERABOX_HOSTS or host.endswith(".terabox.com")

def extract_surl(value:str)->str:
    p=urlparse(value.strip()); q=parse_qs(p.query)
    if q.get("surl"):
        surl=q["surl"][0].strip()
    else:
        m=re.search(r"/s/([A-Za-z0-9_-]+)",p.path)
        if not m: raise ValueError("Could not extract a TeraBox share code.")
        surl=m.group(1)
        if surl.startswith("1") and len(surl)>1: surl=surl[1:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,}",surl):
        raise ValueError("Invalid TeraBox share code.")
    return surl

def _headers(referer:str)->dict[str,str]:
    return {"User-Agent":UA,"Accept":"application/json, text/plain, */*",
            "Accept-Language":"en-US,en;q=0.8","Referer":referer,
            "X-Requested-With":"XMLHttpRequest"}

async def _get_json(session,url,params,referer):
    async with session.get(url,params=params,headers=_headers(referer),allow_redirects=True) as r:
        body=await r.text()
        if r.status>=400: raise RuntimeError(f"TeraBox HTTP {r.status}: {body[:160]}")
        try: data=json.loads(body)
        except json.JSONDecodeError as e: raise RuntimeError("TeraBox returned non-JSON.") from e
        if not isinstance(data,dict): raise RuntimeError("Unexpected TeraBox response.")
        return data

def _errno(data):
    try: return int(data.get("errno") or data.get("code") or 0)
    except (TypeError,ValueError): return 0

def _normalize(row):
    thumbs=row.get("thumbs") if isinstance(row.get("thumbs"),dict) else {}
    try: size=int(row.get("size") or 0)
    except (TypeError,ValueError): size=0
    return {"file_name":str(row.get("server_filename") or row.get("filename") or "").strip(),
            "size":size,"fs_id":str(row.get("fs_id") or ""),
            "path":str(row.get("path") or ""),
            "is_dir":str(row.get("isdir") or "0") in {"1","true","True"},
            "direct_url":str(row.get("dlink") or "").strip(),
            "thumbnail":str(thumbs.get("url3") or "")}

async def _list_scope(session,origin,surl,path="/"):
    params={"app_id":APP_ID,"web":"1","channel":"dubox","clienttype":"0",
            "shorturl":surl,"root":"1"}
    if path!="/": params["dir"]=path
    data=await _get_json(session,f"{origin}/share/list",params,f"{origin}/")
    errno=_errno(data)
    if errno:
        raise RuntimeError(f"TeraBox share API error {errno}: {data.get('errmsg') or data.get('error') or 'unknown'}")
    rows=data.get("list") if isinstance(data.get("list"),list) else []
    return [_normalize(x) for x in rows if isinstance(x,dict)]

async def _origins(session,share_url):
    out=[]
    p=urlparse(share_url)
    if p.scheme and p.netloc: out.append(f"{p.scheme}://{p.netloc}")
    try:
        async with session.get(share_url,headers={"User-Agent":UA,"Accept":"text/html,*/*"},allow_redirects=True) as r:
            u=r.url
            if u.scheme and u.host: out.insert(0,f"{u.scheme}://{u.host}")
    except (aiohttp.ClientError,asyncio.TimeoutError): pass
    out += ["https://www.terabox.com","https://www.1024terabox.com",
            "https://www.terabox.app","https://www.1024tera.com"]
    return list(dict.fromkeys(out))

async def resolve_terabox_url(url:str)->dict:
    if not is_terabox_url(url): raise ValueError("Unsupported TeraBox URL.")
    surl=extract_surl(url)
    connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector,timeout=TIMEOUT,cookie_jar=aiohttp.CookieJar()) as session:
        last=None
        for origin in await _origins(session,url):
            try:
                rows=await _list_scope(session,origin,surl)
                files=[x for x in rows if not x["is_dir"]]
                for folder in [x for x in rows if x["is_dir"]][:10]:
                    try:
                        files.extend(x for x in await _list_scope(session,origin,surl,folder["path"]) if not x["is_dir"])
                    except (aiohttp.ClientError,asyncio.TimeoutError,RuntimeError) as e:
                        log.info("folder listing failed: %s",e)
                if not files: raise RuntimeError("The TeraBox share contains no files.")
                usable=[x for x in files[:50] if x["direct_url"]]
                if not usable:
                    raise RuntimeError("TeraBox returned metadata but no anonymous direct download URL.")
                row=usable[0]
                return {"surl":surl,"file_name":row["file_name"],"size":row["size"],
                        "fs_id":row["fs_id"],"direct_url":row["direct_url"],
                        "files":files[:50],"thumbnail":row["thumbnail"]}
            except (aiohttp.ClientError,asyncio.TimeoutError,RuntimeError) as e:
                last=e; log.info("listing failed via %s: %s",origin,e)
        raise RuntimeError(f"Could not read the TeraBox share. Last error: {last}")
