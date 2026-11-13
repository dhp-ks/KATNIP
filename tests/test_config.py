from pathlib import Path

from katnip.config import load_config


def test_load_config_resolves_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_path = config_dir / "test.yaml"
    config_path.write_text(
        """
run:
  output_root: ../output
test:
  website: https://example.com/path
  location: ca
  journey: standard
vpn:
  enabled: true
  locations:
    CA:
      config_path: ../vpn/ca.conf
recording:
  enabled: false
mitm:
  enabled: false
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.test.location == "CA"
    assert config.run.output_root == (tmp_path / "output").resolve()
    assert config.vpn.locations["CA"].config_path == (tmp_path / "vpn" / "ca.conf").resolve()
