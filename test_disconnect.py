import asyncio
from fastapi import FastAPI, Request
from sse_starlette.sse import EventSourceResponse
import uvicorn

app = FastAPI()

@app.get("/sse")
async def sse(request: Request):
    async def event_generator():
        yield {"event": "test", "data": "initial"}
        print("After initial")
        
        print("Waiting for disconnect...")
        disc = await request.is_disconnected()
        print(f"Disconnected? {disc}")
        
        while True:
            yield {"event": "test", "data": "hello"}
            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
