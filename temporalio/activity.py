def defn(fn=None):
    def wrapper(fn):
        return fn
    return wrapper(fn) if fn else wrapper
