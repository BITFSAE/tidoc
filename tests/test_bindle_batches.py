"""绑定包补充材料与批次归属分别处理，使用虚构发票。"""
from decimal import Decimal

import pytest

from tidoc.db import AttachmentRepo, BatchRepo, Database, DataRoot, EntryRepo, ProfileRepo
from tidoc.engine.models import ParsedInvoice
from tidoc.services.bindle import export_bindle, import_bindle, inspect_bindle


@pytest.fixture
def bindle_batch_case(repos, tmp_path):
    profile = repos['profiles'].create('包内报账人', '包内审核人')
    numbers = ['SUPPLEMENT', 'UNCHANGED', 'UNBATCHED', 'EXCLUDED', 'NEW']
    source_ids = [repos['entries'].create(profile['id'], parsed=ParsedInvoice(
        invoice_no=number, total=Decimal('12.00'))) for number in numbers]
    repos['entries'].update_field(source_ids[0], 'notes', '补交说明', profile['id'])
    package = export_bindle(repos['entries'], repos['attachments'], source_ids,
                            tmp_path / '批次测试.tidoc', {profile['id']: profile})
    root = DataRoot(tmp_path / 'target')
    db = Database(root.db_path)
    profiles = ProfileRepo(db)
    local_profile = profiles.create('本机报账人', '本机审核人')
    entries, attachments, batches = EntryRepo(db), AttachmentRepo(db, root), BatchRepo(db)
    existing_ids = [entries.create(local_profile['id'], parsed=ParsedInvoice(
        invoice_no=number, total=Decimal('12.00'))) for number in numbers[:-1]]
    old = batches.create('原批次', entry_ids=[existing_ids[0], existing_ids[3]])
    archived = batches.create('归档批次', entry_ids=[existing_ids[1]])
    batches.set_archived(archived['id'])
    db.conn.execute('UPDATE batch_entries SET note = ? WHERE entry_id = ?',
                    ('原催办备注', existing_ids[0]))
    db.conn.commit()
    target = batches.create('指定批次')
    yield dict(package=package, db=db, entries=entries, attachments=attachments,
               batches=batches, profile=local_profile, existing_ids=existing_ids,
               old=old, archived=archived, target=target)
    db.close()


def run_import(case, **options):
    return import_bindle(case['entries'], case['attachments'], case['package'],
                         case['profile']['id'], options={
                             'selected_entry_indexes': [0, 1, 2, 4], **options,
                         })


def entry_batches(case, entry_id):
    return [batch['id'] for batch in case['entries'].get(entry_id)['batches']]


@pytest.mark.parametrize('new_batch', [False, True])
@pytest.mark.parametrize('move_existing', [False, True])
def test_global_target_preserves_existing_unless_movement_is_explicit(
    bindle_batch_case, new_batch, move_existing,
):
    c = bindle_batch_case
    preview = inspect_bindle(c['package'], c['entries'])
    assert [entry['import_action'] for entry in preview['entries']] == [
        'merge', 'unchanged', 'unchanged', 'unchanged', 'new',
    ]
    assert preview['entries'][0]['existing_profile'] == {
        'id': c['profile']['id'], 'name': '本机报账人', 'reviewer': '本机审核人',
    }
    assert preview['entries'][1]['existing_batches'][0]['archived'] is True
    result = run_import(c, move_existing_entries=move_existing,
                        **({'batch_name': '本次新批次'} if new_batch else {'batch_id': c['target']['id']}))
    assert result['imported'] == 1
    assert result['updated'] == 1
    target_id = result['batch_id']
    assert target_id
    assert entry_batches(c, result['entry_ids'][0]) == [target_id]
    for index, original in enumerate(([c['old']['id']], [c['archived']['id']], [])):
        assert entry_batches(c, c['existing_ids'][index]) == ([target_id] if move_existing else original)
        assert c['entries'].get(c['existing_ids'][index])['profile_id'] == c['profile']['id']
    assert entry_batches(c, c['existing_ids'][3]) == [c['old']['id']]
    assert c['entries'].get(c['existing_ids'][0])['fields']['notes']['current'] == '补交说明'
    if not move_existing:
        assert c['db'].conn.execute('SELECT note FROM batch_entries WHERE entry_id = ?',
                                    (c['existing_ids'][0],)).fetchone()['note'] == '原催办备注'


