#!/usr/bin/env python3
"""
Gera CHANGELOG.md e RELEASE_NOTES.md a partir do histórico git.

Pensado para o fluxo gitflow com branch `release`:

  - O script roda na branch `release` (HEAD), ANTES do merge na main.
  - O intervalo de commits é "tudo que está na release e ainda não foi
    publicado em nenhuma tag estável anterior".
  - Não usa a API do GitHub: `git flow feature finish` faz merge local,
    sem Pull Request, então a fonte da verdade é o próprio histórico.
  - Cada merge de uma branch de trabalho (feature/, fix/, fix/review/,
    hotfix/...) vira um item do changelog, com os commits que ele trouxe.
  - Merges entre branches de integração (develop, release, main) são
    ignorados.

Variáveis de ambiente:
  RELEASE_VERSION   versão de PRD (ex.: 1.1.1). Alternativa: 1º argumento.
  RELEASE_DATE      opcional, formato DD/MM/AAAA (padrão: hoje).
  JIRA_BASE_URL     opcional, ex.: https://empresa.atlassian.net/browse
  REPOSITORY_URL    opcional, para o link "Compare Changes".
                    Se ausente, usa GITHUB_SERVER_URL/GITHUB_REPOSITORY.
  INTEGRATION_BRANCHES  opcional, lista separada por vírgula.
                    Padrão: main,master,develop,release
"""

import os
import re
import subprocess
import sys
from datetime import datetime

CHANGELOG_FILE = "CHANGELOG.md"
RELEASE_NOTES_FILE = "RELEASE_NOTES.md"

FIELD_SEP = "\x1f"
LOG_FORMAT = FIELD_SEP.join(["%H", "%P", "%cI", "%s"])

# Ignora commits gerados pela própria automação.
IGNORED_TITLE_PREFIXES = ("docs(changelog)",)

JIRA_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")

# Formas de mensagem de merge que o git / gitflow / GitHub geram.
MERGE_PATTERNS = [
    # Merge branch 'feature/X' into develop
    re.compile(r"^Merge branch '(?P<branch>[^']+)'"),
    # Merge remote-tracking branch 'origin/feature/X'
    re.compile(r"^Merge remote-tracking branch '(?:[^/']+/)?(?P<branch>[^']+)'"),
    # Merge pull request #12 from owner/feature/X
    re.compile(r"^Merge pull request #\d+ from [^/\s]+/(?P<branch>\S+)"),
]

# (prefixo da branch, título da seção). A ordem define a ordem no changelog.
SECTIONS = [
    ("feature/", "Funcionalidades"),
    ("fix/review/", "Correções de homologação"),
    ("fix/", "Correções"),
    ("bugfix/", "Correções"),
    ("hotfix/", "Correções emergenciais (hotfix)"),
]
OTHER_SECTION = "Outras alterações"


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------

def git(*args):
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
    ).strip()


def git_log(*args):
    """Retorna commits (mais novos primeiro) como lista de dicts."""
    output = git("log", f"--format={LOG_FORMAT}", *args)
    commits = []

    for line in output.splitlines():
        if not line:
            continue

        sha, parents, date, title = line.split(FIELD_SEP, 3)
        commits.append({
            "sha": sha,
            "parents": parents.split(),
            "date": datetime.fromisoformat(date),
            "title": title,
        })

    return commits


# --------------------------------------------------------------------------
# Versões / tags
# --------------------------------------------------------------------------

def parse_version(tag):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(int(n) for n in match.groups()) if match else None


def get_released_tags(version):
    """
    Tags estáveis (X.Y.Z, com ou sem 'v') anteriores à versão atual,
    da mais nova para a mais antiga. Tags de pré-release são ignoradas.
    """
    current = parse_version(version)

    if current is None:
        raise ValueError(
            f"Versão inválida: '{version}'. Use o formato X.Y.Z"
        )

    tags = []

    for tag in git("tag", "--list").splitlines():
        parsed = parse_version(tag)

        if parsed is not None and parsed < current:
            tags.append((parsed, tag))

    tags.sort(reverse=True)

    return [tag for _, tag in tags]


# --------------------------------------------------------------------------
# Classificação
# --------------------------------------------------------------------------

def integration_branches():
    raw = os.environ.get(
        "INTEGRATION_BRANCHES",
        "main,master,develop,release"
    )

    return {item.strip() for item in raw.split(",") if item.strip()}


def is_integration_branch(name):
    return name in integration_branches() or name.startswith("release/")


def source_branch(title):
    """Nome da branch de origem de um commit de merge, ou None."""
    for pattern in MERGE_PATTERNS:
        match = pattern.match(title)

        if match:
            return match.group("branch")

    return None


def section_for(branch_name):
    name = (branch_name or "").lower()

    for prefix, title in SECTIONS:
        if name.startswith(prefix):
            return title

    return OTHER_SECTION


def card_key(*texts):
    for text in texts:
        match = JIRA_KEY.search(text or "")

        if match:
            return match.group(0)

    return None


def is_noise(commit):
    title = commit["title"]

    return (
        len(commit["parents"]) > 1
        or title.startswith("Merge ")
        or title.startswith(IGNORED_TITLE_PREFIXES)
    )


# --------------------------------------------------------------------------
# Coleta
# --------------------------------------------------------------------------

