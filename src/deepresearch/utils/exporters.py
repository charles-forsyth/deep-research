import json
import re


class DataExporter:
    @staticmethod
    def extract_code_block(text: str, lang: str = "") -> str:
        """Extracts content from a markdown code block."""
        pattern = rf"```{lang}\n(.*?)\n```"
        match = re.search(pattern, text, re.DOTALL)
        if not match and lang:
            pattern = r"```\n(.*?)\n```"
            match = re.search(pattern, text, re.DOTALL)
        return match.group(1) if match else text

    @staticmethod
    def save_json(content: str, filepath: str):
        try:
            clean_content = DataExporter.extract_code_block(content, "json")
            data = json.loads(clean_content)
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
            print(f"[INFO] JSON report saved to {filepath}")
        except json.JSONDecodeError as e:
            print(f"[ERROR] Failed to parse JSON output: {e}")
            with open(filepath + ".raw", "w") as f:
                f.write(content)
            print(f"[WARN] Raw content saved to {filepath}.raw")

    @staticmethod
    def save_csv(content: str, filepath: str):
        try:
            clean_content = DataExporter.extract_code_block(content, "csv")
            with open(filepath, "w") as f:
                f.write(clean_content)
            print(f"[INFO] CSV report saved to {filepath}")
        except Exception as e:
            print(f"[ERROR] Failed to save CSV: {e}")

    @staticmethod
    def export(content: str, filepath: str):
        if filepath.lower().endswith(".json"):
            DataExporter.save_json(content, filepath)
        elif filepath.lower().endswith(".csv"):
            DataExporter.save_csv(content, filepath)
        else:
            with open(filepath, "w") as f:
                f.write(content)
            print(f"[INFO] Report saved to {filepath}")
