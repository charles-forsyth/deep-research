from unittest.mock import MagicMock, patch
import pytest
from deepresearch.storage.files import FileManager


@pytest.fixture
def mock_client():
    client = MagicMock()
    # Setup nested mocks
    client.file_search_stores.create.return_value.name = "stores/test-store"
    client.files.upload.return_value.name = "files/test-file"
    client.files.upload.return_value.state.name = "ACTIVE"
    return client


def test_file_manager_create_store(mock_client):
    """Test that FileManager creates a store and uploads files."""
    fm = FileManager(mock_client)

    # Mock upload helper existence
    mock_client.file_search_stores.upload_to_file_search_store = MagicMock()

    with (
        patch("os.path.isdir", return_value=False),
        patch("os.path.isfile", return_value=True),
    ):
        store_name = fm.create_store_from_paths(["doc.pdf"])

        assert store_name == "stores/test-store"
        # Verify store creation
        mock_client.file_search_stores.create.assert_called_once()
        # Verify file upload
        mock_client.file_search_stores.upload_to_file_search_store.assert_called_once_with(
            file_search_store_name="stores/test-store", file="doc.pdf"
        )


def test_file_manager_cleanup(mock_client):
    """Test that cleanup lists documents, force-deletes them, and deletes the store."""
    fm = FileManager(mock_client)
    fm.created_stores = ["stores/test-store"]

    # Mock document listing
    mock_doc = MagicMock()
    mock_doc.name = "docs/test-doc"
    mock_client.file_search_stores.documents.list.return_value = [mock_doc]

    fm.cleanup()

    # Verify document listing
    mock_client.file_search_stores.documents.list.assert_called_with(
        parent="stores/test-store"
    )
    # Verify document force deletion
    mock_client.file_search_stores.documents.delete.assert_called_with(
        name="docs/test-doc", config={"force": True}
    )
    # Verify store deletion
    mock_client.file_search_stores.delete.assert_called_with(name="stores/test-store")


def test_file_manager_invalid_path(mock_client):
    fm = FileManager(mock_client)
    with (
        patch("os.path.isdir", return_value=False),
        patch("os.path.isfile", return_value=False),
    ):
        fm.create_store_from_paths(["invalid_path"])
        mock_client.file_search_stores.create.assert_called_once()


def test_file_manager_upload_error(mock_client):
    fm = FileManager(mock_client)
    with (
        patch("os.path.isdir", return_value=False),
        patch("os.path.isfile", return_value=True),
    ):
        mock_client.file_search_stores.upload_to_file_search_store.side_effect = (
            Exception("Upload error")
        )
        with pytest.raises(Exception):
            fm.create_store_from_paths(["valid_path"])


def test_file_manager_cleanup_error(mock_client):
    fm = FileManager(mock_client)
    fm.created_stores = ["store1"]
    mock_client.file_search_stores.documents.list.side_effect = Exception("List error")
    mock_client.file_search_stores.delete.side_effect = Exception("Delete error")
    fm.cleanup()  # Should swallow exceptions
