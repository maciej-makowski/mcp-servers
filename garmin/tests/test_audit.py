"""Copied audit module redaction (same shared core as the other servers)."""
from garmin_mcp import audit


def test_audit_redacts_secret_keys_and_values():
    out = audit.redact({
        "Authorization": "Bearer zz",
        "q": "sk-abc123 x",
        "nested": [{"token": "abc"}],
        "activity_id": "12345",
    })
    assert out["Authorization"] == "***"
    assert "sk-" not in out["q"]
    assert out["nested"] == [{"token": "***"}]
    assert out["activity_id"] == "12345"  # ordinary args survive
