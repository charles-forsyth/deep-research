"""Help-text regression tests: the --help output is part of the CLI contract."""

from unittest.mock import patch

import pytest

from deepresearch.__main__ import build_parser


def _sub_help(name: str) -> str:
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    return action.choices[name].format_help()  # type: ignore[union-attr]


def test_top_level_examples_numbered_once():
    text = build_parser().format_help()
    numbers = [
        line.split(".")[0].strip()
        for line in text.splitlines()
        if line[:1].isdigit() and ". " in line
    ]
    assert numbers == [str(i) for i in range(1, len(numbers) + 1)]
    assert "Support web search" not in text
    assert "Supports web search" in text


def test_prog_name_is_stable():
    # sys.argv[0] varies (deepresearch, __main__.py); help should not.
    assert build_parser().format_usage().startswith("usage: deep-research")


@pytest.mark.parametrize("cmd", ["followup", "show", "delete"])
def test_id_help_is_consistent(cmd):
    assert "Session ID (integer, from `list`) or Interaction ID" in _sub_help(cmd)


@pytest.mark.parametrize("cmd", ["research", "start", "estimate"])
def test_depth_and_breadth_show_defaults(cmd):
    text = " ".join(_sub_help(cmd).split())
    assert "(default: 1)" in text
    assert "(default: 3)" in text


def test_start_accepts_stores_like_research():
    assert "--stores" in _sub_help("start")
    assert "--stores" in _sub_help("research")


@patch(
    "sys.argv",
    ["deepresearch", "start", "P", "--stores", "fileSearchStores/abc"],
)
@patch("deepresearch.cli.commands.detach_process", return_value=1)
@patch("deepresearch.cli.commands.SessionManager")
def test_start_forwards_stores_to_child(mock_mgr, mock_detach):
    from deepresearch.__main__ import main

    mock_mgr.return_value.create_session.return_value = 7
    main()
    child_args = mock_detach.call_args[0][0]
    i = child_args.index("--stores")
    assert child_args[i + 1] == "fileSearchStores/abc"


def test_cleanup_help_warns_it_deletes_everything():
    text = " ".join(_sub_help("cleanup").split())
    assert "ALL File Search Stores" in text
    assert "--stores" in text


def test_delete_help_says_no_confirmation():
    assert "no confirmation" in " ".join(_sub_help("delete").split())


def test_output_help_explains_extensions():
    text = " ".join(_sub_help("research").split())
    assert ".json" in text and ".csv" in text
