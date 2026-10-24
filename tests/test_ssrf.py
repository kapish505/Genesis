import pytest

from gateway.ssrf import SSRFError, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://localhost",
        "file:///etc/passwd",
        "http://169.254.169.254/latest",
    ],
)
def test_ssrf_blocks_private_and_non_http(url):
    with pytest.raises(SSRFError):
        validate_url(url)


def test_allowed_host_is_exact():
    with pytest.raises(SSRFError):
        validate_url("https://example.org", {"example.com"})
