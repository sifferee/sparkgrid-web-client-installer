"""Minimal tqdm stub for camoufox runtime.
The real tqdm is only needed for download progress bars during browser installation.
In the standalone build, the browser is pre-installed, so tqdm is never called at runtime.
"""
class tqdm:
    def __init__(self, iterable=None, total=None, desc=None, unit=None,
                 unit_scale=False, bar_format=None, **kwargs):
        self._iterable = iterable
        self._total = total
        self._n = 0
    def __iter__(self):
        if self._iterable is None:
            return iter([])
        for item in self._iterable:
            self._n += 1
            yield item
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def update(self, n=1):
        self._n += n
    def close(self):
        pass
    def set_description(self, *args, **kwargs):
        pass
    def refresh(self):
        pass
    @classmethod
    def write(cls, *args, **kwargs):
        pass
