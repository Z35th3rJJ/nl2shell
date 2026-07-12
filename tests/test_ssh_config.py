from core.ssh_config import load_ssh_hosts, load_ssh_profiles


def test_load_ssh_hosts_ignores_wildcards_and_comments(tmp_path):
    config = tmp_path / "config"
    config.write_text("""# personal hosts
Host web db *.internal
  HostName example.com
Host web
Host\t?
""", encoding="utf-8")

    assert load_ssh_hosts(config) == ["web", "db"]


def test_load_ssh_profiles_reads_metadata_without_reading_key(tmp_path):
    config = tmp_path / "config"
    config.write_text("""Host build
  HostName build.example.com
  User deploy
  Port 2222
  IdentityFile ~/.ssh/build_key
Host *.ignored
  User nobody
""", encoding="utf-8")

    profile = load_ssh_profiles(config)[0]

    assert profile.alias == "build"
    assert profile.hostname == "build.example.com"
    assert profile.user == "deploy"
    assert profile.port == "2222"
    assert profile.identity_file == "~/.ssh/build_key"
