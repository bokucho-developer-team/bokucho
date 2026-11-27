"""Applying a patch to a game folder, and undoing it: everything the window does, without it."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import locale
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePath
from typing import Any
from zipfile import ZipFile

from bokucho.patcher.delta import apply_delta
from bokucho.patcher.payload import FileState, Patch, PatchAdded, PatchedFile, safe_parts

ERROR_SHARING_VIOLATION = 32
SPARE_BYTES = 64 << 20
_DAMAGED = "補丁檔已損壞，請重新下載。遊戲檔案沒有被改動。"
_NO_PERMISSION = (
    "沒有權限寫入「{folder}」。遊戲如果裝在 Program Files 這類系統資料夾，"
    "請把整個遊戲資料夾搬到其他地方（例如「文件」）再試一次。"
)
RECORD = "bokucho-added.json"
_IN_THE_WAY = "「{name}」已經存在，請先移到別處。"
LOCK = "bokucho-patching.lock"
_LOCK_MARK = b"Bokucho is patching this game folder.\n"
_BUSY = "另一個補丁程式正在處理這個遊戲資料夾。請等它完成，再試一次。"
_LEFT_BEHIND = (
    "「{name}」是先前中斷的補丁程式留下的暫存檔，現在無法刪除。"
    "請關閉正在使用它的程式，再試一次。"
)
_SCRATCH = ".bokucho-tmp"
_UNREADABLE = "無法讀寫遊戲資料夾裡的檔案（{error}）。請關閉遊戲和其他程式，再試一次。"
_LINKED = (
    "「{link}」是一個連結，補丁不會讀寫連結指向的位置。"
    "請先把實體檔案移進遊戲資料夾，再試一次。"
)
_DAMAGED_RECORD = (
    f"還原紀錄「{RECORD}」已損壞，無法確定補丁加入了哪些檔案，所以沒有還原。遊戲檔案沒有被改動。"
    "請重新安裝遊戲，或把遊戲資料夾換回套用補丁前的備份。"
)
_DAMAGED_RECORD_RESTORING = (
    f"還原紀錄「{RECORD}」已損壞，無法確定補丁加入了哪些檔案。原版遊戲檔案會照常還原，"
    "但補丁加入的檔案（例如字型）可能會留在遊戲資料夾裡。"
)
_DAMAGED_RECORD_FOUND = (
    f"還原紀錄「{RECORD}」已損壞，無法確定補丁加入了哪些檔案，所以不能套用或還原。"
    "請重新安裝遊戲，或把遊戲資料夾換回套用補丁前的備份。"
)
_OTHER_RECORD = (
    f"遊戲資料夾裡有其他補丁留下的還原紀錄「{RECORD}」。請先用那個補丁還原；"
    "如果已經沒有那個補丁，請把這個檔案移到別處，再試一次。"
)
_OTHER_VERSION = "遊戲裡已經套用了另一個版本的翻譯補丁：請先按「還原」，再按「套用」。"
_OTHER_FILE = (
    "遊戲資料夾裡已經有「{name}」，而且內容不同（可能是其他補丁的檔案）。"
    "請先把它移到別處，再試一次。"
)
_NO_FOLDER = (
    "遊戲資料夾裡找不到「{folder}」資料夾，補丁要把檔案放在裡面。請確認選的是遊戲所在的資料夾。"
)
GAME_INI = "Game.ini"
_IN_USE = "有檔案正在使用中。請關閉遊戲和其他程式，再試一次。"
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
#: Engines whose recipe leaves every game file alone and only adds files beside them.
ADD_ONLY_ENGINES = frozenset({"kirikiri"})
Log = Callable[[str], None]


def _report(log: Log | None, message: str) -> None:
    if log is not None:
        log(message)


class PatchError(Exception):
    """What the player needs to know; the game folder is as it was."""


class State(Enum):
    MISSING = "missing"
    ORIGINAL = "original"
    PATCHED = "patched"
    UNKNOWN = "unknown"
    #: Bokucho's record of what a patch added cannot be trusted.
    DAMAGED = "damaged"
    #: Beside the originals is another patch's record: its entries and this patch's would mix.
    FOREIGN = "foreign"
    #: A game file is reached through a link, so it may be outside the game folder.
    LINKED = "linked"


@dataclass(frozen=True)
class FolderState:
    state: State
    restorable: bool
    #: For :attr:`State.LINKED`, the link in the way, relative to the game folder.
    link: str = ""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def backup_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.bak")


def _temporary_path(target: Path) -> Path:
    return target.with_name(f"{target.name}{_SCRATCH}")


def _names(patch: Patch) -> str:
    if patch.mount is not None:
        from bokucho.patcher.mount import mount_text

        return mount_text(patch).names
    names = [item.name for item in patch.files]
    if len(names) > 3:
        return f"{'、'.join(names[:3])} 等 {len(names)} 個檔案"
    return "、".join(names)


def _file_state(folder: Path, item: PatchedFile, patch: Patch) -> State:
    target = folder / item.name
    if _link_on_the_way(folder, target) is not None:
        return State.LINKED
    if not target.is_file():
        return State.MISSING
    digest = sha256_of(target)
    if digest == item.original.sha256:
        return State.ORIGINAL
    if item.patched is not None and digest == item.patched.sha256:
        return State.PATCHED
    if patch.recipe is not None:
        try:
            record = read_record(folder)
            if (
                record.get("recipe") == _digest(patch.recipe)
                and record.get("compiled", {}).get(item.name) == digest
            ):
                return State.PATCHED
        except (OSError, ValueError, AttributeError):
            pass
    return State.UNKNOWN


def _backup_is_original(folder: Path, item: PatchedFile) -> bool:
    backup = backup_path(folder / item.name)
    # A link put in place would leave the game file pointing wherever it points.
    return (
        _link_on_the_way(folder, backup) is None
        and backup.is_file()
        and sha256_of(backup) == item.original.sha256
    )


def _add_only(patch: Patch) -> bool:
    return patch.recipe is not None and _recipe_engine(patch) in ADD_ONLY_ENGINES


def _applied_recipe(folder: Path) -> str | None:
    """The recipe an add-only patch applied here, while what it added is all still there."""
    try:
        record = read_record(folder)
    except (OSError, ValueError):
        return None
    recipe = record.get("recipe") if isinstance(record, dict) else None
    added = _recorded(folder)
    if not isinstance(recipe, str) or not added:
        return None
    if all(_holds(folder, entry["name"], entry["sha256"]) for entry in added):
        return recipe
    return None


def _holds(folder: Path, name: str, digest: str) -> bool:
    """Whether the file by ``name`` in ``folder``, reached through no link, has ``digest``."""
    return (
        _inside(name)
        and _link_on_the_way(folder, path := folder / name) is None
        and path.is_file()
        and sha256_of(path) == digest
    )


def inspect(folder: Path, patch: Patch) -> FolderState:
    """Whether ``folder`` holds the originals, the translation, or neither."""
    if patch.mount is not None:
        from bokucho.patcher.mount import inspect_mount

        return inspect_mount(folder, patch)
    if _link_on_the_way(folder, folder / RECORD) is not None:
        return FolderState(State.LINKED, False, RECORD)
    each = {item.name: _file_state(folder, item, patch) for item in patch.files}
    for item in patch.files:
        if each[item.name] is State.LINKED:
            link = _link_on_the_way(folder, folder / item.name)
            assert link is not None
            return FolderState(State.LINKED, False, link.relative_to(folder).as_posix())
    states = set(each.values())
    if states == {State.ORIGINAL} and _add_only(patch):
        assert patch.recipe is not None
        try:
            #: The game's own files are untouched, so only the record tells what was added.
            added_files(folder)
        except ValueError:
            return FolderState(State.DAMAGED, False)
        applied = _applied_recipe(folder)
        if applied is not None:
            #: Another version's archive is in place; restoring with this one takes it away.
            same = applied == _digest(patch.recipe)
            return FolderState(State.PATCHED if same else State.UNKNOWN, True)
    if State.MISSING in states:
        state = State.MISSING
    elif states == {State.ORIGINAL}:
        state = State.ORIGINAL
    elif states == {State.PATCHED}:
        state = State.PATCHED
    else:
        state = State.UNKNOWN
    if state is State.ORIGINAL and os.path.lexists(folder / RECORD) and not _own_record(
        folder, patch
    ):
        # Said here, not first by apply, and before a recipe is compiled: a record this
        # patch's own stopped install left is taken up again, and any other is refused.
        try:
            added_files(folder)
        except ValueError:
            return FolderState(State.DAMAGED, False)
        # One a restore here left partway lists only what it was still taking away: a restore
        # finishes it once every original that one was putting back is in place. Apply never
        # takes it up.
        return FolderState(State.FOREIGN, _left_by_a_restore(folder))
    #: Originals beside this patch's own record: it stopped partway, or the originals were
    #: put back some other way. Restore takes away what the record lists, and the record.
    restorable = (state is not State.ORIGINAL or _own_record(folder, patch)) and all(
        each[item.name] is State.ORIGINAL or _backup_is_original(folder, item)
        for item in patch.files
    )
    return FolderState(state, restorable)


def _own_record(folder: Path, patch: Patch) -> bool:
    """Whether the record in ``folder`` is one this patch wrote."""
    try:
        record = read_record(folder)
        # Every entry as Bokucho writes it, whatever names the patch: one it did not write
        # would be dropped from the record apply writes, and its file left by restore.
        entries = _shaped(record)
    except (OSError, ValueError):
        return False
    if "restoring" in record:
        return False
    if patch.recipe is not None:
        return record.get("recipe") == _digest(patch.recipe)
    if "recipe" in record:
        return False
    if "patch" in record:
        return record["patch"] == _identity(patch)
    # Written before records named their patch: told by content alone. Another patch's
    # file by one of these names is not this patch's, nor is a record listing none of them.
    added = {(item.name, _digest(item.data)) for item in patch.added}
    listed = {(entry["name"], entry["sha256"]) for entry in entries}
    return bool(listed) and listed <= added


def _identity(patch: Patch) -> str:
    """What a patch without a recipe is, by content: two that add one same file differ."""
    parts = [
        [item.name, item.original.sha256, item.patched.sha256 if item.patched else None]
        for item in patch.files
    ]
    parts += [[item.name, _digest(item.data)] for item in patch.added]
    return _digest(json.dumps(parts).encode("utf-8"))


def _left_by_a_restore(folder: Path) -> bool:
    """A record a restore marked, every original that restore was putting back now in place."""
    try:
        record = read_record(folder)
    except (OSError, ValueError):
        return False
    putting = record.get("restoring") if isinstance(record, dict) else None
    return isinstance(putting, dict) and all(
        _holds(folder, name, digest) for name, digest in putting.items()
    )


def _mark_as_a_restores(folder: Path, patch: Patch) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Mark another version's record, or a damaged one, as one a restore is taking away.

    Marked before any original goes back: a restore stopped partway, whatever stops it,
    would leave the originals beside a record this patch neither applies nor restores by.
    Marked, no apply takes it up, and a restore finishes it once each original the mark
    names is back: one finishing it sooner would take the record from a translation still
    in place. What the record says was compiled stays, so such a translation is never read
    as the game's own. A record there that cannot be read at all stops the restore here.

    Returns what the record lists as added, from this one read: read again, a record that
    cannot be read lists nothing, and what it added would stay with no record of it. With
    it, what an earlier restore is still to put back besides this patch's own originals.
    """
    try:
        record = read_record(folder)
    except FileNotFoundError:
        return [], {}
    except ValueError:  # not JSON: what it listed is unknown, and the restore says so
        record = None
    listed = _entries(record)
    if isinstance(record, dict) and _own_record(folder, patch):
        return listed, {}
    document = record if isinstance(record, dict) else {}
    # What an earlier restore was putting back stays named: marked afresh, a patch putting
    # back none of it could finish the record while that restore's translation stays.
    earlier = document.get("restoring")
    earlier = earlier if isinstance(earlier, dict) else {}
    mine = {item.name: item.original.sha256 for item in patch.files}
    if any(earlier.get(name, digest) != digest for name, digest in mine.items()):
        # Another original is to go back where this patch would put its own: named over,
        # that one would read as back once this one is.
        raise PatchError(_OTHER_RECORD)
    document = {**document, "added": listed, "restoring": {**earlier, **mine}}
    _rewrite(folder / RECORD, json.dumps(document).encode("utf-8"))
    return listed, {name: digest for name, digest in earlier.items() if name not in mine}


