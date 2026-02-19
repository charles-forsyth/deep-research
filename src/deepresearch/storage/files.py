import os
import time
import typing
from rich.console import Console

console = Console(width=120)


class FileManager:
    def __init__(self, client):
        self.client = client
        self.created_stores = []
        self.uploaded_files = []

    def create_store_from_paths(self, paths: list[str]) -> str:
        console.print(
            f"[bold cyan][INFO][/] Uploading {len(paths)} items to a new File Search Store..."
        )
        store = self.client.file_search_stores.create()
        self.created_stores.append(store.name)
        console.print(f"[bold cyan][INFO][/] Created temporary store: {store.name}")

        for path in paths:
            if os.path.isdir(path):
                for f in os.listdir(path):
                    full_path = os.path.join(path, f)
                    if os.path.isfile(full_path):
                        self._upload_file(full_path, store.name)
            elif os.path.isfile(path):
                self._upload_file(path, store.name)
            else:
                console.print(f"[bold yellow][WARN][/] Skipped invalid path: {path}")

        console.print("[bold cyan][INFO][/] Waiting 5s for file ingestion...")
        time.sleep(5)
        return store.name

    def _upload_file(self, path: str, store_name: str):
        console.print(f"[bold cyan][INFO][/] Uploading: {path}")
        try:
            mime_type = None
            if path.endswith(
                (".py", ".toml", ".md", ".json", ".lock", ".yml", ".yaml", ".txt")
            ):
                mime_type = "text/plain"

            if hasattr(self.client.file_search_stores, "upload_to_file_search_store"):
                kwargs: typing.Dict[str, typing.Any] = {
                    "file_search_store_name": store_name,
                    "file": path,
                }
                if mime_type:
                    kwargs["config"] = {"mime_type": mime_type}

                self.client.file_search_stores.upload_to_file_search_store(**kwargs)
            else:
                upload_config: typing.Dict[str, typing.Any] | None = None
                if mime_type:
                    upload_config = {"mime_type": mime_type}

                file_obj = self.client.files.upload(path=path, config=upload_config)
                self.uploaded_files.append(file_obj.name)

                start_time = time.time()
                while file_obj.state.name == "PROCESSING":
                    if time.time() - start_time > 300:
                        raise TimeoutError(
                            f"File processing timed out after 5 minutes: {path}"
                        )
                    time.sleep(2)
                    file_obj = self.client.files.get(name=file_obj.name)
        except Exception as e:
            console.print(f"[bold red][ERROR][/] Failed to upload {path}: {e}")
            raise

    def cleanup(self):
        console.print("\n[bold cyan][INFO][/] Cleaning up temporary resources...")
        for store_name in self.created_stores:
            try:
                if hasattr(self.client.file_search_stores, "documents"):
                    try:
                        pager = self.client.file_search_stores.documents.list(
                            parent=store_name
                        )
                        for doc in pager:
                            try:
                                console.print(
                                    f"[bold cyan][INFO][/] Deleting document: {doc.name}"
                                )
                                self.client.file_search_stores.documents.delete(
                                    name=doc.name, config={"force": True}
                                )
                            except Exception as e:
                                console.print(
                                    f"[bold yellow][WARN][/] Failed to delete document {doc.name}: {e}"
                                )
                    except Exception as e:
                        console.print(
                            f"[bold yellow][WARN][/] Failed to list documents in {store_name}: {e}"
                        )

                self.client.file_search_stores.delete(name=store_name)
                console.print(f"[bold cyan][INFO][/] Deleted store: {store_name}")
            except Exception as e:
                if "non-empty" in str(e):
                    console.print(
                        f"[bold yellow][WARN][/] Could not delete store {store_name} (contains files). It will persist."
                    )
                else:
                    console.print(
                        f"[bold yellow][WARN][/] Failed to delete store {store_name}: {e}"
                    )

        for file_name in self.uploaded_files:
            try:
                self.client.files.delete(name=file_name)
                console.print(
                    f"[bold cyan][INFO][/] Deleted file resource: {file_name}"
                )
            except Exception:
                pass
