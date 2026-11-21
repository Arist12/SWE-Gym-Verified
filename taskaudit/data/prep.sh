set -eu
repo={repo}; base={base}; sanitize={sanitize}; tok={tok}; harden={harden}
git config --global --add safe.directory '*'
cd "$repo"
head=$(git rev-parse HEAD)
# SWE-bench images add one "SWE-bench" setup commit (file modes) on top of the base commit.
git merge-base --is-ancestor "$base" "$head" || {{ echo "__PREP_HEAD_MISMATCH__ $head"; exit 90; }}
setup_commits=$(git rev-list --count "$base..$head")
dirty=$(git diff HEAD --name-only)
untracked=$(git ls-files --others --exclude-standard)
if [ "$sanitize" = 1 ]; then
  scratch=$(mktemp -d /.prep-git.XXXXXX)
  # single-commit history at the base commit: a depth-(setup_commits+1) shallow clone of HEAD keeps HEAD..base and
  # grafts base as a root, so no commit before the base stays reachable (a later commit or the inverse of the fix
  # cannot be read from the history). The nearest past tag is re-pointed at the base so `git describe` still resolves
  # a version (pandas' versioneer otherwise reads 0+untagged and its test runs fail at conftest import). HEAD's sha is
  # unchanged (base == HEAD for SWE-Gym images, one setup commit above it for SWE-bench images).
  depth=$((setup_commits + 1))
  tag=$(git describe --tags --abbrev=0 "$head" 2>/dev/null || true)
  git update-ref "refs/heads/prep-snap-$tok" "$head"
  git -c pack.threads=1 -c index.threads=1 clone -q --no-local --no-checkout --no-tags --depth "$depth" \
    --single-branch --branch "prep-snap-$tok" "file://$repo" "$scratch/repo"
  git update-ref -d "refs/heads/prep-snap-$tok"
  mv .git "$scratch/orig.git"
  mv "$scratch/repo/.git" .git
  # submodule checkouts point into .git/modules; their histories are kept as shipped
  if [ -d "$scratch/orig.git/modules" ]; then cp -a "$scratch/orig.git/modules" .git/modules; fi
  rm -rf "$scratch"
  git remote remove origin
  git branch -M main
  git reset -q
  if [ -n "$tag" ]; then git tag -f "$tag" "$base" >/dev/null; fi
  [ "$(git rev-parse HEAD)" = "$head" ] || exit 91
  n=$(git rev-list HEAD --count)
  [ "$n" = "$depth" ] || exit 92
  [ "$(git rev-list --all --count)" = "$n" ] || exit 92
  [ "$(git cat-file --batch-all-objects --batch-check='%(objecttype)' | grep -c '^commit$')" = "$n" ] || exit 93
  # fail closed: the base commit must be present and its parent (if any) unreachable, i.e. no pre-base ancestry
  git cat-file -e "$base^{{commit}}" || exit 97
  bp=$(git rev-parse -q --verify "$base^1" 2>/dev/null || true)
  [ -z "$bp" ] || ! git cat-file -e "$bp^{{commit}}" 2>/dev/null || exit 97
  echo "__PREP_COMMITS__ $n $setup_commits $(git tag | wc -l)"
  echo "__PREP_HISTORY__ depth1_at_base $base $n ${{tag:-none}}"
fi
if [ -n "$dirty" ]; then printf '%s\n' "$dirty" | xargs -r git update-index --skip-worktree; fi
if [ -n "$untracked" ]; then printf '%s\n' "$untracked" | sed 's|^|/|' >> .git/info/exclude; fi
[ -z "$(git status --porcelain)" ] || {{ git status --porcelain | head; exit 94; }}
echo "__PREP_HIDDEN__ $(printf '%s\n' "$dirty" | grep -c . || true) $(printf '%s\n' "$untracked" | grep -c . || true)"
if [ "$harden" -ge 1 ]; then
  py=
  for c in python3 /opt/miniconda3/bin/python3 /usr/bin/python3; do
    if "$c" -c "" 2>/dev/null; then py=$c; break; fi
  done
  [ -n "$py" ] || {{ echo __PREP_COPIES_NO_PYTHON__; exit 95; }}
  rep=$("$py" -I /root/.prep_copies.py clean "$repo") || {{ echo "__PREP_COPIES__ $rep"; exit 96; }}
  echo "__PREP_COPIES__ $rep"
fi
echo __PREP_OK__