def describe(found: FolderState, patch: Patch) -> str:
    """The status line for ``found``."""
    names = _names(patch)
    if found.state is State.DAMAGED:
        return _DAMAGED_RECORD_FOUND
    if found.state is State.FOREIGN:
        return _OTHER_VERSION if found.restorable else _OTHER_RECORD
    if found.state is State.LINKED:
        return _LINKED.format(link=found.link)
    if found.state is State.ORIGINAL:
        return f"找到原版 {names}，可以套用。"
    if found.state is State.PATCHED:
        if found.restorable:
            return "已經套用過補丁。要換回原版請按「還原」。"
        return "已經套用過補丁，但找不到原版備份，無法還原。"
    if found.state is State.MISSING:
        if found.restorable:
            return f"找不到 {names}，但有原版備份。要換回原版請按「還原」。"
        return f"這個資料夾裡找不到 {names}。請選擇遊戲所在的資料夾。"
    if found.restorable:
        if _add_only(patch):
            return _OTHER_VERSION
        return f"{names} 不是這個補丁的版本，但有原版備份：請先按「還原」，再按「套用」。"
    return f"{names} 不是這個補丁對應的原版（版本不同，或已被其他補丁修改），所以不能套用。"


def default_folder(start: Path, patch: Patch) -> Path | None:
    """The folder the patch sits in, when the game is there too."""
    if patch.mount is not None:
        found = inspect(start, patch)
        return start if found.state in {State.ORIGINAL, State.PATCHED} or found.restorable else None
    return start if all((start / item.name).is_file() for item in patch.files) else None


