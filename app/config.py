import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    webhook_secret: str
    port: int

def load_settings() -> Settings:
    bot_token=os.getenv("BOT_TOKEN","").strip()
    api_id_raw=os.getenv("API_ID","").strip()
    api_hash=os.getenv("API_HASH","").strip()
    missing=[k for k,v in (("BOT_TOKEN",bot_token),("API_ID",api_id_raw),("API_HASH",api_hash)) if not v]
    if missing: raise RuntimeError("Missing required environment variables: "+", ".join(missing))
    return Settings(bot_token,int(api_id_raw),api_hash,os.getenv("WEBHOOK_SECRET","").strip(),int(os.getenv("PORT","10000")))
