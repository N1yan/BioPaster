"""Keep background Python logging and warnings out of the interactive UI."""
from contextlib import contextmanager
import logging
import warnings


@contextmanager
def background_logging():
    """Discard background diagnostics without redirecting UI stdout/stderr."""
    root = logging.getLogger()
    previous = root.handlers[:]
    previous_warning = warnings.showwarning
    handler = logging.NullHandler()
    root.handlers = [handler]
    logging.captureWarnings(True)
    try:
        yield
    finally:
        root.handlers = previous
        logging.captureWarnings(False)
        warnings.showwarning = previous_warning
        handler.close()