def _open_for_update(path: Path) -> None:
    """Ask for write access the way Windows says who holds the file."""
    if sys.platform != "win32":
        with path.open("r+b"):
            return
    import _winapi

    handle = _winapi.CreateFile(str(path), _winapi.GENERIC_WRITE, 7, 0, _winapi.OPEN_EXISTING, 0, 0)
    _winapi.CloseHandle(handle)


def _require_not_in_use(target: Path) -> None:
    try:
        _open_for_update(target)
    except PermissionError as error:
        if getattr(error, "winerror", None) == ERROR_SHARING_VIOLATION:
            raise PatchError(f"{target.name} 正在使用中。請先關閉遊戲，再試一次。") from error


def _refusal(error: PermissionError, folder: Path) -> PatchError:
    if getattr(error, "winerror", None) == ERROR_SHARING_VIOLATION:
        return PatchError(_IN_USE)
    return PatchError(_NO_PERMISSION.format(folder=folder))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_record(folder: Path) -> Any:
    """The record, parsed. A link there is not followed: it raises ``OSError`` unread.

    No record there raises ``FileNotFoundError``. One that is not JSON, or is nested past
    what the parser follows, raises ``ValueError``: it is damaged.
    """
    record = folder / RECORD
    if _link_on_the_way(folder, record) is not None:
        raise OSError(f"{RECORD} is a link, which the patcher does not read")
    text = record.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except RecursionError:
        raise ValueError(f"{RECORD} is nested past what the parser follows") from None


def _recorded(folder: Path) -> list[dict[str, str]]:
    """The files a patch added to this game; nothing for a record gone or damaged."""
    try:
        return _entries(read_record(folder))
    except (OSError, ValueError):
        return []


def _entries(data: Any) -> list[dict[str, str]]:
    """The well-formed entries a parsed record lists as added."""
    entries = data.get("added") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        {"name": entry["name"], "sha256": entry["sha256"]}
        for entry in entries
        if _well_formed(entry)
    ]


def _well_formed(entry: object) -> bool:
    """An entry as :func:`_record` writes it, naming a file inside the game folder."""
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("sha256"), str)
        and _inside(entry["name"])
    )


def added_files(folder: Path) -> list[str]:
    """The names Bokucho's record lists as added to this game; empty without a record.

    Raises ``ValueError`` for a record that is there but cannot be read, or is not in the
    shape :func:`_record` writes: what the patch added is then unknown, and reading past it
    would take a translation for the game's own.
    """
    try:
        data = read_record(folder)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:  # a link pointing nowhere among them
        raise ValueError(f"{RECORD} cannot be read: {error}") from None
    return [entry["name"] for entry in _shaped(data)]


def _shaped(data: Any) -> list[dict[str, str]]:
    """What a parsed record lists as added; ``ValueError`` unless :func:`_record` wrote it so."""
    entries = data.get("added") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not all(_well_formed(entry) for entry in entries):
        raise ValueError(f"{RECORD} is not in the shape Bokucho writes")
    return _entries(data)


