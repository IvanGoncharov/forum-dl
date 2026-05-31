# pyright: strict
from __future__ import annotations

from .. import options
from ..session import SessionOptions
from ..version import __version__


def test_time_sleep_cli_option_default():
    parser = options.build_parser()
    args = parser.parse_args(["https://example.com/thread"])
    assert args.time_sleep == "5"


def test_time_sleep_cli_option_override():
    parser = options.build_parser()
    args = parser.parse_args(["--time-sleep", "2", "https://example.com/thread"])
    assert args.time_sleep == "2"


def test_session_options_time_sleep_coerces_to_int():
    session_options = SessionOptions(
        timeout=5,
        download_timeout=60,
        retries=1,
        retry_sleep=1,
        retry_sleep_multiplier=2,
        warc_output="",
        user_agent=f"Forum-dl {__version__}",
        get_urls=False,
        time_sleep="3",
    )
    assert session_options.time_sleep == 3
