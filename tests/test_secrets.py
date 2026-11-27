import pytest

from dark_factory.secrets import secret


def test_value_comes_from_the_environment():
    assert secret("TOKEN", {"TOKEN": "abc"}) == "abc"
    assert secret("TOKEN", {}) == ""
    assert secret("", {"": "x"}) == ""


def test_a_mounted_secret_file_is_read_without_its_trailing_newline(tmp_path):
    path = tmp_path / "token"
    path.write_text("from-file\n")
    assert secret("TOKEN", {"TOKEN_FILE": str(path), "TOKEN": "ignored"}) == "from-file"


def test_secret_file_must_be_absolute_regular_and_small(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        secret("TOKEN", {"TOKEN_FILE": "relative"})
    with pytest.raises(ValueError, match="regular"):
        secret("TOKEN", {"TOKEN_FILE": str(tmp_path)})
    big = tmp_path / "big"
    big.write_bytes(b"x" * 70000)
    with pytest.raises(ValueError, match="small"):
        secret("TOKEN", {"TOKEN_FILE": str(big)})


def test_worker_and_operator_credentials_resolve_through_files(tmp_path, monkeypatch):
    from dark_factory.config import load_operator_token

    token = tmp_path / "operator"
    token.write_text("o" * 40 + "\n")
    monkeypatch.setenv("REMOTE_OPERATOR_TOKEN_FILE", str(token))
    monkeypatch.delenv("REMOTE_OPERATOR_TOKEN", raising=False)
    profile = tmp_path / "operator.toml"
    profile.write_text('[runtime]\noperator_token_env = "REMOTE_OPERATOR_TOKEN"\n')
    assert load_operator_token(profile) == "o" * 40