def _inside(name: str) -> bool:
    """A relative path in the game folder: an RGSS fill font goes into its Fonts folder."""
    try:
        safe_parts(name)
    except ValueError:
        return False
    return True


def _record(
    folder: Path, patch: Patch, listed: list[dict[str, str]], fresh: list[PatchAdded]
) -> None:
    """Publish ``listed``, what this patch's record lists already, and ``fresh``, the files
    it adds now."""
    listed = list(listed)
    for item in fresh:
        entry = {"name": item.name, "sha256": _digest(item.data)}
        if entry not in listed:
            listed.append(entry)
    document = {"added": listed}
    if patch.recipe is not None:
        document["recipe"] = _digest(patch.recipe)
        document["compiled"] = {
            item.name: item.patched.sha256 for item in patch.files if item.patched is not None
        }
    else:
        document["patch"] = _identity(patch)
    # Escaped, as every record is written: a record taken up again may hold what UTF-8
    # cannot encode, and the install would stop on it.
    _rewrite(folder / RECORD, json.dumps(document).encode("utf-8"))


#: The game folder this patcher holds, and the handle of the lock file it holds it by.
_HELD: ContextVar[tuple[Path, int] | None] = ContextVar("_HELD", default=None)


def _journal(path: Path, own: os.stat_result) -> None:
    """List ``path``, the file :func:`_write_new` or :func:`_stage` just made, in the lock file.

    Listed before anything is written to it: a patcher stopped partway leaves what it was
    writing, and the next one takes away what is listed, while it is still the very file
    listed. A file that merely has a scratch name is not Bokucho's to take away.
    """
    held = _HELD.get()
    if held is None:
        return
    folder, lock = held
    line = json.dumps([path.relative_to(folder).as_posix(), own.st_dev, own.st_ino])
    os.lseek(lock, 0, os.SEEK_END)
    _write_all(lock, line.encode("utf-8") + b"\n")


def _write_all(handle: int, data: bytes) -> None:
    """Write all of ``data``: one write may take only part of it."""
    while data:
        data = data[os.write(handle, data) :]


def _settle(folder: Path, lock: int) -> list[str]:
    """Take away each file the lock file lists that is still the file listed.

    What cannot be taken away is what this returns, and the list then stays as it was: it
    is never rewritten, so a write cut short cannot lose a line. Any other line names what
    is gone, or no longer the file listed, or what no patcher made: it goes unheeded.
    """
    os.lseek(lock, 0, os.SEEK_SET)
    listed = b"".join(iter(lambda: os.read(lock, 1 << 20), b""))  # every line, however many
    left = []
    for line in listed[len(_LOCK_MARK) :].splitlines():
        try:
            name, device, inode = json.loads(line)
        except (ValueError, TypeError, RecursionError):  # not a line a patcher wrote
            continue
        if not (isinstance(name, str) and name.endswith(_SCRATCH) and _inside(name)):
            continue
        path = folder / name
        try:
            found = None if _link_on_the_way(folder, path) else os.lstat(path)
        except (FileNotFoundError, NotADirectoryError, ValueError):  # ValueError: a NUL in it
            continue
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.ENAMETOOLONG):  # a name no file can have
                continue
            left.append(name)  # not known to be gone, nor to be another file: it stays listed
            continue
        if found is None or (found.st_dev, found.st_ino) != (device, inode):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            left.append(name)
    if not left:
        os.ftruncate(lock, len(_LOCK_MARK))
    return left


def _holds_only_the_mark(lock: int) -> bool:
    """Whether the lock file holds Bokucho's mark and nothing else.

    While a patcher holds it, Windows refuses the read, and the file could not go anyway.
    """
    try:
        os.lseek(lock, 0, os.SEEK_SET)
        return os.read(lock, len(_LOCK_MARK) + 1) == _LOCK_MARK
    except OSError:
        return False


class _Absent(Exception):
    """No game folder there to hold: the apply or restore goes no further."""


@contextmanager
def _one_at_a_time(folder: Path) -> Iterator[None]:
    """Hold ``folder`` for one apply or restore: another patcher there meanwhile is refused.

    The system holds the lock, so a patcher that stops partway leaves none in force; the
    file it leaves is marked as Bokucho's, and lists what that patcher was writing. The next
    patcher takes the file over, and those files away, before it starts.
    """
    if not folder.is_dir():
        # Gone on unlocked, the checks could find a folder put there meanwhile, and write
        # beside another patcher holding it.
        raise _Absent
    lock = folder / LOCK
    _refuse_link(folder, lock)
    if lock.exists() and not lock.is_file():
        raise PatchError(_IN_THE_WAY.format(name=LOCK))
    try:
        # A link put there since the check above is not followed where the system allows.
        # Binary, or Windows would write each "\n" as two bytes, past where it is cut back to.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        handle = os.open(lock, flags | getattr(os, "O_NOFOLLOW", 0))
    except PermissionError as error:
        raise _refusal(error, folder) from error
    except OSError as error:
        raise PatchError(_IN_THE_WAY.format(name=LOCK)) from error
    done = bare = False
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            # Held elsewhere: only Bokucho's mark alone may go, below. Another program's
            # file stays, and so does a list of what a stopped patcher left.
            bare = _holds_only_the_mark(handle)
            raise PatchError(_BUSY) from error
        # The file locked must be the one at the name, not where a link there leads: the
        # patcher that held it may have taken it away, and another may hold a new one.
        try:
            own = os.fstat(handle)
            same = os.path.samestat(own, os.lstat(lock))
        except OSError as error:
            raise PatchError(_BUSY) from error
        if not same:
            raise PatchError(_BUSY)
        # A file with another name too may be one outside the folder: written, it would change.
        if own.st_nlink > 1:
            raise PatchError(_IN_THE_WAY.format(name=LOCK))
        # Empty, or the mark cut short, a patcher stopped before marking it in full: the mark
        # is finished here. Anything else is another program's.
        found = os.read(handle, len(_LOCK_MARK))
        if not _LOCK_MARK.startswith(found):
            raise PatchError(_IN_THE_WAY.format(name=LOCK))
        _write_all(handle, _LOCK_MARK[len(found) :])
        if left := _settle(folder, handle):
            raise PatchError(_LEFT_BEHIND.format(name=left[0]))
        token = _HELD.set((folder, handle))
        try:
            yield
        finally:
            _HELD.reset(token)
            # What this patcher could not take away stays listed for the next, and so does a
            # list it cannot look at now: that neither undoes what was done nor hides why it
            # stopped.
            with suppress(OSError):
                done = not _settle(folder, handle)
    finally:
        # The patcher that held the lock takes the file away, and while it still holds it
        # where the system allows that, so no other can lock the file on its way out.
        # Windows refuses to remove a file any patcher has open: there it goes after
        # closing, and a patcher refused meanwhile tries too, since it may have been the one
        # whose open file kept it there. One still open elsewhere stays.
        if done and sys.platform != "win32":
            with suppress(OSError):
                lock.unlink()
        os.close(handle)
        if (done or bare) and sys.platform == "win32":
            with suppress(OSError):
                lock.unlink()


