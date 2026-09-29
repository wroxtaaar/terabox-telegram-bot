from urllib.parse import urlparse
TERABOX_HOSTS={"terabox.com","www.terabox.com","1024terabox.com","www.1024terabox.com","teraboxapp.com","www.teraboxapp.com"}
def is_terabox_url(value:str)->bool:
    try: host=(urlparse(value.strip()).hostname or "").lower()
    except ValueError: return False
    return host in TERABOX_HOSTS or host.endswith(".terabox.com")
async def resolve_terabox_url(url:str)->dict:
    raise RuntimeError("Anonymous TeraBox resolver is not implemented yet")
