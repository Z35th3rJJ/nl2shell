from core.ssh_config import load_ssh_hosts


def test_load_ssh_hosts_ignores_wildcards_and_comments(tmp_path):
    config = tmp_path / "config"
    config.write_text("""# personal hosts
Host web db *.internal
  HostName example.com
Host web
Host\t?
""", encoding="utf-8")

    assert load_ssh_hosts(config) == ["web", "db"]
