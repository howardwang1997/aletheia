from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

VULNERABLE_DEPLOYMENT_PINS = (
    "cryptography==48.0.0",
    "pydantic-settings==2.14.1",
    "torch==2.12.0",
)


def test_python_environment_retains_audited_security_floors() -> None:
    environment = (ROOT / "environment.yml").read_text(encoding="utf-8")

    for requirement in (
        "pip>=26.2",
        "setuptools>=83.0.0",
        "cryptography>=50.0.0",
        "pillow>=12.3.0",
        "mcp>=1.28.1,<2",
        "starlette>=1.3.1",
        "python-multipart>=0.0.31",
        "pydantic-settings>=2.14.2",
        "torch>=2.13.0",
        "h2>=4.4.1",
        "tornado>=6.5.7",
        "pyasn1>=0.6.4",
    ):
        assert requirement in environment


def test_deployment_images_do_not_reintroduce_audited_vulnerable_pins() -> None:
    sources = {
        path.relative_to(ROOT): path.read_text(encoding="utf-8")
        for path in (
            ROOT / "docker/discoveryworld-candidate.Dockerfile",
            ROOT / "docker/discoveryworld.Dockerfile",
            ROOT / "docker/evaluator-agent.Dockerfile",
            ROOT / "docker/legacy-evaluation-runtime.Dockerfile",
            ROOT / "docker/qualification-runtime.Dockerfile",
            ROOT / "docker/sandbox.Dockerfile",
            ROOT / "docker/sandbox-sota.Dockerfile",
            ROOT / "docker/simulation/ase-emt.Dockerfile",
            ROOT / "configs/capabilities/legacy-evaluation-runtime-constraints-v1.txt",
        )
    }

    for path, source in sources.items():
        for vulnerable_pin in VULNERABLE_DEPLOYMENT_PINS:
            assert vulnerable_pin not in source, f"{path} retained {vulnerable_pin}"
        if path.suffix == ".Dockerfile":
            assert '"pip==26.2.1"' in source
            assert '"setuptools==84.0.0"' in source

    assert '"cryptography==50.0.0"' in sources[Path("docker/legacy-evaluation-runtime.Dockerfile")]
    assert '"cryptography==50.0.0"' in sources[Path("docker/qualification-runtime.Dockerfile")]
    assert (
        "cryptography==50.0.0"
        in sources[Path("configs/capabilities/legacy-evaluation-runtime-constraints-v1.txt")]
    )
    assert '"pydantic-settings==2.14.2"' in sources[Path("docker/sandbox.Dockerfile")]
    assert '"pydantic-settings==2.14.2"' in sources[Path("docker/sandbox-sota.Dockerfile")]
    assert '"torch==2.13.0"' in sources[Path("docker/sandbox-sota.Dockerfile")]