def apply(folder: Path, patch: Patch, log: Log | None = None) -> None:
    """Put the translation in place of each original, keeping the original as ``<name>.bak``."""
    try:
        with _one_at_a_time(folder):
            _apply(folder, patch, log)
    except _Absent:
        # Said as found then: looked at again, a folder put there since could read as ready.
        raise PatchError(describe(FolderState(State.MISSING, False), patch)) from None
    except OSError as error:  # what no step put into words
        raise PatchError(_UNREADABLE.format(error=error)) from error


def _apply(folder: Path, patch: Patch, log: Log | None) -> None:
    _report(log, "正在檢查遊戲檔案。")
    if patch.mount is not None:
        from bokucho.patcher.mount import install_mount, mount_text

        _report(log, mount_text(patch).installing)
        install_mount(folder, patch, log)
        return
    if patch.recipe is None:
        _report(log, "正在套用翻譯檔案。")
        _install(folder, patch)
        return
    found = inspect(folder, patch)
    if found.state is not State.ORIGINAL:
        raise PatchError(describe(found, patch))
    add_only = _add_only(patch)
    for item in patch.files:
        target = folder / item.name
        _require_not_in_use(target)
        if add_only:
            continue
        # A link pointing nowhere reads as missing: the swap would put the original over it.
        if os.path.lexists(backup_path(target)) and not _backup_is_original(folder, item):
            raise PatchError(f"「{backup_path(target).name}」不是原版備份，請先移到別處。")
    try:
        from bokucho.patcher.recipe import compile_recipe, recipe_size

        #: An add-only recipe writes its output beside the game and changes nothing else.
        needed = 0 if add_only else sum(item.original.size for item in patch.files) * 4
        needed += recipe_size(patch.recipe) * 2 + SPARE_BYTES
        if shutil.disk_usage(folder).free < needed:
            raise PatchError("磁碟空間不足：編譯補丁需要解包與重建遊戲的暫存空間。")
        with tempfile.TemporaryDirectory(prefix=".bokucho-compile-", dir=folder) as scratch:
            rebuilt, added = compile_recipe(folder, patch, Path(scratch), log)
            files = tuple(
                replace(
                    item,
                    patched=FileState(
                        rebuilt[item.name].stat().st_size, sha256_of(rebuilt[item.name])
                    ),
                )
                if item.name in rebuilt or not add_only
                else item
                for item in patch.files
            )
            compiled = replace(patch, files=files, added=added)
            _report(log, "正在安裝編譯結果。")
            _install(folder, compiled, rebuilt)
    except PatchError:
        raise
    except PermissionError as error:
        raise _refusal(error, folder) from error
    except Exception as error:
        raise PatchError(f"補丁編譯失敗，遊戲檔案沒有被改動。（{error}）") from error
    _retitle(folder, patch.recipe, log)


def _title_line(lines: list[bytes]) -> int | None:
    """Where ``[Game]``'s ``Title=`` is.

    A line's first byte, and every byte before its first ``=``, is ASCII in the code pages
    RGSS games are written in (CP932, Big5, GBK), so the lines are read without decoding.
    """
    section = b""
    for index, line in enumerate(lines):
        text = line.strip()
        key, equals, _ = text.partition(b"=")
        if text.startswith(b"["):
            section = text.strip(b"[] \t").lower()
        elif section == b"game" and equals and key.strip().lower() == b"title":
            return index
    return None


def _with_title(line: bytes, value: bytes) -> bytes:
    body = line.rstrip(b"\r\n")
    return body.partition(b"=")[0] + b"=" + value + line[len(body) :]


def _rewrite(path: Path, data: bytes, *, new: bool = False) -> os.stat_result:
    """Put ``data`` at ``path`` whole, or leave ``path`` as it was; the file put there.

    ``new``, it goes in only while nothing is at ``path``, or at a name the system reads as
    ``path``: a file put there since stays, and the install stops naming it.
    """
    temporary = _temporary_path(path)
    # Making the file follows a link, even one to a file not there yet. A link, like a
    # file, at the scratch name is not Bokucho's to take away: it stays, in the way.
    if temporary.is_symlink() or temporary.is_junction():
        raise FileExistsError(errno.EEXIST, "A link is at the scratch name", str(temporary))
    own = _write_new(temporary, data)
    try:
        # What goes in place is the file written, not one put at its name since.
        if not _is_own(temporary, own):
            raise PatchError(_IN_THE_WAY.format(name=temporary.name))
        if not new:
            os.replace(temporary, path)
        else:
            try:
                # Neither replaces a file there. ponytail: POSIX goes by a hard link, so a
                # file system without them refuses the install; renameat2 if that matters.
                (os.rename if os.name == "nt" else os.link)(temporary, path)
            except FileExistsError as error:
                raise PatchError(_IN_THE_WAY.format(name=path.name)) from error
    finally:
        _remove_own(temporary, own)  # after a hard link, only the scratch name goes
    return own


