import asyncio
from datetime import datetime

def defn(cls=None):
    def wrapper(cls):
        return cls
    return wrapper(cls) if cls else wrapper

def signal(fn):
    return fn

def query(fn):
    return fn

def run(fn):
    return fn

async def wait_condition(cond):
    while not cond():
        await asyncio.sleep(0.01)

def now():
    return datetime.utcnow()
