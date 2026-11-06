#!/bin/bash
# Scan the working public tree, including archives without Git metadata.
# Exceptions are exact line hashes, confined to the reference deployment.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
ALLOW=scripts/leak-allowlist.txt
FILES=$(mktemp)
HITS=$(mktemp)
USED=$(mktemp)
trap 'rm -f "$FILES" "$HITS" "$USED"' EXIT HUP INT TERM
# Split spellings keep the gate's own source from matching its deny pattern.
NAMES='da''vid|lu''na|speed''lead|lead''2'
IDENTIFIERS='da''zheng|da''vidzhang|Based''Hardware|cl''2-|cl2''d-|my''space|br''amp|roll''eff|m[145]-'
PATHS='/Us''ers/|/Vol''umes/'
TAILNET='100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|fd7a:115c:''a1e0:'
# Substring matching also catches identifiers written in snake or camel case.
PATTERN="$NAMES|$IDENTIFIERS|$PATHS|$TAILNET"
[ -f "$ALLOW" ] || { echo 'leak-check: missing explicit allow-list' >&2; exit 1; }
while IFS= read -r entry || [ -n "$entry" ]; do
  case "$entry" in ''|'#'*) continue ;; esac
  path=${entry%%:*}; hash=${entry#*:}
  case "$path" in examples/omi/*) ;; *) echo "leak-check: invalid allow-list path: $path" >&2; exit 1 ;; esac
  case "$path" in *'/../'*|*'/./'*) echo 'leak-check: noncanonical allow-list path' >&2; exit 1 ;; esac
  [ "${#hash}" = 64 ] && ! printf '%s' "$hash" | grep -Eq '[^0-9a-f]' || {
    echo 'leak-check: invalid allow-list hash' >&2; exit 1;
  }
done < "$ALLOW"
find . -name .git -prune -o -type d \( -name .oss -o -name .coordkit -o -name .venv \
  -o -name __pycache__ -o -name '*.egg-info' -o -name build -o -name dist \) -prune \
  -o -type f -print | LC_ALL=C sort > "$FILES"
count=0
allowed=0
while IFS= read -r file; do
  count=$((count + 1))
  if grep -I -n -i -E "$PATTERN" "$file" > "$HITS"; then
    while IFS= read -r hit; do
      number=${hit%%:*}; line=${hit#*:}
      if command -v sha256sum >/dev/null 2>&1; then
        digest=$(printf '%s' "$line" | sha256sum)
      else
        digest=$(printf '%s' "$line" | shasum -a 256)
      fi
      key="${file#./}:${digest%% *}"
      if grep -Fqx -- "$key" "$ALLOW"; then
        allowed=$((allowed + 1)); printf '%s\n' "$key" >> "$USED"
      else
        printf '%s:%s:%s\n' "${file#./}" "$number" "$line" >&2
        echo 'leak-check: FAIL (unapproved hit)' >&2
        exit 1
      fi
    done < "$HITS"
  else
    rc=$?
    [ "$rc" = 1 ] || { echo "leak-check: grep failed: $file" >&2; exit "$rc"; }
  fi
done < "$FILES"
while IFS= read -r entry || [ -n "$entry" ]; do
  case "$entry" in ''|'#'*) continue ;; esac
  grep -Fqx -- "$entry" "$USED" || { echo "leak-check: stale allow-list entry: $entry" >&2; exit 1; }
done < "$ALLOW"
printf 'leak-check: PASS (%s files, %s intentional lines allowed; 0 unapproved hits)\n' "$count" "$allowed"
