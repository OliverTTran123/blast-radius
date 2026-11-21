"""Download sqlglot at one commit from its own GitHub repository and unpack it.

Usage: fetch_sqlglot.py <commit> <destination>

The base image has no git or curl, so this uses only the standard library. The
commit id in the URL is the pin: the archive's top-level directory must carry
the same id or the build stops.
"""

import io
import shutil
import sys
import tarfile
import urllib.request


def main(commit: str, destination: str) -> None:
    url = f"https://github.com/tobymao/sqlglot/archive/{commit}.tar.gz"
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        top_level = {member.name.split("/")[0] for member in archive.getmembers()}
        if top_level - {"pax_global_header"} != {f"sqlglot-{commit}"}:
            sys.exit(f"unexpected archive layout: {sorted(top_level)}")
        archive.extractall("/tmp/unpack", filter="data")
    shutil.move(f"/tmp/unpack/sqlglot-{commit}", destination)
    print(f"sqlglot {commit} unpacked to {destination} ({len(data)} archive bytes)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
