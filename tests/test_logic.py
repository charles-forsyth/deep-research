from unittest.mock import MagicMock, patch
import pytest
from deep_research import FileManager, DeepResearchAgent, ResearchRequest

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
    
    with patch("os.path.isdir", return_value=False), \
         patch("os.path.isfile", return_value=True):
        
        store_name = fm.create_store_from_paths(["doc.pdf"])
        
        assert store_name == "stores/test-store"
        # Verify store creation
        mock_client.file_search_stores.create.assert_called_once()
        # Verify file upload
        mock_client.file_search_stores.upload_to_file_search_store.assert_called_once_with(
            file_search_store_name="stores/test-store",
            file="doc.pdf"
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
    mock_client.file_search_stores.documents.list.assert_called_with(parent="stores/test-store")
    # Verify document force deletion
    mock_client.file_search_stores.documents.delete.assert_called_with(
        name="docs/test-doc", 
        config={'force': True}
    )
    # Verify store deletion
    mock_client.file_search_stores.delete.assert_called_with(name="stores/test-store")

def test_agent_auto_upload_and_cleanup(mock_client):
    """Test that agent handles auto-upload, modifies prompt, and cleans up."""
    # Setup config
    config = MagicMock()
    config.api_key = "test"
    
    agent = DeepResearchAgent(config)
    agent.client = mock_client
    # Mock the internal file manager
    agent.file_manager = MagicMock()
    agent.file_manager.create_store_from_paths.return_value = "stores/temp-store"
    
    # Setup request with upload
    req = ResearchRequest(prompt="Base prompt", upload_paths=["doc.pdf"])
    
    # Run stream (mocking interaction to avoid loop)
    mock_client.interactions = MagicMock() # Ensure attribute exists
    
    # We expect start_research_stream to call interactions.create
    # We force an empty stream to return immediately
    mock_client.interactions.create.return_value = []
    
    agent.start_research_stream(req)
    
    # 1. Verify Auto-Upload was triggered
    agent.file_manager.create_store_from_paths.assert_called_with(["doc.pdf"])
    
    # 2. Verify Prompt was modified to include priority instruction
    call_args = mock_client.interactions.create.call_args
    assert "IMPORTANT: You have access to a File Search Store" in call_args.kwargs['input']
    
    # 3. Verify Store was added to tools
    tools = call_args.kwargs['tools']
    assert tools[0]['file_search_store_names'] == ["stores/temp-store"]
    
    # 4. Verify Cleanup was called
    agent.file_manager.cleanup.assert_called_once()

def test_recursive_research():
    with patch("deep_research.DeepResearchAgent.start_research_poll") as mock_poll, \
         patch("deep_research.DeepResearchAgent.analyze_gaps") as mock_gaps, \
         patch("deep_research.DeepResearchAgent.synthesize_findings") as mock_synth, \
         patch("deep_research.SessionManager.create_session") as mock_create_session, \
         patch("deep_research.SessionManager.get_session") as mock_get_session, \
         patch("deep_research.SessionManager.update_session"):
         
         # Setup mocks
         mock_poll.return_value = "interaction_123"
         mock_gaps.return_value = ["Q1", "Q2"]
         mock_synth.return_value = "Final Report"
         mock_create_session.return_value = 100
         mock_get_session.return_value = {'status': 'completed', 'result': 'Initial Report', 'id': 1}
         
         agent = DeepResearchAgent()
         req = ResearchRequest(prompt="Topic", depth=2)
         
         agent.start_recursive_research(req)
         
         # Verify logic
         # 1 Parent + 2 Children = 3 calls
         assert mock_poll.call_count == 3
         mock_gaps.assert_called_once()
         mock_synth.assert_called_once()
         
         # Verify synthesis args
         args = mock_synth.call_args
         assert args[0][0] == "Topic" # original prompt
         assert args[0][1] == "Initial Report" # main report
         # Sub reports should be in the list (mocked result from get_session is 'Initial Report' for children too)
         assert len(args[0][2]) == 2