def test_individual_choices_override_global_move_and_no_unused_batch_is_created(bindle_batch_case):
    c = bindle_batch_case
    result = run_import(c, batch_name='不应创建', move_existing_entries=True,
                        entry_batch_overrides={
                            '0': {'mode': 'keep'},
                            '1': {'mode': 'none'},
                            '2': {'mode': 'existing', 'batch_id': c['target']['id']},
                            '4': {'mode': 'new', 'batch_name': '单独新建'},
                        })
    assert entry_batches(c, c['existing_ids'][0]) == [c['old']['id']]
    assert entry_batches(c, c['existing_ids'][1]) == []
    assert entry_batches(c, c['existing_ids'][2]) == [c['target']['id']]
    assert c['entries'].get(result['entry_ids'][0])['batches'][0]['name'] == '单独新建'
    assert c['db'].conn.execute("SELECT COUNT(*) FROM batches WHERE name='不应创建'").fetchone()[0] == 0
    assert result['individual_batch_assignments'] == 3


def test_only_existing_entries_do_not_create_empty_global_batch_even_when_tags_change(bindle_batch_case):
    c = bindle_batch_case
    result = run_import(c, selected_entry_indexes=[0, 1, 2], batch_name='无需新建', tags=['收齐'])
    assert result['batch_created'] is False
    assert result['updated'] == 3
    assert c['db'].conn.execute("SELECT COUNT(*) FROM batches WHERE name='无需新建'").fetchone()[0] == 0
    assert entry_batches(c, c['existing_ids'][0]) == [c['old']['id']]
    assert entry_batches(c, c['existing_ids'][1]) == [c['archived']['id']]
    assert entry_batches(c, c['existing_ids'][2]) == []


@pytest.mark.parametrize('options', [
    {'move_existing_entries': True},
    {'move_existing_entries': 'false', 'batch_name': '目标'},
])
def test_invalid_movement_settings_do_not_write(bindle_batch_case, options):
    c = bindle_batch_case
    with pytest.raises(ValueError):
        run_import(c, **options)
    fields = c['entries'].get(c['existing_ids'][0])['fields']
    assert not fields.get('notes', {}).get('current')
    assert len(c['entries'].list()) == 4


def test_failure_after_batch_movement_rolls_back_material_and_batch_changes(bindle_batch_case, monkeypatch):
    from tidoc.services import bindle_import
    c = bindle_batch_case
    original = bindle_import._assign_batches

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('模拟失败')

    monkeypatch.setattr(bindle_import, '_assign_batches', interrupted)
    with pytest.raises(RuntimeError, match='模拟失败'):
        run_import(c, batch_name='回滚批次', move_existing_entries=True)
    assert entry_batches(c, c['existing_ids'][0]) == [c['old']['id']]
    assert entry_batches(c, c['existing_ids'][1]) == [c['archived']['id']]
    assert entry_batches(c, c['existing_ids'][2]) == []
    assert len(c['entries'].list()) == 4
    assert c['db'].conn.execute("SELECT COUNT(*) FROM batches WHERE name='回滚批次'").fetchone()[0] == 0


def test_retaining_exchange_provenance_does_not_count_as_content_supplement(api, tmp_path):
    profile = api.profiles.create('虚构报账人', '虚构审核人')
    entry_id = api.entries.create(profile['id'], parsed=ParsedInvoice(
        invoice_no='UNCHANGED-WITH-SCHEME', total=Decimal('12.00')))
    package = export_bindle(api.entries, api.attachments, [entry_id],
                            tmp_path / '原样交接.tidoc', {profile['id']: profile})
    preview = inspect_bindle(package, api.entries)
    assert preview['entries'][0]['import_action'] == 'unchanged'
    result = import_bindle(api.entries, api.attachments, package, profile['id'], inspected=preview)
    assert result['imported'] == result['updated'] == 0
    assert len(result['skipped']) == 1
    assert api.entries.get(entry_id)['adapter_sources']
    again = import_bindle(api.entries, api.attachments, package, profile['id'])
    assert again['updated'] == 0
