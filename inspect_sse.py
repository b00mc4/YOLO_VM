import site
import os

try:
    from sse_starlette.sse import EventSourceResponse
    import inspect
    print(inspect.getsource(EventSourceResponse))
except Exception as e:
    print(f"Error: {e}")
