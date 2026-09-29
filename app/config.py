import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    webhook_secret: str
    public_base_url: str
    port: int
    terabox_edge_resolver_url: str

def load_settings() -> Settings:
    bot_token=os.getenv("BOT_TOKEN","").strip()
    api_id_raw=os.getenv("API_ID","").strip()
    api_hash=os.getenv("API_HASH","").strip()
    missing=[k for k,v in (("BOT_TOKEN",bot_token),("API_ID",api_id_raw),("API_HASH",api_hash)) if not v]
    if missing:
        raise RuntimeError("Missing required environment variables: "+", ".join(missing))
    return Settings(
        bot_token=bot_token,
        api_id=int(api_id_raw),
        api_hash=api_hash,
        webhook_secret=os.getenv("WEBHOOK_SECRET","").strip(),
        public_base_url=os.getenv("PUBLIC_BASE_URL","").strip().rstrip("/"),
        port=int(os.getenv("PORT","10000")),
        terabox_edge_resolver_url=os.getenv(
            "TERABOX_EDGE_RESOLVER_URL",
            "https://terabox-worker.robinkumarshakya103.workers.dev/api",
        ).strip().rstrip("/"),
    )
