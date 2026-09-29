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


async def _share_page_tokens(session, share_url):
    """Fetch the public share page and extract short-lived request tokens."""
    async with session.get(
        share_url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
        allow_redirects=True,
    ) as r:
        html = await r.text()
        final_url = str(r.url)
        if r.status >= 400:
            raise RuntimeError(f"TeraBox share page HTTP {r.status}")

    js_token = ""
    for pattern in (
        r'window\.jsToken\s*=\s*["\']([^"\']+)',
        r'jsToken\s*[:=]\s*["\']([^"\']+)',
        r'fn%28%22([^%]+)%22%29',
        r'fn\(\x22([^\x22]+)\x22\)',
    ):
        m = re.search(pattern, html)
        if m:
            js_token = m.group(1)
            break

    dp_logid = ""
    for pattern in (r'dp-logid[=:]["\']?([0-9]+)', r'dp-logid=([0-9]+)'):
        m = re.search(pattern, html)
        if m:
            dp_logid = m.group(1)
            break

    return final_url, js_token, dp_logid


def _first_list(data):
    candidates = [
        data.get("list"),
        data.get("data", {}).get("list") if isinstance(data.get("data"), dict) else None,
    ]
    return next((value for value in candidates if isinstance(value, list)), [])


async def _resolve_via_shorturlinfo(session, origin, share_url, surl):
    """Resolve a share through TeraBox's public share-page token flow."""
    page_url, js_token, dp_logid = await _share_page_tokens(session, share_url)
    if not js_token:
        raise RuntimeError("TeraBox share page did not expose jsToken.")

    referer = page_url or f"{origin}/"
    last = None

    for shorturl in (f"1{surl}", surl):
        params = {
            "app_id": APP_ID,
            "shorturl": shorturl,
            "root": "1",
            "jsToken": js_token,
        }
        if dp_logid:
            params["dp-logid"] = dp_logid

        try:
            data = await _get_json(
                session, f"{origin}/api/shorturlinfo", params, referer
            )
            errno = _errno(data)
            if errno:
                last = RuntimeError(
                    f"TeraBox shorturlinfo error {errno}: "
                    f"{data.get('errmsg') or data.get('show_msg') or 'unknown'}"
                )
                continue

            rows = [_normalize(x) for x in _first_list(data) if isinstance(x, dict)]
            if not rows:
                last = RuntimeError("TeraBox shorturlinfo returned no files.")
                continue

            meta = data.get("data") if isinstance(data.get("data"), dict) else data
            share_id = str(meta.get("shareid") or meta.get("share_id") or "")
            uk = str(meta.get("uk") or "")
            sign = str(meta.get("sign") or "")
            timestamp = str(meta.get("timestamp") or "")

            usable = [x for x in rows if x["direct_url"]]
            if usable:
                return usable[0], rows

            row = next((x for x in rows if not x["is_dir"] and x["fs_id"]), None)
            if not row:
                last = RuntimeError("TeraBox metadata contains no downloadable file.")
                continue

            if not (share_id and uk and sign and timestamp):
                last = RuntimeError(
                    "TeraBox metadata lacks shareid/uk/sign/timestamp for download."
                )
                continue

            download_params = {
                "app_id": APP_ID,
                "web": "1",
                "channel": "dubox",
                "clienttype": "0",
                "jsToken": js_token,
                "shareid": share_id,
                "sign": sign,
                "timestamp": timestamp,
            }
            if dp_logid:
                download_params["dp-logid"] = dp_logid

            form = {
                "product": "share",
                "nozip": "0",
                "fid_list": json.dumps([int(row["fs_id"])]),
                "uk": uk,
                "primaryid": share_id,
            }

            async with session.post(
                f"{origin}/share/download",
                params=download_params,
                data=form,
                headers=_headers(referer),
                allow_redirects=False,
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(
                        f"TeraBox share/download HTTP {response.status}: {body[:160]}"
                    )
                try:
                    download_data = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "TeraBox share/download returned non-JSON."
                    ) from exc

            errno = _errno(download_data)
            if errno:
                last = RuntimeError(
                    f"TeraBox share/download error {errno}: "
                    f"{download_data.get('errmsg') or download_data.get('show_msg') or 'unknown'}"
                )
                continue

            dlink = str(download_data.get("dlink") or "").strip()
            if not dlink and isinstance(download_data.get("data"), dict):
                dlink = str(download_data["data"].get("dlink") or "").strip()

            if dlink:
                row["direct_url"] = dlink
                return row, rows

            last = RuntimeError("TeraBox share/download returned no dlink.")
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            last = exc

    raise last or RuntimeError("TeraBox shorturlinfo resolution failed.")

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
                if usable:
                    row=usable[0]
                    return {"surl":surl,"file_name":row["file_name"],"size":row["size"],
                            "fs_id":row["fs_id"],"direct_url":row["direct_url"],
                            "files":files[:50],"thumbnail":row["thumbnail"]}

                row, token_rows = await _resolve_via_shorturlinfo(
                    session, origin, url, surl
                )
                return {"surl":surl,"file_name":row["file_name"],"size":row["size"],
                        "fs_id":row["fs_id"],"direct_url":row["direct_url"],
                        "files":token_rows[:50],"thumbnail":row["thumbnail"]}
            except (aiohttp.ClientError,asyncio.TimeoutError,RuntimeError) as e:
                last=e; log.info("listing failed via %s: %s",origin,e)
        raise RuntimeError(f"Could not read the TeraBox share. Last error: {last}")
