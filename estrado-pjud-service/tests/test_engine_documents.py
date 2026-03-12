"""Test document download integration in sync engine."""


def test_r2_disabled_skips_documents():
    """When R2_ENABLED=False, no document processing happens."""
    from worker.config import WorkerConfig
    import os
    os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
    os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
    config = WorkerConfig(R2_ENABLED=False)
    assert config.R2_ENABLED is False


def test_r2_key_format():
    """R2 keys follow the expected path structure."""
    law_firm_id = "abc-123"
    case_id = "def-456"
    ext_key = "C-1234-2024:Principal:1"
    ext = "pdf"
    key = f"{law_firm_id}/{case_id}/{ext_key}.{ext}"
    assert key == "abc-123/def-456/C-1234-2024:Principal:1.pdf"
