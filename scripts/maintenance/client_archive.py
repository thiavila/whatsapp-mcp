"""Bounded extraction shared by the CI builder and the admitted client executor."""
import hashlib
from pathlib import Path, PurePosixPath
import tarfile

MAX_ARCHIVE = 160 * 1024**2
MAX_EXPANDED = 512 * 1024**2


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def extract(path, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('extraction destination must be new')
    if not 0 < Path(path).stat().st_size <= MAX_ARCHIVE:
        raise ValueError('client archive size')
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        if len(members) > 15000 or sum(m.size for m in members) > MAX_EXPANDED:
            raise ValueError('expanded client archive size')
        names = set()
        for m in members:
            parts = PurePosixPath(m.name).parts
            canonical = PurePosixPath(m.name).as_posix()
            if not parts or m.name.startswith('/') or '..' in parts or canonical in names:
                raise ValueError('unsafe client archive member')
            names.add(canonical)
            if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                raise ValueError('special client archive member')
            if m.issym() or m.islnk():
                target = PurePosixPath(m.linkname)
                if target.is_absolute():
                    raise ValueError('absolute client archive link')
                stack = list(parts[:-1]) if m.issym() else []
                for part in target.parts:
                    if part == '..':
                        if not stack:
                            raise ValueError('escaping client archive link')
                        stack.pop()
                    elif part != '.':
                        stack.append(part)
        destination.mkdir(mode=0o700)
        archive.extractall(destination, members=members, filter='data')


def sync_tree(destination):
    """Complete data+directory durability before publishing an extracted tree."""
    import os
    for current, directories, files in os.walk(destination, topdown=False, followlinks=False):
        root = Path(current)
        for name in files:
            path = root / name
            if path.is_symlink():
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try: os.fsync(fd)
            finally: os.close(fd)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)


def verify_tree(path, destination):
    """Reject modified extracted code, links and extra files before execution."""
    destination = Path(destination)
    expected = set()
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive.getmembers():
            target = destination / member.name
            expected.add(target)
            if member.isdir():
                if target.is_symlink() or not target.is_dir():
                    raise ValueError('client directory changed')
            elif member.issym():
                if not target.is_symlink() or target.readlink().as_posix() != member.linkname:
                    raise ValueError('client link changed')
            else:
                if target.is_symlink() or not target.is_file():
                    raise ValueError('client file changed')
                with archive.extractfile(member) as stream:
                    wanted = hashlib.file_digest(stream, 'sha256').hexdigest()
                if digest(target) != wanted:
                    raise ValueError('client extracted bytes changed')
    if set(destination.rglob('*')) != expected:
        raise ValueError('unexpected extracted client files')
