#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "parver",
# ]
# ///
from collections.abc import Iterable, Sequence
import difflib
import json
import logging
import runpy
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from parver import Version

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent.parent


def get_current_version() -> str:
    d = runpy.run_path(str(PROJECT_DIR / "_version.py"))
    return d["__version__"]


def sanitize_version(s: str) -> str:
    parsed = Version.parse(s)
    normalized = parsed.normalize()
    out = str(normalized)
    return out


@dataclass
class Args:
    new_version: str
    log_level: int
    execute: bool

    @classmethod
    def parse(cls, args=None):
        parser = ArgumentParser()
        parser.add_argument("new_version", type=sanitize_version)
        parser.add_argument("-v", "--verbose", action="count", default=0)
        parser.add_argument(
            "--execute",
            action="store_true",
            help="write the changes; by default, just print the diff",
        )
        p = parser.parse_args(args)

        log_level = {
            0: logging.WARNING,
            1: logging.INFO,
            2: logging.DEBUG,
        }.get(p.verbose, logging.DEBUG)

        return cls(p.new_version, log_level, p.execute)


JSO = int | float | None | bool | str | list["JSO"] | dict[str, "JSO"]


class JsoUpdater:
    def __init__(
        self, old_version: str, new_version: str, key: str, substring=False
    ) -> None:
        self.old_version = old_version
        self.new_version = new_version
        self.key = key
        self.substring = substring

    def apply(self, jso: JSO) -> int:
        count = 0
        if jso is None or isinstance(jso, (int, float, bool, str)):
            return count
        elif isinstance(jso, list):
            for item in jso:
                count += self.apply(item)
        elif isinstance(jso, dict):
            val = jso.get(self.key)
            if isinstance(val, str):
                if self.substring:
                    if self.old_version in val:
                        jso[self.key] = val.replace(self.old_version, self.new_version)
                        count += 1
                elif self.old_version == val:
                    jso[self.key] = self.new_version
                    count += 1
            for v in jso.values():
                count += self.apply(v)
        else:
            raise TypeError(f"Unknown JSO type: {type(jso)}")

        return count


@dataclass
class Update:
    old: str
    new: str


class VersionUpdater:
    def __init__(self, old_version: str, new_version: str) -> None:
        self.old = old_version
        self.new = new_version
        self.mapping: dict[Path, Update] = {}

    def apply_updates(self) -> int:
        count = 0
        for path, update in self.mapping.items():
            path.write_text(update.new)
            count += 1
        return count

    def _update_example(self, fpath: Path) -> bool:
        """Handle examples which are JSONC files which might be a full zarr.json document,
        an attributes object, or an ome object, or something else.

        Currently does a dumb string replace.
        """
        orig = fpath.read_text()
        # if (ome := jso.get("attributes", {}).get("ome")) or (ome := jso.get("ome")):
        #     inner = ome
        # else:
        #     inner = jso

        # if inner.get("version") == self.old:
        #     inner["version"] = self.new
        #     self.mapping[fpath] = Update(orig, json.dumps(jso, indent=2) + "\n")
        #     return True
        old = f'"version": "{self.old}"'
        new = f'"version": "{self.new}"'
        if old in orig:
            updated = orig.replace(old, new)
            self.mapping[fpath] = Update(orig, updated)
            return True
        return False

    def _update_schema_id(self, jso: dict[str, JSO]) -> bool:
        s = jso.get("$id", "")
        if isinstance(s, str) and self.old in s:
            jso["$id"] = s.replace(self.old, self.new)
            return True
        else:
            return False

    def _update_version_schema(self) -> bool:
        p = PROJECT_DIR / "schemas" / "_version.schema"
        orig = p.read_text()
        jso = json.loads(orig)
        updated_id = self._update_schema_id(jso)
        if not updated_id:
            logger.warning("Did not update $id of %s", p)
        vals = []
        updated_enum = False

        for s in jso["enum"]:
            if s == self.old:
                vals.append(self.new)
                updated_enum = True
            else:
                vals.append(s)

        if not updated_enum:
            logger.warning("Did not update enum field of %s", p)

        if updated_enum or updated_id:
            self.mapping[p] = Update(orig, json.dumps(jso, indent=2))
            return True
        return False

    def _update_schema(self, fpath: Path) -> int:
        orig = fpath.read_text()
        jso = json.loads(orig)
        updated_id = self._update_schema_id(jso)
        updater = JsoUpdater(self.old, self.new, "$ref", True)
        count = updater.apply(jso)
        total = count + updated_id
        if total:
            self.mapping[fpath] = Update(orig, json.dumps(jso, indent=2) + "\n")
        return total

    def _update_schemas(self) -> int:
        count = 0
        for p in PROJECT_DIR.joinpath("schemas").glob("*.schema*"):
            if p.name.startswith("_") or not p.is_file():
                continue
            n_updates = self._update_schema(p)
            count += bool(n_updates)
        return count + self._update_version_schema()

    def _update_examples(self) -> int:
        count = 0
        for p in PROJECT_DIR.joinpath("examples").glob("**/*.json"):
            if p.name.startswith(".") or not p.is_file():
                continue
            count += self._update_example(p)
        return count

    def _update_version_py(self):
        fpath = PROJECT_DIR / "_version.py"
        orig = fpath.read_text()
        self.mapping[fpath] = Update(orig, f"__version__ = {self.new}\n")
        return True

    def _update_ome(self, ome: dict[str, JSO]) -> bool:
        if ome.get("version") == self.old:
            ome["version"] = self.new
            return True
        return False

    def _update_attributes(self, attributes: dict[str, JSO]) -> bool:
        ome = attributes.get("ome")
        if isinstance(ome, dict):
            return self._update_ome(ome)
        return False

    def _update_attributes_file(self, fpath: Path) -> bool:
        orig = fpath.read_text()
        jso = json.loads(orig)
        updated = self._update_attributes(jso)
        if updated:
            self.mapping[fpath] = Update(orig, json.dumps(jso, indent=2) + "\n")
            return True
        return False

    def _update_zarr_json_file(self, fpath: Path) -> bool:
        orig = fpath.read_text()
        jso = json.loads(orig)
        attrs = jso.get("attributes")
        if attrs is None:
            return False
        updated = self._update_attributes(attrs)
        if updated:
            self.mapping[fpath] = Update(orig, json.dumps(jso, indent=2) + "\n")
            return True
        return False

    def _update_zarr_hierarchy(self, dpath: Path) -> int:
        count = 0
        for fpath in dpath.glob("**/zarr.json"):
            count += self._update_zarr_json_file(fpath)
        return count

    def _update_zarr_tests(self) -> int:
        count = 0
        for dpath in PROJECT_DIR.joinpath("tests/zarr").glob("**/*.ome.zarr"):
            count += self._update_zarr_hierarchy(dpath)
        return count

    def _update_attributes_tests(self) -> int:
        count = 0
        for fpath in PROJECT_DIR.joinpath("tests/attributes").glob("**/*.json"):
            count += self._update_attributes_file(fpath)
        return count

    def _update_changelog(self):
        path = PROJECT_DIR.joinpath("version_history.md")
        orig = path.read_text()

        updated = orig.replace(
            "## Unreleased",
            f"## Unreleased\n\n## {self.new} - TBC",
        )
        if updated != orig:
            self.mapping[path] = Update(orig, updated)
            return True
        return False

    def plan_updates(self) -> int:
        count = 0
        count += self._update_examples()
        count += self._update_schemas()
        count += self._update_attributes_tests()
        count += self._update_zarr_tests()
        count += self._update_version_py()
        count += self._update_changelog()
        return count

    def list_updated_files(self) -> list[Path]:
        return sorted(self.mapping)

    def _format_diff(self, path: Path) -> str:
        update = self.mapping[path]
        fname = str(path.relative_to(PROJECT_DIR))
        n1 = f"{fname} v{self.old}"
        n2 = f"{fname} v{self.new}"
        return "\n".join(
            difflib.unified_diff(
                update.old.splitlines(),
                update.new.splitlines(),
                fromfile=n1,
                tofile=n2,
                lineterm="",
            )
        )

    def format_diffs(self, path: Path | None = None):
        if path is not None:
            yield self._format_diff(path)
            return

        for p in self.list_updated_files():
            yield self._format_diff(p)



