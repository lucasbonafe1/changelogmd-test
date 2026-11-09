
import json
import os
import subprocess
import sys

from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError


CHANGELOG_FILE = "CHANGELOG.md"
RELEASE_NOTES_FILE = "RELEASE_NOTES.md"


def git(*args):
    return subprocess.check_output(
        ["git", *args],
        text=True
    ).strip()


def parse_date(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def api_get(endpoint, params=None):
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]

    query = f"?{urlencode(params)}" if params else ""

    url = (
        f"https://api.github.com/repos/"
        f"{repository}/{endpoint}{query}"
    )

    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
    )

    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except HTTPError as error:
        raise RuntimeError(
            f"GitHub API HTTP {error.code}: "
            f"{error.read().decode('utf-8')}"
        )


def get_all_pages(endpoint, params=None):
    results = []
    page = 1

    while True:
        current_params = {
            **(params or {}),
            "per_page": 100,
            "page": page
        }

        data = api_get(endpoint, current_params)

        if not data:
            break

        results.extend(data)

        if len(data) < 100:
            break

        page += 1

    return results


def get_previous_tag(current_tag):
    tags = git(
        "tag",
        "--sort=-version:refname"
    ).splitlines()

    if current_tag not in tags:
        raise RuntimeError(
            f"Tag não encontrada: {current_tag}"
        )

    index = tags.index(current_tag)

    if index + 1 >= len(tags):
        return None

    return tags[index + 1]


def get_release_commits(previous_tag, current_tag):
    revision = (
        f"{previous_tag}..{current_tag}"
        if previous_tag
        else current_tag
    )

    return set(
        git("rev-list", revision).splitlines()
    )


def get_pull_request_commits(number):
    return get_all_pages(
        f"pulls/{number}/commits"
    )


def get_merged_pull_requests(target_branch):
    pull_requests = get_all_pages(
        "pulls",
        {
            "state": "closed",
            "base": target_branch,
            "sort": "updated",
            "direction": "desc"
        }
    )

    return [
        pr for pr in pull_requests
        if pr.get("merged_at")
    ]


def collect_pull_requests(release_commits):
    default_branch = os.environ.get(
        "DEFAULT_BRANCH",
        "main"
    )

    develop_branch = os.environ.get(
        "DEVELOP_BRANCH",
        "develop"
    )

    target_branches = {
        develop_branch,
        default_branch
    }

    relevant_prs = {}

    for target in target_branches:
        pull_requests = get_merged_pull_requests(
            target
        )

        for pr in pull_requests:
            number = pr["number"]

            if number in relevant_prs:
                continue

            commits = get_pull_request_commits(number)

            merge_sha = pr.get("merge_commit_sha")

            relevant = (
                merge_sha in release_commits
                or any(
                    commit["sha"] in release_commits
                    for commit in commits
                )
            )

            if relevant:
                relevant_prs[number] = {
                    "branch": pr["head"]["ref"],
                    "commits": commits
                }

    return relevant_prs


def collect_changes(relevant_prs, release_commits):
    branches = {}

    for pr in relevant_prs.values():
        branch_name = pr["branch"]

        # Não documentar branches de integração
        if branch_name in {
            os.environ.get("DEFAULT_BRANCH", "main"),
            os.environ.get("DEVELOP_BRANCH", "develop")
        }:
            continue

        if branch_name not in branches:
            branches[branch_name] = {
                "date": None,
                "commits": {}
            }

        branch = branches[branch_name]

        for commit in pr["commits"]:
            sha = commit["sha"]

            # Considera somente commits da release.
            # Para squash merge, utiliza o SHA do merge
            # como critério de inclusão do PR.
            committed_at = parse_date(
                commit["commit"]["committer"]["date"]
            )

            title = (
                commit["commit"]["message"]
                .splitlines()[0]
            )

            branch["commits"][sha] = {
                "title": title,
                "date": committed_at
            }

            if (
                branch["date"] is None
                or committed_at > branch["date"]
            ):
                branch["date"] = committed_at

    return branches


def generate_markdown(
    version,
    branches,
    previous_tag,
    repository_url
):
    lines = [
        f"## {version}",
        ""
    ]

    if not branches:
        lines.extend([
            "Nenhuma alteração identificada nesta release.",
            ""
        ])

    sorted_branches = sorted(
        branches.items(),
        key=lambda item: item[1]["date"],
        reverse=True
    )

    for branch_name, data in sorted_branches:
        branch_date = data["date"].strftime("%d/%m/%Y")

        lines.append(
            f"### {branch_name} - {branch_date}"
        )

        commits = sorted(
            data["commits"].values(),
            key=lambda item: item["date"]
        )

        for commit in commits:
            lines.append(
                f"- {commit['title']}"
            )

        lines.append("")

    if previous_tag:
        compare_url = (
            f"{repository_url}/compare/"
            f"{previous_tag}...{version}"
        )

        lines.extend([
            f"[Compare Changes]({compare_url})",
            ""
        ])

    return "\n".join(lines)


def update_changelog(version, release_content):
    if os.path.exists(CHANGELOG_FILE):
        with open(
            CHANGELOG_FILE,
            encoding="utf-8"
        ) as file:
            content = file.read()
    else:
        content = "# Changelog\n"

    lines = content.splitlines()

    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line == f"## {version}"
        ),
        None
    )

    if start is not None:
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].startswith("## ")
            ),
            len(lines)
        )

        del lines[start:end]

    if not lines:
        lines = ["# Changelog"]

    if lines[0] != "# Changelog":
        lines.insert(0, "# Changelog")

    updated = (
        "\n".join(lines).rstrip()
        + "\n\n"
        + release_content.strip()
        + "\n"
    )

    with open(
        CHANGELOG_FILE,
        "w",
        encoding="utf-8"
    ) as file:
        file.write(updated)


def main():
    version = os.environ["RELEASE_TAG"]

    repository_url = (
        os.environ["GITHUB_SERVER_URL"]
        + "/"
        + os.environ["GITHUB_REPOSITORY"]
    )

    print(f"Gerando changelog da versão {version}")

    previous_tag = get_previous_tag(version)

    release_commits = get_release_commits(
        previous_tag,
        version
    )

    relevant_prs = collect_pull_requests(
        release_commits
    )

    branches = collect_changes(
        relevant_prs,
        release_commits
    )

    release_content = generate_markdown(
        version,
        branches,
        previous_tag,
        repository_url
    )

    update_changelog(
        version,
        release_content
    )

    with open(
        RELEASE_NOTES_FILE,
        "w",
        encoding="utf-8"
    ) as file:
        file.write(release_content)

    print("CHANGELOG.md atualizado com sucesso!")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Erro na geração do changelog: {error}")
        sys.exit(1)
