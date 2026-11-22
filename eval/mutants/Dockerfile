# The labeling environment exactly as README.md describes it and nothing more:
# the pinned image, every package in requirements.lock, sqlglot at the pinned
# commit. The mutants in this directory are generated and labeled in it. It is
# never part of the scored image, and the scored image never sees this directory.
#
#   docker build --platform linux/amd64 -f eval/mutants/Dockerfile -t blast-radius-label .
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

COPY requirements.lock /tool/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore -r /tool/requirements.lock

COPY tools/build_map/fetch_sqlglot.py /tool/fetch_sqlglot.py
RUN python /tool/fetch_sqlglot.py "$SQLGLOT_COMMIT" /sqlglot

ENV SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    SQLGLOT_COMMIT=$SQLGLOT_COMMIT
WORKDIR /sqlglot