def _write_new(path: Path, data: bytes) -> os.stat_result:
    """Make ``path``, list it in the lock file, and write ``data`` through the handle made.

    Written through that handle, the bytes go to the file made, whatever is put at its name
    meanwhile. Should the write stop, that file goes again.
    """
    with path.open("xb") as out:
        own = os.fstat(out.fileno())
        try:
            _journal(path, own)
            out.write(data)
        except BaseException:
            out.close()  # Windows takes no open file away
            _remove_own(path, own)
            raise
    return own


def _is_own(path: Path, own: os.stat_result) -> bool:
    """Whether ``path`` is still the very file ``own`` describes, and not a link."""
    try:
        return os.path.samestat(os.lstat(path), own)
    except OSError:
        return False


def _remove_own(path: Path, own: os.stat_result) -> None:
    """Take ``path`` away while it is the file the patcher made: any other there stays."""
    if _is_own(path, own):
        with suppress(OSError):
            path.unlink()


def _retitle(folder: Path, recipe: bytes, log: Log | None) -> None:
    """Put the translated title in the window, which RPG Maker reads in the player's ANSI code page.

    Only the ``Title=`` line changes: the game writes its own settings into Game.ini as it runs.
    """
    with ZipFile(io.BytesIO(recipe)) as archive:
        title = json.loads(archive.read("recipe.json")).get("title")
    ini = folder / GAME_INI
    if not isinstance(title, str) or not title:
        return
    if _link_on_the_way(folder, ini) is not None:  # read or written, it would be elsewhere
        _report(log, f"無法改寫 {GAME_INI} 的視窗標題，標題保留原文；翻譯已經套用。")
        return
    try:
        lines = ini.read_bytes().splitlines(keepends=True)
    except OSError:
        return
    index = _title_line(lines)
    if index is None:
        return
    encoding = locale.getencoding()
    try:
        value = title.encode(encoding)
    except UnicodeEncodeError:
        _report(log, f"這台電腦的系統編碼（{encoding}）顯示不了翻譯後的標題，視窗標題保留原文。")
        return
    original = lines[index].rstrip(b"\r\n").partition(b"=")[2]
    if value == original:
        return
    lines[index] = _with_title(lines[index], value)
    try:
        record = folder / RECORD
        document = read_record(folder)
        document["title"] = original.hex()
        _rewrite(record, json.dumps(document).encode("utf-8"))
        _rewrite(ini, b"".join(lines))
    except (OSError, ValueError, PatchError):  # the translation is in place: only the title stays
        _report(log, f"無法改寫 {GAME_INI} 的視窗標題，標題保留原文；翻譯已經套用。")


def _restore_title(folder: Path) -> None:
    """Put back the title :func:`_retitle` replaced, keeping the settings the game wrote since."""
    if _link_on_the_way(folder, folder / GAME_INI) is not None:  # not this game's file
        return
    try:
        original = bytes.fromhex(
            read_record(folder).get("title")
        )
        lines = (folder / GAME_INI).read_bytes().splitlines(keepends=True)
    except (OSError, ValueError, AttributeError, TypeError):
        return
    index = _title_line(lines)
    # A title apply could not change is not written again: nothing would change.
    if index is not None and lines[index] != (titled := _with_title(lines[index], original)):
        lines[index] = titled
        _rewrite(folder / GAME_INI, b"".join(lines))


def _install(folder: Path, patch: Patch, prepared: dict[str, Path] | None = None) -> None:
    _refuse_install(folder, patch)
    staged: list[tuple[Path, os.stat_result]] = []
    try:
        for item in patch.files:
            if item.patched is not None:
                _stage(folder, item, prepared, staged)
        _commit_install(folder, patch, staged)
    except ValueError as error:
        raise PatchError(_DAMAGED) from error
    except PermissionError as error:
        raise _refusal(error, folder) from error
    finally:
        for temporary, own in staged:
            _remove_own(temporary, own)


def _refuse_link(folder: Path, path: Path) -> None:
    """Refuse a path the patch writes when it, or a folder between it and ``folder``, is a link.

    A link pointing nowhere reads as missing, yet a write through it lands where it points,
    which can be outside the game folder. ``folder`` itself may be a link.
    """
    if (link := _link_on_the_way(folder, path)) is not None:
        raise PatchError(
            f"「{link.relative_to(folder).as_posix()}」是一個連結，補丁不會寫進連結"
            "指向的位置。請先把它移到別處，再試一次。"
        )


def _link_on_the_way(folder: Path, path: Path) -> Path | None:
    """``path``, or a folder between it and ``folder``, that is a link."""
    for candidate in (path, *path.parents):
        if candidate == folder:
            return None
        if candidate.is_symlink() or candidate.is_junction():
            return candidate
    return None


