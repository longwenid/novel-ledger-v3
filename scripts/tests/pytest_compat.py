from __future__ import annotations
import sys
import contextlib

class RaisesContext:
    def __init__(self, expected_exception, match=None):
        self.expected_exception = expected_exception
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(f'DID NOT RAISE {self.expected_exception}')
        if not issubclass(exc_type, self.expected_exception):
            return False
        self.value = exc_val
        if self.match:
            import re
            if not re.search(self.match, str(exc_val)):
                raise AssertionError(f'Pattern {self.match!r} does not match {str(exc_val)!r}')
        return True

def raises(expected_exception, match=None):
    return RaisesContext(expected_exception, match=match)

def fixture(*args, **kwargs):
    def decorator(fn):
        return fn
    if len(args) == 1 and callable(args[0]):
        return args[0]
    return decorator

class Mark:
    def __getattr__(self, name):
        return lambda *args, **kwargs: (lambda fn: fn)

mark = Mark()
