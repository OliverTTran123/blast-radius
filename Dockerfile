# The scorer builds this on linux/amd64 and runs it with no network, a
# read-only disk and user nobody. The image holds the selector and its map,
# each copied by name, and nothing else from this repository. The selector is
# static, so sqlglot is not in the image.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The directories come first, with mode 755. COPY --chmod=644 would otherwise
# also give 644 to the directories it creates, and nobody could not enter them.
RUN mkdir -p /app/map && chmod 755 /app /app/map
COPY --chmod=644 selector.py /app/selector.py
COPY --chmod=644 map/index.json map/files.bin map/sets.bin /app/map/
CMD ["python", "/app/selector.py"]
