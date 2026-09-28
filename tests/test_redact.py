from cc_exec_guard import redact


def test_redact_token():
    out = redact.redact("password=secret123")
    assert "secret123" not in out and "[REDACTED]" in out


def test_redact_bearer():
    out = redact.redact("Authorization: Bearer abcdef1234567890")
    assert "abcdef1234567890" not in out


def test_preview_truncates():
    p, truncated = redact.preview("x" * 2000, limit=100)
    assert truncated and len(p) < 2000


def test_hash_stable():
    assert redact.sha256_hex("ls") == redact.sha256_hex("ls")
