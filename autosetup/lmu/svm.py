"""Read and write LMU setup files (.svm).

Format: INI-like, CRLF line endings, one ``Key=<index>//<display value>`` line per
setting under ``[SECTION]`` headers. A few header lines (VehicleClassSetting,
UpgradeSetting, ``//VEH=...``) come before the first section; ``[BASIC]`` holds the
garage's simple-mode sliders. The game ignores ``//`` comments, so after changing an
index we rewrite the comment with our own label (or drop it).

Files are handled as latin-1 so every byte round-trips unchanged; some community setups
contain bytes that aren't valid UTF-8.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from autosetup.lmu.mapping import DO_NOT_WRITE, SVM_TO_VM, VM_TO_SVM
from autosetup.lmu.setup_space import CarSetup

ENCODING = "latin-1"
_SECTION = re.compile(r"^\[([^\]]+)\]\s*$")
_SETTING = re.compile(r"^(?P<key>[A-Za-z0-9_]+)=(?P<value>[^/\r\n]*)(?://(?P<comment>[^\r\n]*))?(?P<eol>\r?\n?)$")


@dataclass(frozen=True)
class SvmEntry:
    section: str | None
    key: str
    raw: str  # value text before any // comment
    comment: str | None
    line: int

    @property
    def index(self) -> int | None:
        try:
            return int(self.raw.strip())
        except ValueError:
            return None


class SvmFile:
    def __init__(self, lines: list[str], path: Path | None = None):
        self.lines = lines
        self.path = path
        self._entries: dict[tuple[str | None, str], SvmEntry] = {}
        self._reindex()

    @classmethod
    def read(cls, path: str | Path) -> "SvmFile":
        path = Path(path)
        text = path.read_bytes().decode(ENCODING)
        return cls(text.splitlines(keepends=True), path)

    @classmethod
    def parse(cls, text: str) -> "SvmFile":
        return cls(text.splitlines(keepends=True))

    def _reindex(self) -> None:
        self._vm_cache = None
        self._entries.clear()
        section = None
        for i, line in enumerate(self.lines):
            if m := _SECTION.match(line):
                section = m.group(1)
                continue
            if m := _SETTING.match(line):
                self._entries[(section, m["key"])] = SvmEntry(section, m["key"], m["value"], m["comment"], i)

    # -- reading -----------------------------------------------------------------
    def text(self) -> str:
        return "".join(self.lines)

    def entries(self) -> list[SvmEntry]:
        return list(self._entries.values())

    def get(self, section: str | None, key: str) -> SvmEntry | None:
        return self._entries.get((section, key))

    @property
    def vehicle_class(self) -> str:
        e = self.get(None, "VehicleClassSetting")
        return e.raw.strip().strip('"') if e else ""

    @property
    def veh_file(self) -> str:
        """Path of the .VEH the setup was saved for (from the ``//VEH=`` header)."""
        for line in self.lines:
            if line.startswith("//VEH="):
                return line[6:].strip()
        return ""

    def _vm(self) -> tuple[dict[str, int], dict[str, str]]:
        if self._vm_cache is None:
            values, labels = {}, {}
            for (section, key), e in self._entries.items():
                vm = SVM_TO_VM.get((section, key))
                if vm and e.index is not None:
                    values[vm] = e.index
                if vm and e.comment is not None:
                    labels[vm] = e.comment
            self._vm_cache = (values, labels)
            self._keyset_cache = frozenset(values)
        return self._vm_cache

    def _keyset(self) -> frozenset:
        """The mapped settings this file has (shared by every file of one car)."""
        self._vm()
        return self._keyset_cache

    def vm_values(self) -> dict[str, int]:
        """Index of every mapped setting, keyed by telemetry name (VM_*/WM_*)."""
        return dict(self._vm()[0])

    def vm_labels(self) -> dict[str, str]:
        """The display value written after ``//`` for every mapped setting."""
        return dict(self._vm()[1])

    # -- writing -----------------------------------------------------------------
    def set(self, section: str, key: str, index: int, label: str | None = None) -> None:
        e = self._entries.get((section, key))
        if e is None:
            raise KeyError(f"[{section}] {key} not in setup file")
        eol = "\r\n" if self.lines[e.line].endswith("\r\n") else "\n"
        comment = f"//{label}" if label is not None else ""
        self.lines[e.line] = f"{key}={int(index)}{comment}{eol}"
        self._entries[(section, key)] = SvmEntry(section, key, str(int(index)), label, e.line)
        self._vm_cache = None

    def set_vm(self, values: dict[str, int], labels: dict[str, str] | None = None) -> list[str]:
        """Apply telemetry-keyed indices. Returns the keys that have no line in this file.
        Keys in ``DO_NOT_WRITE`` are skipped silently."""
        missing = []
        for vm, index in values.items():
            if vm in DO_NOT_WRITE:
                continue
            target = VM_TO_SVM.get(vm)
            if target is None or self.get(*target) is None:
                missing.append(vm)
                continue
            label = (labels or {}).get(vm)
            self.set(*target, index, label)
        return missing

    def set_notes(self, notes: str) -> None:
        e = self.get("GENERAL", "Notes")
        if e is None:
            return
        safe = notes.replace('"', "'").replace("\r", " ").replace("\n", " ")
        # The file is Latin-1: arrows and math signs in plain characters, anything else '?'.
        # A '/' would start a // comment in the file: none in the notes.
        for ch, plain in (("→", "->"), ("≈", "~"), ("−", "-"), ("–", "-"), ("—", "-"), ("·", "-"), ("/", "-")):
            safe = safe.replace(ch, plain)
        safe = safe.encode(ENCODING, "replace").decode(ENCODING)
        eol = "\r\n" if self.lines[e.line].endswith("\r\n") else "\n"
        self.lines[e.line] = f'Notes="{safe}"{eol}'
        self._reindex()

    def write(self, path: str | Path) -> Path:
        """Write to ``path``. Never replaces an existing file."""
        path = Path(path)
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.text().encode(ENCODING))
        return path


