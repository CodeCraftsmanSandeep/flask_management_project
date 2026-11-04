import logging
from pathlib import Path

import pytest

from app.credentials import SECRET_NAMES, TOKEN_NAMES, TotpCredentials, load_credentials, parse_keys
from app.errors import CredentialsError

FAKE_TOKEN = "eyJhbGciOiJub25lIn0.eyJleHAiOjk5OTk5OTk5OTl9.ZmFrZS1zaWduYXR1cmU"  # not a real token
FAKE_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
NO_ENV: dict[str, str] = {}

# Same *labels* as the project's real keys.txt (including the "TOP" spelling), fake values.
REAL_LAYOUT = f"""\
GROW_API_KEY=not-needed-api-key-value
GROW_API_SECRET=not-needed-api-secret-value

GROWW_TOP_TOKEN={FAKE_TOKEN}
GROWW_TOP_QR_CODE={FAKE_SECRET}
"""


def write_keys(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "keys.txt"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_reads_the_label_spelling_used_in_the_real_keys_file(tmp_path: Path) -> None:
    creds = load_credentials(write_keys(tmp_path, REAL_LAYOUT), NO_ENV)
    assert (creds.token, creds.secret) == (FAKE_TOKEN, FAKE_SECRET)


def test_reads_the_canonical_names(tmp_path: Path) -> None:
    text = f"{TOKEN_NAMES[0]}={FAKE_TOKEN}\n{SECRET_NAMES[0]}={FAKE_SECRET}\n"
    creds = load_credentials(write_keys(tmp_path, text), NO_ENV)
    assert (creds.token, creds.secret) == (FAKE_TOKEN, FAKE_SECRET)


def test_unrelated_lines_are_discarded_while_parsing() -> None:
    wanted = frozenset(TOKEN_NAMES + SECRET_NAMES)
    assert parse_keys(REAL_LAYOUT, wanted) == {
        "GROWW_TOP_TOKEN": FAKE_TOKEN,
        "GROWW_TOP_QR_CODE": FAKE_SECRET,
    }


def test_tolerates_colons_quotes_export_comments_and_crlf(tmp_path: Path) -> None:
    text = (
        "# my Groww keys\r\n"
        f"export GROWW_TOTP_TOKEN: '{FAKE_TOKEN}'\r\n"
        "\r\n"
        f'groww totp secret = "{FAKE_SECRET}"\r\n'
    )
    path = tmp_path / "keys.txt"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())  # UTF-8 BOM, as Windows editors write it
    path.chmod(0o600)
    creds = load_credentials(path, NO_ENV)
    assert (creds.token, creds.secret) == (FAKE_TOKEN, FAKE_SECRET)


def test_secret_is_normalised(tmp_path: Path) -> None:
    grouped = "gezd gnbv-gy3t qojq gezd gnbv gy3t qojq"
    text = f"GROWW_TOTP_TOKEN={FAKE_TOKEN}\nGROWW_TOTP_SECRET={grouped}\n"
    assert load_credentials(write_keys(tmp_path, text), NO_ENV).secret == FAKE_SECRET


def test_environment_overrides_the_file(tmp_path: Path) -> None:
    env = {"GROWW_TOTP_TOKEN": "token-from-the-environment"}
    creds = load_credentials(write_keys(tmp_path, REAL_LAYOUT), env)
    assert creds.token == "token-from-the-environment"
    assert creds.secret == FAKE_SECRET


def test_environment_alone_is_enough_when_there_is_no_file(tmp_path: Path) -> None:
    env = {"GROWW_TOTP_TOKEN": FAKE_TOKEN, "GROWW_TOTP_SECRET": FAKE_SECRET}
    creds = load_credentials(tmp_path / "missing.txt", env)
    assert (creds.token, creds.secret) == (FAKE_TOKEN, FAKE_SECRET)


def test_missing_file_is_explained_without_leaking_anything(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError, match="not found") as info:
        load_credentials(tmp_path / "missing.txt", NO_ENV)
    assert str(tmp_path) in info.value.message
    assert TOKEN_NAMES[0] in info.value.message


def test_missing_token(tmp_path: Path) -> None:
    path = write_keys(tmp_path, f"GROWW_TOTP_SECRET={FAKE_SECRET}\n")
    with pytest.raises(CredentialsError, match="No TOTP token"):
        load_credentials(path, NO_ENV)


def test_missing_secret(tmp_path: Path) -> None:
    path = write_keys(tmp_path, f"GROWW_TOTP_TOKEN={FAKE_TOKEN}\n")
    with pytest.raises(CredentialsError, match="No TOTP secret"):
        load_credentials(path, NO_ENV)


def test_the_api_key_pair_alone_is_not_mistaken_for_totp_credentials(tmp_path: Path) -> None:
    path = write_keys(tmp_path, "GROW_API_KEY=abc\nGROW_API_SECRET=def\n")
    with pytest.raises(CredentialsError, match="No TOTP token"):
        load_credentials(path, NO_ENV)


@pytest.mark.parametrize("bad_secret", ["not base32 at all!", "1111111111111111", "ABC"])
def test_secret_must_be_base32_and_is_never_echoed(tmp_path: Path, bad_secret: str) -> None:
    text = f"GROWW_TOTP_TOKEN={FAKE_TOKEN}\nGROWW_TOTP_SECRET={bad_secret}\n"
    with pytest.raises(CredentialsError, match="not valid base32") as info:
        load_credentials(write_keys(tmp_path, text), NO_ENV)
    assert bad_secret not in info.value.message


def test_token_must_not_contain_whitespace(tmp_path: Path) -> None:
    text = f"GROWW_TOTP_TOKEN=two words\nGROWW_TOTP_SECRET={FAKE_SECRET}\n"
    with pytest.raises(CredentialsError, match="whitespace"):
        load_credentials(write_keys(tmp_path, text), NO_ENV)


def test_repr_never_shows_values() -> None:
    text = repr(TotpCredentials(token=FAKE_TOKEN, secret=FAKE_SECRET))
    assert FAKE_TOKEN not in text
    assert FAKE_SECRET not in text


def test_warns_when_the_file_is_readable_by_other_users(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_keys(tmp_path, REAL_LAYOUT)
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="app.credentials"):
        load_credentials(path, NO_ENV)
    assert "chmod 600" in caplog.text


def test_stays_quiet_when_the_file_is_private(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.credentials"):
        load_credentials(write_keys(tmp_path, REAL_LAYOUT), NO_ENV)
    assert caplog.text == ""
