"""Check that the coffea version agrees in all three places that declare it.

Three files pin coffea, and they have to stay in step:

  * requirements.txt              -- what the notebook venv and CI install
  * constraints.txt               -- the generated freeze of that same stack
  * sidm/tools/scaleout.py        -- the apptainer image the dask WORKERS run in

The last one is the one that gets forgotten. Dask workers on LPC do not install
requirements.txt at all; they get coffea from the image tag, while the client
(your notebook) gets it from the venv. Client and workers exchange serialized
objects, so a version split between them fails at runtime, confusingly, far from
the cause.

Nothing else catches it. The chain report runs the processor locally over a
committed fixture and never starts an apptainer worker, so a coffea bump that
updates requirements.txt and regenerates constraints.txt -- exactly the ritual
the constraints.txt header prescribes -- goes green while quietly breaking the
dask workflow for whoever runs it next.

Run standalone from anywhere:  python tests/check_worker_image.py
"""
import re
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.txt"
CONSTRAINTS = REPO / "constraints.txt"
SCALEOUT = REPO / "sidm" / "tools" / "scaleout.py"

# e.g. "coffea-dask-almalinux9:2025.5.0.rc2-py3.11" -> "2025.5.0.rc2"
_IMAGE_TAG_RE = re.compile(r"coffea-dask-\w+:([0-9][^\"'\s]*)")


def _pinned_coffea(path):
    """The version in a `coffea==X` line, or None if the file has no such line."""
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.lower().startswith("coffea=="):
            return line.split("==", 1)[1].strip()
    return None


def _image_coffea():
    """(full tag, coffea part) from the apptainer image string in scaleout.py.

    The string is split across source lines, so match against the whole file
    rather than line by line.
    """
    match = _IMAGE_TAG_RE.search(SCALEOUT.read_text())
    if match is None:
        return None, None
    tag = match.group(1)
    return tag, tag.split("-")[0]   # drop the "-py3.11" suffix


def main():
    """Compare the three declared coffea versions; return a process exit code."""
    found = {
        "requirements.txt": _pinned_coffea(REQUIREMENTS),
        "constraints.txt": _pinned_coffea(CONSTRAINTS),
    }
    image_tag, found["sidm/tools/scaleout.py (worker image)"] = _image_coffea()

    missing = [where for where, version in found.items() if version is None]
    if missing:
        print("ERROR: could not find a coffea version in: " + ", ".join(missing))
        print("This check has gone stale -- update tests/check_worker_image.py "
              "to match how those files now declare coffea.")
        return 1

    # PEP 440 normalises "2025.5.0.rc2" (image tag style) and "2025.5.0rc2"
    # (PyPI style) to the same version, so compare parsed rather than as text.
    parsed = {}
    for where, version in found.items():
        try:
            parsed[where] = Version(version)
        except InvalidVersion:
            print(f"ERROR: {where} declares coffea {version!r}, which is not a "
                  f"valid version string")
            return 1

    for where, version in found.items():
        print(f"  {where}: {version}")
    print(f"  (worker image tag: {image_tag})")

    if len(set(parsed.values())) == 1:
        print("\ncoffea agrees across the venv, the freeze and the worker image")
        return 0

    print("\nERROR: these must all be the same coffea version.")
    print("The dask client runs the version from requirements.txt/constraints.txt "
          "and the workers run the one baked into the apptainer image; a split "
          "between them breaks the dask workflow at runtime.")
    print("Fix: update _DEFAULT_LPC_IMAGE in sidm/tools/scaleout.py to the tag "
          "matching the new coffea, then confirm it exists under "
          "/cvmfs/unpacked.cern.ch/registry.hub.docker.com/coffeateam/.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
