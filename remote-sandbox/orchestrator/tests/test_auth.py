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


def test_token_store_revokes_removed_secret(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("alice secret-a\nbob secret-b\n", encoding="utf-8")
    store = TokenStore(tokens)
    assert store.authenticate("Bearer secret-b").token_id == "bob"
    tokens.write_text("alice secret-a\n", encoding="utf-8")
    try:
        store.authenticate("Bearer secret-b")
        assert False, "expected PermissionError after revoke"
    except PermissionError:
        pass
    assert store.authenticate("Bearer secret-a").token_id == "alice"


def test_token_store_missing_file_revokes_all(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("alice secret-a\n", encoding="utf-8")
    store = TokenStore(tokens)
    assert store.authenticate("Bearer secret-a").token_id == "alice"
    tokens.unlink()
    try:
        store.authenticate("Bearer secret-a")
        assert False, "expected PermissionError when tokens file is missing"
    except PermissionError:
        pass
