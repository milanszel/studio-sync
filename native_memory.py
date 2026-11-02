"""Drain temporary Objective-C objects on background capture threads."""

import sys
from functools import wraps


def native_pool(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if sys.platform != "darwin":
            return function(*args, **kwargs)
        import objc

        # A new pool per call also drains on exceptions and early returns.
        with objc.autorelease_pool():
            return function(*args, **kwargs)

    return wrapped
