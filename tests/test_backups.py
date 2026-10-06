"""Upgrade backups: one copy per start, none for an empty database, and explicit pruning."""
import os
import tempfile

import pytest

from tests.test_adapter_storage import legacy_db
from tidoc.adapters.service import AdapterService
from tidoc.api import Api
from tidoc.db import Database, DataRoot, ProfileRepo
from tidoc.db.backups import BACKUP_KEEP, list_backups, prune_backups


def make_backups(folder, names):
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        path = folder / name
        path.write_bytes(b'x' * (index + 1))
        os.utime(path, (1_000 + index, 1_000 + index))   # later names are newer


def test_prune_keeps_the_newest_and_only_touches_upgrade_backups(tmp_path):
    folder = tmp_path / 'backups'
    names = [f'tidoc-before-{letter}.sqlite' for letter in 'abcde']
    make_backups(folder, names)
    (folder / 'notes.txt').write_text('mine')
    (folder / 'tidoc-before-x.sqlite.tmp').write_text('partial')
    (folder / 'tidoc-before-dir.sqlite').mkdir()
    outside = tmp_path / 'outside.sqlite'
    outside.write_text('not a backup')
    (folder / 'tidoc-before-link.sqlite').symlink_to(outside)

    assert [item['name'] for item in list_backups(folder)] == names[::-1]
    result = prune_backups(folder, keep=2)
    assert sorted(result['removed']) == names[:3] and result['count'] == 3
    assert result['size'] == 1 + 2 + 3
    assert [item['name'] for item in list_backups(folder)] == ['tidoc-before-e.sqlite', 'tidoc-before-d.sqlite']
    # everything that is not a regular upgrade backup is left alone
    assert (folder / 'notes.txt').read_text() == 'mine'
    assert (folder / 'tidoc-before-x.sqlite.tmp').exists()
    assert (folder / 'tidoc-before-dir.sqlite').is_dir()
    assert (folder / 'tidoc-before-link.sqlite').is_symlink() and outside.read_text() == 'not a backup'
    assert prune_backups(folder, keep=2)['count'] == 0
    for bad in (0, -1, True, '2', None):
        with pytest.raises(ValueError, match='至少'):
            prune_backups(folder, keep=bad)


def test_missing_backup_folder_lists_nothing(tmp_path):
    assert list_backups(tmp_path / 'missing') == []
    assert prune_backups(tmp_path / 'missing')['count'] == 0


def test_upgrade_start_saves_one_backup_not_two(tmp_path):
    root = DataRoot(tmp_path / 'data')
    legacy_db(root.db_path).close()
    db = Database(root.db_path)
    assert db.migration_backup is not None and db.migration_backup.parent == root.backups_dir
    AdapterService(db, root).bootstrap()
    names = [item['name'] for item in list_backups(root.backups_dir)]
    assert len(names) == 1 and names[0].startswith('tidoc-before-v12-')
    db.close()


def test_fresh_install_writes_no_backup_of_an_empty_database(tmp_path):
    root = DataRoot(tmp_path / 'data')
    db = Database(root.db_path)
    assert db.migration_backup is None
    AdapterService(db, root).bootstrap()
    assert list_backups(root.backups_dir) == []
    db.close()


def test_current_schema_with_user_data_but_no_adapters_still_gets_one_backup(tmp_path):
    root = DataRoot(tmp_path / 'data')
    first = Database(root.db_path)
    ProfileRepo(first).create('已有数据', '')
    first.close()
    db = Database(root.db_path)
    assert db.migration_backup is None
    AdapterService(db, root).bootstrap()
    names = [item['name'] for item in list_backups(root.backups_dir)]
    assert len(names) == 1 and names[0].startswith('tidoc-before-adapters-')
    db.close()


def test_api_reports_and_prunes_old_backups():
    api = Api(tempfile.mkdtemp())
    make_backups(api.data_root.backups_dir, [f'tidoc-before-{n}.sqlite' for n in range(4)])
    status = api.storage_maintenance_status()['data']
    assert status['backups'] == 4 and status['old_backups'] == 4 - BACKUP_KEEP
    assert status['old_backups_size'] == 1 + 2
    result = api.cleanup_old_backups()['data']
    assert result['count'] == 2 and result['size'] == 3
    after = api.storage_maintenance_status()['data']
    assert after['backups'] == BACKUP_KEEP and after['old_backups'] == 0