def collect_changes(released_tags):
    """
    Retorna {chave: item}, onde cada item representa uma branch de trabalho
    (ou um grupo de commits sem branch identificável).
    """
    exclusions = [f"^{tag}" for tag in released_tags]

    # Tudo que está na release e não saiu em nenhuma tag anterior.
    # Excluir TODAS as tags anteriores evita repetir hotfix já publicados,
    # mesmo quando a tag do hotfix não é ancestral direta da release.
    release_commits = list(reversed(git_log("HEAD", *exclusions)))

    items = {}
    assigned = set()

    def add(key, branch, commits):
        item = items.setdefault(key, {
            "branch": branch,
            "card": card_key(branch, *(c["title"] for c in commits)),
            "commits": {},
            "date": None,
        })

        for commit in commits:
            if is_noise(commit):
                continue

            item["commits"][commit["sha"]] = commit

            if item["date"] is None or commit["date"] > item["date"]:
                item["date"] = commit["date"]

    # 1) Merges de branches de trabalho: feature, fix, fix/review, hotfix...
    for merge in release_commits:
        if len(merge["parents"]) < 2:
            continue

        branch = source_branch(merge["title"])

        if not branch or is_integration_branch(branch):
            continue

        first_parent, second_parent = merge["parents"][:2]

        # Commits que o merge trouxe, descontando o que já existia
        # no destino (ex.: develop mergeada na feature no passo 3)
        # e o que já foi publicado em tags anteriores.
        brought = git_log(
            f"{first_parent}..{second_parent}",
            "--no-merges",
            *exclusions
        )

        add(branch, branch, list(reversed(brought)))
        assigned.update(c["sha"] for c in brought)

    # 2) Commits que não vieram de merge identificável
    #    (squash merge, commit direto, rebase...).
    orphans = [
        c for c in release_commits
        if c["sha"] not in assigned and not is_noise(c)
    ]

    for commit in orphans:
        key = card_key(commit["title"])
        group = f"__orphan__{key or 'outros'}"

        add(group, None, [commit])

        if key:
            items[group]["card"] = key

    # Remove itens que ficaram vazios (ex.: só tinham merges).
    return {k: v for k, v in items.items() if v["commits"]}


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def jira_link(card):
    base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")

    if base:
        return f"[{card}]({base}/{card})"

    return card


def format_item(item):
    parts = []

    if item["card"]:
        parts.append(f"**{jira_link(item['card'])}**")

    if item["branch"]:
        parts.append(f"`{item['branch']}`")

    parts.append(item["date"].strftime("%d/%m/%Y"))

    lines = [f"- {' · '.join(parts)}"]

    commits = sorted(item["commits"].values(), key=lambda c: c["date"])

    for commit in commits:
        lines.append(f"  - {commit['title']}")

    return lines


def generate_markdown(version, release_date, items, previous_tag, repo_url):
    lines = [f"## {version} - {release_date}", ""]

    if not items:
        lines.extend(["Nenhuma alteração identificada nesta release.", ""])

    grouped = {}

    for item in items.values():
        grouped.setdefault(section_for(item["branch"]), []).append(item)

    ordered_titles = []

    for _, title in SECTIONS:
        if title not in ordered_titles:
            ordered_titles.append(title)

    ordered_titles.append(OTHER_SECTION)

    for title in ordered_titles:
        section_items = grouped.get(title)

        if not section_items:
            continue

        lines.extend([f"### {title}", ""])

        for item in sorted(
            section_items,
            key=lambda i: i["date"],
            reverse=True
        ):
            lines.extend(format_item(item))

        lines.append("")

    if previous_tag and repo_url:
        lines.extend([
            f"[Compare Changes]({repo_url}/compare/{previous_tag}...{version})",
            ""
        ])

    return "\n".join(lines)


def update_changelog(version, release_content):
    """Insere a versão no TOPO do arquivo, substituindo se já existir."""
    lines = []

    if os.path.exists(CHANGELOG_FILE):
        with open(CHANGELOG_FILE, encoding="utf-8") as file:
            lines = file.read().splitlines()

    if lines and lines[0].strip() == "# Changelog":
        lines = lines[1:]

    kept = []
    skipping = False

    for line in lines:
        if line.startswith("## "):
            heading = line[3:].split(" - ")[0].strip()
            skipping = heading == version

        if not skipping:
            kept.append(line)

    body = "\n".join(kept).strip()

    updated = "# Changelog\n\n" + release_content.strip() + "\n"

    if body:
        updated += "\n" + body + "\n"

    with open(CHANGELOG_FILE, "w", encoding="utf-8") as file:
        file.write(updated)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def repository_url():
    explicit = os.environ.get("REPOSITORY_URL")

    if explicit:
        return explicit.rstrip("/")

    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")

    if server and repository:
        return f"{server}/{repository}"

    return None


def main():
    version = (
        os.environ.get("RELEASE_VERSION")
        or (sys.argv[1] if len(sys.argv) > 1 else None)
    )

    if not version:
        raise ValueError(
            "Informe a versão via RELEASE_VERSION ou como argumento."
        )

    release_date = (
        os.environ.get("RELEASE_DATE")
        or datetime.now().strftime("%d/%m/%Y")
    )

    print(f"Gerando changelog da versão {version}")

    released_tags = get_released_tags(version)
    previous_tag = released_tags[0] if released_tags else None

    print(f"Tag anterior: {previous_tag or '(nenhuma)'}")

    items = collect_changes(released_tags)

    content = generate_markdown(
        version,
        release_date,
        items,
        previous_tag,
        repository_url()
    )

    update_changelog(version, content)

    with open(RELEASE_NOTES_FILE, "w", encoding="utf-8") as file:
        file.write(content)

    print("CHANGELOG.md e RELEASE_NOTES.md atualizados com sucesso!")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Erro na geração do changelog: {error}")
        sys.exit(1)