def _refuse_install(folder: Path, patch: Patch) -> None:
    """Stop before anything is written unless the folder holds the original, with room."""
    # Two additions at one name, in any case, would both be recorded, and the one written
    # last would stand for both. One at a name the patcher keeps for itself would overwrite
    # its record, its lock, what it stages, or the original it keeps. Windows reads a name
    # ending in a dot or a space as the name without it, and one with a colon as a stream
    # of another file, so only names that are what they say are compared.
    names = [item.name.casefold() for item in patch.added]
    kept = {RECORD.casefold(), LOCK.casefold()}
    kept |= {f"{item.name}.bak".casefold() for item in patch.files}
    if len(set(names)) != len(names) or any(
        not _inside(name) or name in kept or name.endswith(_SCRATCH) for name in names
    ):
        raise PatchError(_DAMAGED)
    # A record here that is not this patch's own reads as damaged or another's: another
    # patch's entries kept in this patch's record would go with this patch's files.
    found = inspect(folder, patch)
    if found.state is not State.ORIGINAL:
        raise PatchError(describe(found, patch))
    writes = [folder / item.name for item in patch.added]
    if patch.added or patch.recipe is not None:
        writes.append(folder / RECORD)
    # Each is written beside itself first and renamed in place.
    scratch = [_temporary_path(path) for path in writes]
    scratch += [_temporary_path(folder / i.name) for i in patch.files if i.patched is not None]
    for path in writes + scratch:
        _refuse_link(folder, path)
    for path in writes:
        # Found missing only once the record is out, it would stop every apply there.
        if not path.parent.is_dir():
            raise PatchError(_NO_FOLDER.format(folder=path.parent.relative_to(folder).as_posix()))
    for path in scratch:
        if path.exists():
            raise PatchError(_IN_THE_WAY.format(name=path.relative_to(folder).as_posix()))
    for item in patch.files:
        target = folder / item.name
        _require_not_in_use(target)
        backup = backup_path(target)
        if (
            item.patched is not None
            and os.path.lexists(backup)  # a link pointing nowhere too
            and not _backup_is_original(folder, item)
        ):
            raise PatchError(
                f"「{backup.name}」已經存在，而且不是原版。請先把它移到別處，再試一次。"
            )
    for item in patch.added:
        existing = folder / item.name
        if existing.exists() and sha256_of(existing) != _digest(item.data):
            raise PatchError(_OTHER_FILE.format(name=item.name))
    needed = (
        sum(item.patched.size for item in patch.files if item.patched is not None)
        + sum(len(item.data) for item in patch.added)
        + SPARE_BYTES
    )
    if shutil.disk_usage(folder).free < needed:
        raise PatchError("磁碟空間不足：套用時需要再多一份遊戲檔案的空間。")


def _stage(
    folder: Path,
    item: PatchedFile,
    prepared: dict[str, Path] | None,
    staged: list[tuple[Path, os.stat_result]],
) -> None:
    """Write the translated file beside the original, and check it came out as built."""
    target = folder / item.name
    temporary = _temporary_path(target)
    try:
        out = temporary.open("xb")
    except FileExistsError as error:
        raise PatchError(_IN_THE_WAY.format(name=temporary.name)) from error
    with out:
        own = os.fstat(out.fileno())
        staged.append((temporary, own))
        _journal(temporary, own)
        if prepared is not None:
            with prepared[item.name].open("rb") as source:
                shutil.copyfileobj(source, out)
        else:
            digest = apply_delta(target.read_bytes(), item.delta, item.window_log, out)
    if prepared is not None:
        digest = sha256_of(temporary)
    assert item.patched is not None
    if digest != item.patched.sha256:
        raise PatchError(_DAMAGED)


def _commit_install(
    folder: Path, patch: Patch, staged: list[tuple[Path, os.stat_result]]
) -> None:
    """Add the new files and swap the staged ones in, or leave the folder as it was."""
    record = folder / RECORD
    before = record.read_bytes() if record.is_file() else None
    # What the record lists already comes from this one read: read again in vain, it would
    # list nothing, and what a stopped install put in would be left out of the record.
    try:
        listed = [] if before is None else _shaped(json.loads(before))
    except (ValueError, RecursionError):  # damaged since the checks
        raise PatchError(_DAMAGED_RECORD_FOUND) from None
    # Only a file not there yet is this patch's to add, and to take away again: one there
    # already by the same content was there before the patch, and stays. One put there
    # since the checks, while the game files were staged, is looked at again.
    fresh = []
    for item in patch.added:
        if not os.path.lexists(folder / item.name):
            fresh.append(item)
        elif sha256_of(folder / item.name) != _digest(item.data):
            raise PatchError(_OTHER_FILE.format(name=item.name))
    wrote: list[tuple[Path, os.stat_result, str]] = []
    moved: list[Path] = []
    published = False
    try:
        # The record goes first: a process stopped between writes cannot roll back, and a file
        # added but not recorded would be one restore cannot take away.
        if fresh or patch.recipe is not None:
            _record(folder, patch, listed, fresh)
            published = True
        for item in fresh:
            path = folder / item.name
            # Whole or not at all, should the process stop, and only at a name still free;
            # once there, it is this install's.
            wrote.append((path, _rewrite(path, item.data, new=True), _digest(item.data)))
        # What goes in place of a game file is the file staged, not one put at its name since.
        for temporary, own in staged:
            if not _is_own(temporary, own):
                raise PatchError(_IN_THE_WAY.format(name=temporary.name))
        _swap(folder, patch, moved)
    except BaseException:
        # An original not put back leaves its translation in place: the record saying what
        # was compiled stays, so the translation is never read as the game's own.
        stays = bool(moved)
        for path, own, digest in wrote:
            try:
                # Not another program's since, nor edited by one in place.
                if os.path.samestat(os.lstat(path), own) and sha256_of(path) == digest:
                    path.unlink()
            except (FileNotFoundError, NotADirectoryError):  # gone
                pass
            except OSError:  # not known to be gone, or it would not go: restore takes it away
                stays = True
        # The record before stays untouched unless the new one replaced it, and then goes
        # back whole: a write cut short would leave neither. A step here that fails leaves
        # the new record, which restore clears, and never hides why the install stopped.
        if published and not stays:
            with suppress(OSError, PatchError):
                if before is None:
                    record.unlink(missing_ok=True)
                else:
                    _rewrite(record, before)
        raise


