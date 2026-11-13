from pathlib import Path

from app.auth import TokenStore


def test_token_store_parses_and_authenticates(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text(
        "# comment\n"
        "alice secret-a Alice Phone\n"
        "bob secret-b\n",
        encoding="utf-8",
    )
    store = TokenStore(tokens)
    alice = store.authenticate("Bearer secret-a")
    assert alice.token_id == "alice"
    assert alice.label == "Alice Phone"
    bob = store.authenticate("bearer secret-b")
    assert bob.token_id == "bob"


def test_token_store_rejects_bad_token(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("alice secret-a\n", encoding="utf-8")
    store = TokenStore(tokens)
    try:
        store.authenticate("Bearer nope")
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_token_store_hot_reloads(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("alice secret-a\n", encoding="utf-8")
    store = TokenStore(tokens)
    tokens.write_text("alice secret-a\nbob secret-b\n", encoding="utf-8")
    bob = store.authenticate("Bearer secret-b")
    assert bob.token_id == "bob"
