from pathlib import Path


def test_runtime_dependencies_include_r2_client_dependency():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert '"boto3>=' in pyproject
