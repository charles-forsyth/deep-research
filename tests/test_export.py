from unittest.mock import mock_open, patch
from deep_research import DataExporter, ResearchRequest

def test_extract_code_block_json():
    text = (
        "Here is the report:\n"
        "```json\n"
        '{"key": "value"}\n'
        "```\n"
    )
    assert DataExporter.extract_code_block(text, "json").strip() == '{"key": "value"}'

def test_extract_code_block_generic():
    text = (
        "```\n"
        "data,value\n"
        "1,2\n"
        "```\n"
    )
    extracted = DataExporter.extract_code_block(text, "csv")
    assert "data,value" in extracted
    assert "1,2" in extracted

def test_extract_code_block_no_block():
    text = "Just raw text"
    assert DataExporter.extract_code_block(text) == "Just raw text"

def test_save_json_valid():
    content = '```json\n{"a": 1}\n```'
    with patch("builtins.open", mock_open()) as mock_file, patch("rich.console.Console") as mock_console:
        DataExporter.save_json(content, "out.json", mock_console)
        mock_file.assert_called_with("out.json", "w")
        handle = mock_file()
        handle.write.assert_called()

def test_save_json_invalid_fallback():
    content = "Not JSON"
    with patch("builtins.open", mock_open()) as mock_file, patch("rich.console.Console") as mock_console:
        DataExporter.save_json(content, "out.json", mock_console)
        # Should save to .raw
        mock_file.assert_called_with("out.json.raw", "w")

def test_request_auto_format_json():
    req = ResearchRequest(prompt="test", output_file="data.json")
    assert "Output the final report as valid JSON" in req.final_prompt

def test_request_auto_format_csv():
    req = ResearchRequest(prompt="test", output_file="data.csv")
    assert "Output the final report as valid CSV" in req.final_prompt
