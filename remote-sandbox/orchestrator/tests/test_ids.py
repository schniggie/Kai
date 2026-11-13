from app.docker_mgr import container_name_for, sandbox_id_for, volume_name_for


def test_sandbox_id_stable_per_token_device() -> None:
    a = sandbox_id_for("alice", "iphone-1")
    b = sandbox_id_for("alice", "iphone-1")
    c = sandbox_id_for("alice", "iphone-2")
    d = sandbox_id_for("bob", "iphone-1")
    assert a == b
    assert a != c
    assert a != d
    assert a.startswith("sbx_")
    assert container_name_for(a) == f"kai-{a}"
    assert volume_name_for(a) == f"kai-{a}-data"
