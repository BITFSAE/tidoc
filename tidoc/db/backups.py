"""Database copies written before a schema upgrade: listing and pruning only touch those files."""
from __future__ import annotations

from pathlib import Path

BACKUP_GLOB = 'tidoc-before-*.sqlite'
BACKUP_KEEP = 2


def list_backups(backups_dir) -> list[dict]:
    """Upgrade backups, newest first. Other files in the folder are never listed."""
    root = Path(backups_dir)
    if not root.is_dir():
        return []
    items = []
    for path in root.glob(BACKUP_GLOB):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        items.append({'path': path, 'name': path.name, 'size': stat.st_size, 'modified': stat.st_mtime})
    return sorted(items, key=lambda item: (item['modified'], item['name']), reverse=True)


def prune_backups(backups_dir, keep: int = BACKUP_KEEP) -> dict:
    """Delete all but the newest `keep` upgrade backups. At least one backup always remains."""
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError('至少需要保留 1 份备份。')
    removed = []
    released = 0
    for item in list_backups(backups_dir)[keep:]:
        try:
            item['path'].unlink()
        except FileNotFoundError:
            continue
        removed.append(item['name'])
        released += item['size']
    return {'removed': removed, 'count': len(removed), 'size': released}