def main(raw_args=None):
    args = Args.parse(raw_args)
    logging.basicConfig(level=args.log_level)
    old_version = get_current_version()

    updater = VersionUpdater(old_version, args.new_version)
    n_updates = updater.plan_updates()
    if not n_updates:
        logger.warning("No updates to make")
        return 0
    if args.execute:
        updater.apply_updates()
    else:
        sep = "\n\n" + ("-" * 80) + "\n\n"
        print(sep.join(updater.format_diffs()))


@dataclass(frozen=True)
class Context:
    lines: int = 3
    characters: int = 80


DEFAULT_CONTEXT = Context()


@dataclass
class Pos:
    offset: int
    line: int
    col: int


def iter_occurrences(haystack: str, needle: str) -> Iterable[int]:
    offset = 0
    while True:
        local_offset = haystack.find(needle, offset)
        if local_offset < 0:
            return
        start = offset + local_offset
        yield start
        offset = start + len(needle)


T = TypeVar("T")


def scan(seq: Sequence[T], start=0, step=1) -> Iterable[T]:
    end = len(seq)
    idx = start
    while start < 0:
        idx += end

    while True:
        yield seq[idx]
        idx += step
        if step >= end or step < 0:
            return


class InteractiveReplacer:
    def __init__(self, text: str, old: str, new: str, context: int = 80) -> None:
        self.text = text
        self.old = old
        self.new = new
        self.context = context

    def iter_occurrences(self) -> Iterable[int]:
        offset = 0
        while True:
            local_offset = self.text.find(self.old, offset)
            if local_offset < 0:
                return
            start = offset + local_offset
            yield start
            offset = start + len(self.old)

    def iter_diffs(self) -> Iterable[str]:
        for offset in self.iter_occurrences():
            pre = self.text[max(offset - self.context, 0) : offset]
            end = offset + len(self.old)
            post = self.text[end : min(end, len(self.text))]

            before = f"{pre}{self.old}{post}"
            after = f"{pre}{self.new}{post}"

            yield just_diff(before, after)


def just_diff(before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(), after.splitlines())
    return "\n".join(ln for ln in lines if not ln.endswith("\n"))


if __name__ == "__main__":
    # sys.exit(main())
    a = list("abcdefghijk")
    b = list("AbcdefghijK")
    print("\nLINEBREAK\n".join(difflib.context_diff(a, b, lineterm="")))
