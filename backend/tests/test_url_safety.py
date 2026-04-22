"""Unit tests for URL safety validation."""
from app.routers.video import is_safe_url


def test_safe_external_https():
    assert is_safe_url("https://example.com/video.mp4") is True


def test_safe_external_http():
    assert is_safe_url("http://example.com/video.mp4") is True


def test_blocks_localhost_non_storage():
    assert is_safe_url("http://localhost:8080/internal") is False
    assert is_safe_url("http://localhost/admin") is False


def test_blocks_loopback_non_storage():
    assert is_safe_url("http://127.0.0.1/admin") is False
    assert is_safe_url("http://127.0.0.1:8080/api") is False


def test_allows_localhost_temp_storage():
    assert is_safe_url("http://localhost:8000/temp_storage/abc.mp4") is True
    assert is_safe_url("http://127.0.0.1:8000/temp_storage/xyz.mp4") is True


def test_blocks_private_ip_ranges():
    assert is_safe_url("http://192.168.1.1/video") is False
    assert is_safe_url("http://10.0.0.1/video") is False
    assert is_safe_url("http://172.16.0.1/video") is False


def test_blocks_non_http_schemes():
    assert is_safe_url("file:///etc/passwd") is False
    assert is_safe_url("ftp://example.com/video") is False
    assert is_safe_url("javascript:alert(1)") is False


def test_blocks_empty_and_malformed():
    assert is_safe_url("") is False
    assert is_safe_url("not-a-url") is False
    assert is_safe_url("://missing-scheme.com") is False
