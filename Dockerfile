# The scorer builds this on linux/amd64 and runs it with no network, a
# read-only disk and user nobody. The image holds the selector, its map, the
# test runner, and what the runner needs to run sqlglot's own tests: the
# packages the tests were labeled with and sqlglot at the pinned commit. Every
# file from this repository is copied by name.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

# Every package of the environment the tests were labeled in (README.md), from PyPI.
COPY --chmod=644 requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore -r /build/requirements.lock

# The directories come first, with mode 755. COPY --chmod=644 would otherwise
# also give 644 to the directories it creates, and nobody could not enter them.
RUN mkdir -p /app/map && chmod 755 /app /app/map
COPY --chmod=644 map/index.json map/files.bin map/sets.bin map/sources.bin /app/map/

# sqlglot at the pinned commit, from its own repository. The build stops unless
# every sqlglot/**/*.py is byte for byte the file the map was built from. Only
# sqlglot/ and tests/ stay, compiled, in /app/pinned.
COPY --chmod=644 tools/build_map/fetch_sqlglot.py tools/image/check_pinned.py /build/
RUN python /build/fetch_sqlglot.py "$SQLGLOT_COMMIT" /build/checkout \
 && python /build/check_pinned.py /build/checkout /app/pinned /app/map \
 && rm -rf /build/checkout /tmp/unpack

COPY --chmod=644 selector.py runner.py /app/
# README.md: the tests were labeled with this set, so the executor tests run.
ENV SKIP_INTEGRATION=0
CMD ["python", "/app/selector.py"]