def match_score(setup: CarSetup, svm: SvmFile) -> float:
    """Share of the setup's adjustable settings whose index *and* label equal the file's.

    ~1.0 means the file holds this very setup; ≥0.85 reliably means the same car model
    (labels differ between cars even at equal indices); below ~0.5 it is another car or a
    file saved under an older game version whose labels changed.
    """
    return score_keys(match_keys(setup), svm, 0.0)


def score_keys(keys: list[tuple[str, int, str]], svm: SvmFile, min_score: float) -> float:
    """match_score from ``match_keys`` (prepared once for many files); -1 once
    ``min_score`` is out of reach."""
    idx = svm._vm()[0]
    return _score([k for k in keys if k[0] in idx], svm, min_score)


def best_match(setup: CarSetup, svms: dict[Path, SvmFile], min_score: float) -> tuple[float, Path | None]:
    """(match_score, file) of the saved setup that best matches ``setup``, or (0, None)
    when none reaches ``min_score``. A file stops being scored as soon as it can't reach
    it, so files of other cars cost a few comparisons instead of every setting."""
    keys = match_keys(setup)
    present: dict[frozenset, list] = {}  # files of one car hold the same settings
    best: tuple[float, Path | None] = (0.0, None)
    for f, svm in svms.items():
        ks = svm._keyset()
        if ks not in present:
            present[ks] = [k for k in keys if k[0] in ks]
        score = _score(present[ks], svm, min_score)
        if score >= min_score and (best[1] is None or (score, f) > best):
            best = (score, f)
    return best


def match_keys(setup: CarSetup) -> list[tuple[str, int, str]]:
    """(key, index, label) of the settings match_score compares."""
    return [(p.key, p.value, p.text.strip()) for p in setup.adjustable() if p.key not in DO_NOT_WRITE]


def _score(present: list[tuple[str, int, str]], svm: SvmFile, min_score: float) -> float:
    """match_score over the ``match_keys`` the file has; -1 once ``min_score`` is out of reach."""
    idx, labels = svm._vm()  # read-only use; skips the defensive copies
    if not present:
        return 0.0
    allowed = len(present) * (1.0 - min_score) + 1e-9  # misses that still reach min_score
    misses = 0
    for key, value, text in present:
        if idx[key] != value or labels.get(key, "").strip() != text:
            misses += 1
            if misses > allowed:
                return -1.0
    return 1.0 - misses / len(present)


_READ_CACHE: dict[Path, tuple[float, int, SvmFile]] = {}


def read_settings(settings_dir: Path) -> dict[Path, SvmFile]:
    """Every saved setup (``<track>/*.svm``), re-read only when its file changed. Shared
    between calls: for reading only, never modify them."""
    out = {}
    for f in settings_dir.glob("*/*.svm"):
        st = f.stat()
        cached = _READ_CACHE.get(f)
        if cached is None or cached[:2] != (st.st_mtime, st.st_size):
            cached = (st.st_mtime, st.st_size, SvmFile.read(f))
            _READ_CACHE[f] = cached
        out[f] = cached[2]
    for f in set(_READ_CACHE) - set(out):
        if f.parent.parent == settings_dir:
            del _READ_CACHE[f]
    return out


def next_free_name(folder: Path, name_at) -> Path:
    """``<name>.svm`` with the lowest revision n not yet used: ``name_at(n)`` is the name at
    revision n. A write never replaces a file, so a taken name gets the next one."""
    n = 1
    while (folder / f"{name_at(n)}.svm").exists():
        n += 1
    return folder / f"{name_at(n)}.svm"
