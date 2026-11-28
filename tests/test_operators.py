import pytest

from services.app import _parse_operators
from services.session import _issue, _session

# New format: dict[name] = {token, role, expires_at}
OPERATORS = {
    "ana": {"token": "token-ana-12345", "role": "operator", "expires_at": None},
    "luis": {"token": "token-luis-1234", "role": "operator", "expires_at": None}
}


def test_operators_are_read_from_a_name_token_string():
    result = _parse_operators("ana:token-ana-12345, luis:token-luis-1234")
    assert result == OPERATORS
    assert _parse_operators("") == {} and _parse_operators(None) == {}


@pytest.mark.parametrize("spec", [
    "ana",                                              # no token
    "ana:short",                                        # short token
    "admin:token-long-123456",                         # reserved name
    "an a:token-long-123456",                          # invalid name
    "ana:token-long-123456,luis:token-long-123456",   # repeated token
    "ana:token-long-123456,ana:other-token-123456",     # repeated name
])
def test_invalid_operators_config_is_rejected(spec):
    with pytest.raises(ValueError):
        _parse_operators(spec)


def test_the_session_cookie_cannot_be_passed_to_another_operator():
    # _session expects dict[str, str] (name -> token)
    tokens = {name: meta["token"] for name, meta in OPERATORS.items()}
    v = _issue(tokens["ana"], "ana")
    assert _session(tokens, v) == "ana"
    assert _session(tokens, "luis" + v[len("ana"):]) is None
    name, exp, sig = v.split(".")
    assert _session(tokens, f"{name}.{int(exp) + 1}.{sig}") is None
    assert _session(tokens, None) is None
    assert _session(tokens, "basura") is None