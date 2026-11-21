# The labeling environment as README.md describes it, plus coverage.py.
# Only used to generate map/. Never part of the scored image, and it never
# sees the sample commits or their expected results.
#
#   docker build --platform linux/amd64 -f tools/build_map/Dockerfile -t blast-radius-map .
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

COPY requirements.lock /tool/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore -r /tool/requirements.lock

# The one addition to the labeling environment.
RUN pip install --no-cache-dir --root-user-action=ignore coverage==7.17.4

COPY tools/build_map/fetch_sqlglot.py /tool/fetch_sqlglot.py
RUN python /tool/fetch_sqlglot.py "$SQLGLOT_COMMIT" /sqlglot

# The builder itself, last so editing it does not redo the layers above.
COPY tools/build_map /tool

# /tool/site holds sitecustomize.py, which stays inert unless asked (see there).
ENV PYTHONPATH=/tool/site:/sqlglot \
    SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    SQLGLOT_COMMIT=$SQLGLOT_COMMIT
WORKDIR /sqlglot
