from pathlib import Path

import yaml

from readiness.__main__ import main
from readiness.compose import check_compose
from readiness.dockerfile import check_dockerfile, has_healthcheck, parse
from readiness.project import check_repo
from readiness.workflow import check_workflow


def rules(findings):
    return {f.rule for f in findings}


GOOD_DOCKERFILE = """\
FROM python:3.11-slim
RUN pip install --no-cache-dir flask
RUN useradd app
USER app
HEALTHCHECK CMD curl -f http://localhost/health
CMD ["python", "app.py"]
"""


def test_dockerfile_good_has_no_findings():
    assert check_dockerfile(GOOD_DOCKERFILE) == []


def test_dockerfile_bad_triggers_each_rule():
    bad = """\
FROM python
ENV API_TOKEN=abcd1234
ADD https://example.com/x.sh /x.sh
RUN curl https://example.com/i.sh | sh
RUN pip install flask
RUN apt-get update && apt-get install -y curl
CMD ["python", "app.py"]
"""
    assert rules(check_dockerfile(bad)) >= {"DF001", "DF002", "DF003", "DF004", "DF005", "DF006",
                                             "DF007", "DF009"}
    assert "DF001" in rules(check_dockerfile("FROM python:latest\nUSER app"))


def test_dockerfile_multistage_checks_the_final_stage_and_ignores_stage_refs():
    text = "FROM python:3.11 AS build\nRUN true\nFROM build\nUSER app\nHEALTHCHECK CMD true\n"
    assert check_dockerfile(text) == []
    assert has_healthcheck(text)
    assert "DF002" in rules(check_dockerfile("FROM a:1\nUSER app\nFROM b:1\nCMD x\n"))


def test_dockerfile_line_continuations_are_joined():
    assert parse("RUN a \\\n  && b\n")[0][2].split() == ["a", "&&", "b"]


GOOD_COMPOSE = """
services:
  api:
    build: .
    restart: unless-stopped
    mem_limit: 256m
    healthcheck: {test: ["CMD", "true"]}
    environment:
      TOKEN: ${API_TOKEN}
  db:
    image: postgres:16
    restart: always
    mem_limit: 512m
    healthcheck: {test: ["CMD", "true"]}
    ports: ["127.0.0.1:5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
    depends_on: []
volumes:
  pgdata: {}
"""


def test_compose_good_is_clean():
    assert check_compose(yaml.safe_load(GOOD_COMPOSE), env_example_keys={"API_TOKEN"}) == []


def test_compose_bad_triggers_each_rule():
    bad = yaml.safe_load("""
version: "3"
services:
  web:
    image: nginx:latest
    privileged: true
    environment: ["DB_PASSWORD=hunter2"]
    depends_on: [ghost]
    volumes: ["data:/d"]
  cache:
    image: redis:7
    ports: ["6379:6379"]
  broken: {}
""")
    got = rules(check_compose(bad, env_example_keys=set()))
    assert got >= {"DC001", "DC002", "DC003", "DC004", "DC005", "DC006", "DC007", "DC008",
                   "DC009", "DC011", "DC013"}
    uses_var = yaml.safe_load("services: {a: {image: 'x:1', environment: {K: '${NEW_VAR}'}}}")
    assert "DC010" in rules(check_compose(uses_var, env_example_keys=set()))
    with_default = yaml.safe_load("services: {a: {image: 'x:1', environment: {K: '${NEW_VAR:-1}'}}}")
    assert "DC010" not in rules(check_compose(with_default, env_example_keys=set()))


def test_compose_healthcheck_inherited_from_dockerfile():
    data = yaml.safe_load("services: {api: {build: ., restart: always, mem_limit: 1g}}")
    assert "DC003" in rules(check_compose(data))
    assert "DC003" not in rules(check_compose(data, dockerfile_healthy={".": True}))


GOOD_WORKFLOW = """
on: [push]
permissions:
  contents: read
jobs:
  t:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@0123456789012345678901234567890123456789
      - run: echo hi
"""


def test_workflow_good_is_clean_and_bad_triggers_rules():
    assert check_workflow(yaml.safe_load(GOOD_WORKFLOW), "w.yml") == []
    bad = yaml.safe_load("""
on:
  pull_request_target:
jobs:
  t:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@main
      - uses: actions/setup-python@v5
      - run: echo "${{ secrets.TOKEN }}"
      - run: echo "${{ github.event.pull_request.title }}"
""")
    assert rules(check_workflow(bad, "w.yml")) == {"GH001", "GH002", "GH003", "GH004", "GH005", "GH006"}
    severities = {f.rule + f.severity for f in check_workflow(bad, "w.yml")}
    assert "GH002high" in severities and "GH002low" in severities  # unpinned vs movable tag


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_repo_level_checks(tmp_path):
    root = _repo(tmp_path, {
        "Dockerfile": GOOD_DOCKERFILE,
        "docker-compose.yml": GOOD_COMPOSE,
        "config.py": 'GROQ = "gsk_' + "a" * 30 + '"\nAWS = "AKIA' + "B" * 16 + '"\n',
        "notes.txt": 'password = "placeholder_value_here_ok"\n',
    })
    got = rules(check_repo(root).findings)
    assert {"RP002", "RP003", "RP004"} <= got  # no .dockerignore, vars but no .env.example, secrets
    secrets = [f for f in check_repo(root).findings if f.rule == "RP004"]
    assert {f.file for f in secrets} == {"config.py"} and all(f.line for f in secrets)

    ok = _repo(tmp_path / "ok", {
        "Dockerfile": GOOD_DOCKERFILE, ".dockerignore": ".git\n.env\n", ".gitignore": ".env\n",
        ".env.example": "API_TOKEN=\n", "docker-compose.yml": GOOD_COMPOSE,
    })
    assert check_repo(ok).findings == []


def test_invalid_yaml_is_a_high_finding(tmp_path):
    root = _repo(tmp_path, {"docker-compose.yml": "services:\n  a: [unclosed\n"})
    f = check_repo(root).findings
    assert f[0].rule == "YAML001" and f[0].severity == "high"


def test_cli_exit_codes(tmp_path):
    bad = _repo(tmp_path / "bad", {"Dockerfile": "FROM python\n"})
    assert main([str(bad), "--json"]) == 1  # high finding (unpinned image)
    assert main([str(bad), "--fail-on", "never"]) == 0
    good = _repo(tmp_path / "good", {"Dockerfile": GOOD_DOCKERFILE, ".dockerignore": ".git\n.env\n"})
    assert main([str(good)]) == 0


def test_this_repo_has_no_high_or_medium_findings():
    root = Path(__file__).resolve().parent.parent
    bad = [f for f in check_repo(root).findings if f.severity in ("high", "medium")]
    assert bad == [], [str(f) for f in bad]