def _swap(folder: Path, patch: Patch, moved: list[Path]) -> None:
    """Move each original aside and its translation in; put the originals back if one fails.

    ``moved`` is left holding each game file whose original is aside and not put back.
    """
    try:
        for item in patch.files:
            if item.patched is None:
                continue
            target = folder / item.name
            os.replace(target, backup_path(target))
            moved.append(target)
            os.replace(_temporary_path(target), target)
    except OSError:
        for target in reversed(moved[:]):
            # One that will not go back stays for restore; the others still go back, and
            # what stopped the swap is what is reported.
            with suppress(OSError):
                os.replace(backup_path(target), target)
                moved.remove(target)
        raise


def restore(folder: Path, patch: Patch, log: Log | None = None) -> list[str]:
    """Put each original back from its ``<name>.bak``; take away what a patch added."""
    try:
        with _one_at_a_time(folder):
            return _restore(folder, patch, log)
    except _Absent:
        raise PatchError(describe(FolderState(State.MISSING, False), patch)) from None
    except OSError as error:  # what no step put into words
        raise PatchError(_UNREADABLE.format(error=error)) from error


def _restore(folder: Path, patch: Patch, log: Log | None) -> list[str]:
    _report(log, "正在檢查還原備份。")
    if patch.mount is not None:
        from bokucho.patcher.mount import mount_text, restore_mount

        _report(log, mount_text(patch).removing)
        return restore_mount(folder, patch)
    _report(log, "正在還原原版遊戲檔案。")
    found = inspect(folder, patch)
    if found.state is State.LINKED:
        raise PatchError(describe(found, patch))
    damaged = False
    try:
        added_files(folder)
    except ValueError as error:
        #: An add-only patch leaves only what it added: left behind unrecorded, the game
        #: still loads it and Bokucho reads it as the game's own. Any other patch has the
        #: originals in its backups, and those go back whatever the record says.
        if _add_only(patch):
            raise PatchError(_DAMAGED_RECORD) from error
        damaged = True
    if not found.restorable:
        raise PatchError("找不到可以還原的原版備份（.bak）。遊戲檔案沒有被改動。")
    for item in patch.files:
        if (folder / item.name).exists():
            _require_not_in_use(folder / item.name)
    # Marked, the record reads as whole: a restore stopped after the mark is the last to
    # find it damaged, so the warning is given before anything changes.
    if damaged:
        _report(log, _DAMAGED_RECORD_RESTORING)
    try:
        listed, others = _mark_as_a_restores(folder, patch)
        _restore_title(folder)
        for item in patch.files:
            if _backup_is_original(folder, item):
                target = folder / item.name
                os.replace(backup_path(target), target)
    except PermissionError as error:
        raise _refusal(error, folder) from error
    except FileExistsError as error:
        raise PatchError(_IN_THE_WAY.format(name=Path(error.filename).name)) from error
    # An original another restore is still to put back stays its to put back: the record,
    # and what it lists, stay too, or its translation would be read as the game's own.
    if not all(_holds(folder, name, digest) for name, digest in others.items()):
        raise PatchError(_OTHER_RECORD)
    left = []
    held: PermissionError | None = None
    # The game's own files and their backups stay, whatever a record lists: this patch's,
    # and those an earlier restore put back.
    kept = {
        name.casefold()
        for original in [*(item.name for item in patch.files), *others]
        for name in (original, f"{original}.bak")
    }
    for entry in listed:
        path = folder / entry["name"]
        if entry["name"].casefold() in kept:
            continue
        # Reached through a link, the file is outside the game folder: not the patch's. A
        # link pointing nowhere reads as missing, yet stays in the way of the next apply.
        if _link_on_the_way(folder, path) is not None:
            left.append(entry["name"])
            continue
        if not path.exists():
            continue
        try:
            if sha256_of(path) == entry["sha256"]:
                path.unlink()
                continue
        except PermissionError as error:
            # Held open, it may be the patch's still: the record stays for the next restore,
            # which takes it away once it is let go. A file changed since is left for good.
            held = error
            continue
        left.append(entry["name"])
    if held is not None:
        raise _refusal(held, folder) from held
    (folder / RECORD).unlink(missing_ok=True)
    return left


def _recipe_engine(patch: Patch) -> str:
    assert patch.recipe is not None
    with ZipFile(io.BytesIO(patch.recipe)) as archive:
        return str(json.loads(archive.read("recipe.json")).get("engine", ""))


def applied_message(patch: Patch, folder: Path | None = None) -> str:
    message = "套用完成，可以開始遊戲了。"
    if patch.mount is not None:
        return message + "\n需要還原時，請再次開啟此補丁並按「還原」。"
    names = [item.name for item in patch.added]
    if patch.recipe is not None and folder is not None:
        names.extend(item["name"] for item in _recorded(folder))
    #: An RGSS game asks for its fill font itself; it has no font menu to visit.
    if patch.recipe is not None and _recipe_engine(patch).startswith("rpgmaker-"):
        names = []
    for name in names:
        if name.lower().endswith(FONT_SUFFIXES):
            family = PurePath(name).stem
            message += (
                f"\n如果以前在遊戲選項裡換過字型，進遊戲後請到「オプション → フォント選択」"
                f"選擇「{family}」。"
            )
    return message


def restored_message(left: list[str]) -> str:
    message = "已經還原成原版。"
    if left:
        message += (
            f"\n這些檔案套用後被改過、無法刪除，或在遊戲資料夾外，所以沒有刪除：{'、'.join(left)}。"
        )
    return message
