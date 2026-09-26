"""CLI wiring for `deep-research dashboard`."""

from unittest.mock import patch

import pytest

from deepresearch.__main__ import build_parser, main


def test_dashboard_is_a_known_command():
    args = build_parser().parse_args(["dashboard", "--start"])
    assert args.command == "dashboard" and args.start


def test_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["dashboard", "--start", "--stop"])


@pytest.mark.parametrize(
    "argv,fn,call_args",
    [
        (["dashboard", "--start"], "start", ("0.0.0.0", 7420)),
        (["dashboard", "--start", "--port", "9000"], "start", ("0.0.0.0", 9000)),
        (["dashboard", "--stop"], "stop", ()),
        (["dashboard", "--restart"], "restart", (None, None)),
        (["dashboard"], "status", ()),
        (["dashboard", "--status"], "status", ()),
    ],
)
def test_dispatch(argv, fn, call_args):
    with (
        patch("sys.argv", ["deep-research", *argv]),
        patch(f"deepresearch.dashboard.daemon.{fn}", return_value=0) as m,
    ):
        main()
    m.assert_called_once_with(*call_args)


def test_nonzero_exit_propagates():
    with (
        patch("sys.argv", ["deep-research", "dashboard", "--start"]),
        patch("deepresearch.dashboard.daemon.start", return_value=1),
        pytest.raises(SystemExit) as exc,
    ):
        main()
    assert exc.value.code == 1


def test_help_warns_about_no_auth():
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    text = " ".join(action.choices["dashboard"].format_help().split())  # type: ignore[union-attr]
    assert "no login" in text and "127.0.0.1" in text and "7420" in text
