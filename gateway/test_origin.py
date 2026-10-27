import pytest

from gateway.main import validate_public_base


@pytest.mark.parametrize(
  'origin,expected',
  [
    ('https://memory.example.net', 'https://memory.example.net'),
    ('https://memory.example.net:8443/', 'https://memory.example.net:8443'),
  ],
)
def test_configured_https_origin(origin, expected):
  assert validate_public_base(origin) == expected


@pytest.mark.parametrize(
  'origin',
  [
    'http://memory.example.net',
    'https://',
    'https://user:password@memory.example.net',
    'https://memory.example.net/path',
    'https://memory.example.net?x=1',
    'https://memory.example.net#fragment',
    'https://memory.example.net:bad',
  ],
)
def test_invalid_public_origin_is_rejected(origin):
  with pytest.raises(ValueError):
    validate_public_base(origin)
