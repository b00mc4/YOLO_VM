import asyncio
import httpx
import json

async def main():
    async with httpx.AsyncClient() as client:
        response = await client.get('http://127.0.0.1:8000/api/v1/sse/test')
        async for line in response.aiter_lines():
            print(line)

asyncio.run(main())
