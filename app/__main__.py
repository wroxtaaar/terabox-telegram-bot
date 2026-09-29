import asyncio,os
from dotenv import load_dotenv
from hypercorn.asyncio import serve
from hypercorn.config import Config
from .server import create_app
async def main():
    load_dotenv()
    c=Config(); c.bind=[f"0.0.0.0:{os.getenv('PORT','10000')}"]; c.accesslog="-"; c.errorlog="-"
    await serve(create_app(),c)
if __name__=="__main__": asyncio.run(main())
