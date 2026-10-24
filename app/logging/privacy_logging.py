"""Runtime logs contain source location and exception type, never content.

A single LogRecord factory also covers SQLAlchemy/provider exception bind
parameters and third-party HTTP diagnostics. No request, response, URL, tool
arguments or exception message is emitted. B8 may add reviewed structured
operational fields; privacy maintenance already exposes bounded counts.
"""
import logging


def install_privacy_logging():
    previous = logging.getLogRecordFactory()
    if getattr(previous, '_privacy_safe', False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        exception_type = record.exc_info[0].__name__ if record.exc_info else 'none'
        # Keep only a reviewed, fixed-template numeric maintenance outcome.
        if record.msg == 'privacy_retention_removed=%d' and isinstance(record.args, tuple) and len(record.args)==1 and type(record.args[0]) is int:
            pass
        else:
            record.msg = 'event module=%s function=%s line=%d exception=%s'
            record.args = (record.module, record.funcName, record.lineno, exception_type)
        record.exc_info = None; record.exc_text = None; record.stack_info = None
        return record
    factory._privacy_safe = True
    logging.setLogRecordFactory(factory)
