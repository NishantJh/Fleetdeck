import logging

from fleetdeck.__main__ import _QuietPolls, configure_logging


def _access(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:1", method, path, "1.1", status),
        None,
    )


def test_quiet_polls_drops_successful_polls_only():
    f = _QuietPolls()
    assert not f.filter(_access("GET", "/api/targets", 200))
    assert not f.filter(_access("GET", "/api/jobs/abc?since=3", 200))
    assert f.filter(_access("GET", "/api/targets", 500))
    assert f.filter(_access("POST", "/api/jobs", 200))
    assert f.filter(_access("GET", "/", 200))


def test_asyncssh_info_is_silenced():
    configure_logging()
    assert logging.getLogger("asyncssh").getEffectiveLevel() == logging.WARNING
