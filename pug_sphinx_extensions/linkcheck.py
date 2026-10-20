"""Use a completed Sphinx JSONL report to exclude previously known links."""

import json
import os
import re
from pathlib import Path

from sphinx.errors import ConfigError


def baseline_patterns(path):
    """Read exact URI patterns; provenance and completion belong to the producer."""
    uris = set()
    try:
        with Path(path).open(encoding="utf-8") as report:
            for number, line in enumerate(report, 1):
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError) as exc:
                    raise ConfigError(
                        f"Invalid linkcheck baseline {path}, line {number}: {exc}"
                    ) from exc
                if not isinstance(record, dict) or not isinstance(
                    record.get("uri"), str
                ):
                    raise ConfigError(
                        f"Invalid linkcheck baseline {path}, line {number}: "
                        "expected an object with a string uri"
                    )
                uris.add(record["uri"])
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read linkcheck baseline {path}: {exc}") from exc

    # Sphinx uses re.match(). Escape metacharacters and reject prefix matches,
    # including the final-newline exception of the $ anchor.
    return [re.escape(uri) + r"\Z" for uri in sorted(uris)]


def configure_linkcheck(app):
    """Activate only for the candidate linkcheck job, never the baseline job."""
    if (
        app.builder.name != "linkcheck"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") not in {"pull_request", "merge_group"}
    ):
        return

    if path := os.environ.get("LINKCHECK_BASELINE"):
        app.config.linkcheck_ignore.extend(baseline_patterns(path))
