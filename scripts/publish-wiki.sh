#!/usr/bin/env bash
set -euo pipefail

# Publish the version-controlled Markdown under docs/wiki to the GitHub Wiki
# repository. Run this with Git credentials that can push to the repository.
#
# Optional overrides:
#   WIKI_REMOTE_URL  Full Git URL for the wiki repository.
#   WIKI_BRANCH      Wiki branch to push (default: master).
#
# Example:
#   WIKI_REMOTE_URL=git@github.com:amirss/ninjatech-deployment-lab.wiki.git \
#     bash scripts/publish-wiki.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${ROOT_DIR}/docs/wiki"
REPOSITORY_SLUG="${GITHUB_REPOSITORY:-amirss/ninjatech-deployment-lab}"
WIKI_REMOTE_URL="${WIKI_REMOTE_URL:-https://github.com/${REPOSITORY_SLUG}.wiki.git}"
WIKI_BRANCH="${WIKI_BRANCH:-master}"

MANAGED_PAGES=(
  "Home.md"
  "Architecture-Overview.md"
  "Reliable-Worker-Execution.md"
  "Failure-Semantics.md"
  "Enterprise-Integration-Design.md"
  "FDE-Walkthrough.md"
  "Roadmap.md"
  "_Sidebar.md"
)

for page in "${MANAGED_PAGES[@]}"; do
  if [[ ! -f "${SOURCE_DIR}/${page}" ]]; then
    echo "Missing managed wiki source: ${SOURCE_DIR}/${page}" >&2
    exit 1
  fi
done

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

WIKI_DIR="${TMP_DIR}/wiki"

echo "Cloning ${WIKI_REMOTE_URL}..."
git clone --branch "${WIKI_BRANCH}" --single-branch "${WIKI_REMOTE_URL}" "${WIKI_DIR}"

for page in "${MANAGED_PAGES[@]}"; do
  cp "${SOURCE_DIR}/${page}" "${WIKI_DIR}/${page}"
done

cd "${WIKI_DIR}"

if git diff --quiet -- "${MANAGED_PAGES[@]}" && \
   [[ -z "$(git status --short --untracked-files=all -- "${MANAGED_PAGES[@]}")" ]]; then
  echo "Wiki is already up to date."
  exit 0
fi

git add -- "${MANAGED_PAGES[@]}"
git commit -m "docs: publish deployment lab wiki"
git push origin "${WIKI_BRANCH}"

echo "Wiki published successfully."
