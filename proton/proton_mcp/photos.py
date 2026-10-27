"""Actions on `proton drive photos`.

CLI contract verified against the real binary's --help output:

- Reads: `list` (flags --album --tag --limit --page --sort --desc),
  `albums` (remaps to `drive photos albums list`, flags --limit --page
  --sort --desc) and `download` (REF positional + --dest-dir; the download
  is classified as a READ per the project's Drive-download precedent — it
  writes only into the caller-named dest_dir and never touches the account).
- Gated: trash / delete (PERMANENT) / favorite / unfavorite, all taking
  positional REF... (link_ids).

Photos have NO filename field in the CLI's JSON — address them by link_id
(found via `list`) or by capture_time via a list query. `download --dest -`
streams raw binary to stdout (verified), which the JSON runner cannot
convey, so downloads always go to --dest-dir and there is deliberately no
`get` action: the CLI exposes no per-photo metadata verb.

Shared helpers live in .base (imported by tools.py too), so this module
imports cleanly on its own — no circular import through tools.
"""

from __future__ import annotations

from . import runner
from .base import _BaseTool, _limit_error, _refs_error

# Tag enum from `proton drive photos list --help`.
PHOTO_TAGS = frozenset(
    {
        "favorites",
        "screenshots",
        "videos",
        "live-photos",
        "motion-photos",
        "selfies",
        "portraits",
        "bursts",
        "panoramas",
        "raw",
    }
)


def _list_flags(params: dict) -> tuple[list[str], str | None]:
    """Shared flag mapping for `photos list` and `photos albums list`.

    Handles the flags both verbs accept (--limit/--page/--sort/--desc, plus
    the limit rail). Returns (argv_suffix, error); on error argv_suffix is
    empty and the caller returns a validation failure. list-only flags
    (--album, --tag) are mapped by the caller.
    """
    args: list[str] = []
    if params.get("sort") is not None:
        args += ["--sort", str(params["sort"])]
    limit = params.get("limit")
    if limit is not None:
        err = _limit_error({"limit": limit})
        if err:
            return [], err
        args += ["--limit", str(limit)]
    if params.get("page") is not None:
        args += ["--page", str(params["page"])]
    if params.get("desc"):
        args.append("--desc")
    return args, None


class PhotosTool(_BaseTool):
    """Actions on `proton drive photos`."""

    GATED_ACTIONS = frozenset({"trash", "delete", "favorite", "unfavorite"})

    def _read(self, action: str, params: dict) -> dict:
        if action == "list":
            args = ["drive", "photos", "list"]
            if params.get("album") is not None:
                args += ["--album", str(params["album"])]
            tag = params.get("tag")
            if tag is not None:
                if tag not in PHOTO_TAGS:
                    return self._validation_error(
                        f"unknown tag {tag!r}; valid tags: {', '.join(sorted(PHOTO_TAGS))}"
                    )
                args += ["--tag", tag]
            suffix, err = _list_flags(params)
            if err:
                return self._validation_error(err)
            return self._run(args + suffix)

        if action == "albums":
            # `albums` remaps to `drive photos albums list` (no bare
            # `drive photos albums` verb exists).
            args = ["drive", "photos", "albums", "list"]
            suffix, err = _list_flags(params)
            if err:
                return self._validation_error(err)
            return self._run(args + suffix)

        if action == "download":
            # 'id' is accepted as an alias for 'ref' (documented in the
            # MCP schema description); 'ref' is the canonical form.
            ref = params.get("ref") or params.get("id")
            if not ref:
                return self._validation_error("download requires 'ref' (photo link_id)")
            dest_dir = params.get("dest_dir")
            if not dest_dir:
                return self._validation_error(
                    "download requires 'dest_dir' (local directory; photos have no "
                    "filename in the API, so a caller-named directory is used)"
                )
            args = ["drive", "photos", "download", str(ref), "--dest-dir", str(dest_dir)]
            if params.get("force"):
                args.append("--force")
            # Download is a read (writes only into the caller-named dir):
            # never issued with --dry-run, so no belt-and-braces check.
            return self._run(args)

        raise ValueError(f"unknown read action {action!r} for photos")

    def _mutate(self, action: str, params: dict, dry_run: bool = False) -> dict:
        if action not in self.GATED_ACTIONS:
            raise ValueError(f"unknown gated action {action!r} for photos")
        refs = params.get("refs")
        single = params.get("ref") or params.get("id")
        if refs and single:
            return self._validation_error(
                f"{action} accepts either 'ref' or 'refs' (list of link_ids), not both"
            )
        if refs:
            # I1: reject non-list refs (a string would be iterated
            # per-character and act on garbage ids — fatal for PERMANENT
            # delete).
            err = _refs_error(refs)
            if err:
                return self._validation_error(f"{action}: {err}")
            refs = list(refs)
        elif single:
            refs = [single]
        if not refs:
            return self._validation_error(
                f"{action} requires 'ref' (a photo link_id) or 'refs' (list of link_ids)"
            )
        args = ["drive", "photos", action, *[str(r) for r in refs]]
        res = self._run(args, dry_run=dry_run)
        out = dict(res)
        out["refs"] = refs
        if action == "delete":
            out["warning"] = "delete is PERMANENT — photos are not recoverable"
        return out


PHOTOS_TOOL = PhotosTool()
