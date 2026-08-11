#!/bin/sh
set -u

REAL_CRAWL=/usr/games/crawl
PROBE_OUT=/tmp/dcss-builddb-probe.out
PROBE_ERR=/tmp/dcss-builddb-probe.err

printf '[probe] wrapper pid=%s HOME=%s TERM=%s LANG=%s\n' "$$" "${HOME:-}" "${TERM:-}" "${LANG:-}" >&2
printf '[probe] crawl_version=' >&2
"$REAL_CRAWL" -version 2>&1 | head -n 1 >&2 || true
printf '[probe] debian_package=' >&2
dpkg-query -W -f='${Version}\n' crawl 2>/dev/null >&2 || printf '<unknown>\n' >&2

start=$(date +%s)
"$REAL_CRAWL" -builddb >"$PROBE_OUT" 2>"$PROBE_ERR"
rc=$?
end=$(date +%s)
printf '[probe] runtime_builddb rc=%s elapsed=%ss\n' "$rc" "$((end - start))" >&2
if [ -s "$PROBE_OUT" ]; then
    printf '[probe] runtime_builddb_stdout_tail:\n' >&2
    tail -n 20 "$PROBE_OUT" >&2 || true
fi
if [ -s "$PROBE_ERR" ]; then
    printf '[probe] runtime_builddb_stderr_tail:\n' >&2
    tail -n 20 "$PROBE_ERR" >&2 || true
fi

if [ "$rc" -ne 0 ]; then
    printf '[probe] refusing normal startup because runtime -builddb failed\n' >&2
    exit "$rc"
fi

printf '[probe] runtime_builddb passed; exec normal crawl\n' >&2
exec "$REAL_CRAWL" "$@"
