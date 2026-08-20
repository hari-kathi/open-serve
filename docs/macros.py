"""mkdocs-macros hook: expose the current release version to docs pages.

The version is derived at build time from the latest git tag (`git describe
--tags --abbrev=0`), so docs never carry a hardcoded release number — pushing
a new release tag is all it takes to update the site. Requires the checkout
to include tags (CI uses fetch-depth: 0).

Variables:
  {{ release_version }}       e.g. "v0.2.0"  (falls back to "dev" without tags)
  {{ release_version_bare }}  e.g. "0.2.0"   (leading "v" stripped)
"""

import subprocess


def define_env(env):
    try:
        version = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        version = "dev"
    env.variables["release_version"] = version
    env.variables["release_version_bare"] = version.lstrip("v")
