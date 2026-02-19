from unittest.mock import mock_open, patch
from deepresearch.utils.exporters import DataExporter


def test_extract_code_block_json():
    text = """Here is the report:
```json
{"key": "value"}
```
"""
    assert DataExporter.extract_code_block(text, "json").strip() == '{"key": "value"}'


def test_extract_code_block_generic():
    text = """```
data,value
1,2
```
"""
    extracted = DataExporter.extract_code_block(text, "csv")
    assert "data,value" in extracted
    assert "1,2" in extracted


def test_extract_code_block_no_block():
    text = "Just raw text"
    assert DataExporter.extract_code_block(text) == "Just raw text"


def test_save_json_valid():
    content = """```json
{"a": 1}
```"""
    with patch("builtins.open", mock_open()) as mock_file:
        DataExporter.save_json(content, "out.json")
        mock_file.assert_called_with("out.json", "w")
        handle = mock_file()
        handle.write.assert_called()


def test_save_json_invalid_fallback():
    content = "Not JSON"
    with patch("builtins.open", mock_open()) as mock_file:
        DataExporter.save_json(content, "out.json")
        mock_file.assert_called_with("out.json.raw", "w")


def test_data_exporter_coverage():
    # save_csv
    with patch("builtins.open", mock_open()) as mock_file:
        DataExporter.save_csv("csv,data", "out.csv")
        mock_file.assert_called_with("out.csv", "w")

    # save_csv exception
    with patch("builtins.open", side_effect=Exception("Disk error")):
        DataExporter.save_csv("csv,data", "out.csv")

    # export
    with (
        patch("deepresearch.utils.exporters.DataExporter.save_json") as mock_json,
        patch("deepresearch.utils.exporters.DataExporter.save_csv") as mock_csv,
        patch("builtins.open", mock_open()) as mock_file,
    ):
        DataExporter.export("json_data", "file.json")
        mock_json.assert_called_once()

        DataExporter.export("csv_data", "file.csv")
        mock_csv.assert_called_once()

        DataExporter.export("text_data", "file.txt")
        mock_file.assert_called_with("file.txt", "w")
