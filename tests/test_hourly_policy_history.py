"""Расширение корпуса не переписывает exact политику прежних записей."""
import json
from types import SimpleNamespace

import pytest

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.article_sources import policy_for_record, registered_policies
from max_postiz_agent.config import AgentError, fingerprint
from test_article_sources import URLS, base_policy as base_policy, configured_sources


def save(settings, policy):
    directory = settings.root / 'data/hourly-policy-history'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (fingerprint(policy) + '.local.json')
    path.write_text(json.dumps(policy), encoding='utf-8')
    return path


@pytest.mark.parametrize('marker', [None, 'article-illustrated-v1', 'article-illustrated-v2'])
@pytest.mark.parametrize('source', ['nasa_svs_articles', 'noirlab_iotw', 'esahubble_potw'])
def test_exact_old_policy_restored_after_explicit_five_year_change(tmp_path, base_policy, marker, source):
    settings, _ = configured_sources()
    settings.root = tmp_path
    previous = dict(base_policy)
    if marker:
        previous.update(presentation_contract=marker, presentation_owner_instruction='Exact original image and bold instruction')
    policy = next(p for p in registered_policies(previous, settings) if p['source_id'] == source)
    current = {**base_policy, 'max_source_age_hours': 43800,
               'owner_instruction': base_policy['owner_instruction'] + ' Owner permits archive up to five years.',
               'presentation_contract': 'article-illustrated-v2',
               'presentation_owner_instruction': 'Exact original image and bold instruction'}
    record = {'source_id': source, 'url': URLS.get(source, 'https://svs.gsfc.nasa.gov/12345/'),
              'policy_hash': fingerprint(policy)}
    if marker:
        record['presentation_contract'] = marker
    path = save(settings, policy)
    before = path.read_bytes()
    assert policy_for_record(current, settings, record) == policy
    assert path.read_bytes() == before
    fresh = {k: v for k, v in record.items() if k != 'policy_hash'}
    assert policy_for_record(current, settings, fresh)['max_source_age_hours'] == 43800


@pytest.mark.parametrize('change', ['tamper', 'duplicate_key', 'symlink', 'wrong_target', 'wrong_rights', 'wrong_marker', 'oversize'])
def test_archive_cannot_replace_pinned_permissions_or_presentation(tmp_path, base_policy, change):
    settings, _ = configured_sources()
    settings.root = tmp_path
    old = dict(base_policy)
    current = {**base_policy, 'max_source_age_hours': 43800}
    if change == 'wrong_target':
        old['target_id'] = 'other'
    if change == 'wrong_rights':
        old['rights_evidence'] = 'https://untrusted.invalid/'
    if change == 'wrong_marker':
        old.update(presentation_contract='article-illustrated-v1', presentation_owner_instruction='Original owner quote')
    path = save(settings, old)
    record = {'source_id': 'nasa_svs_articles', 'url': 'https://svs.gsfc.nasa.gov/12345/', 'policy_hash': fingerprint(old)}
    if change == 'tamper':
        path.write_text('{}')
    if change == 'duplicate_key':
        path.write_text('{"enabled":true,"enabled":false}')
    if change == 'oversize':
        path.write_bytes(b'x' * (64 * 1024 + 1))
    if change == 'symlink':
        other = tmp_path / 'other.json'
        other.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(other)
        except OSError:
            pytest.skip('Symlink privilege unavailable on this host')
    with pytest.raises(AgentError, match='ARTICLE_POLICY_HISTORY_INVALID'):
        policy_for_record(current, settings, record)


@pytest.mark.parametrize('age', [72, 8760, 8761, 43800])
def test_native_policy_accepts_explicit_age_up_to_five_years(tmp_path, base_policy, age):
    path = tmp_path / 'data/hourly-article-policy.local.json'
    path.parent.mkdir()
    path.write_text(json.dumps({**base_policy, 'max_source_age_hours': age}))
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    assert cycle._policy()['max_source_age_hours'] == age


@pytest.mark.parametrize('age', [True, 71, 43801, 43800.0, '43800'])
def test_native_policy_still_rejects_unbounded_or_invalid_age(tmp_path, base_policy, age):
    path = tmp_path / 'data/hourly-article-policy.local.json'
    path.parent.mkdir()
    path.write_text(json.dumps({**base_policy, 'max_source_age_hours': age}))
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    with pytest.raises(AgentError, match='ARTICLE_POLICY_INVALID'):
        cycle._policy()


def science_age_file(root, contents):
    path = root / 'data/hourly-science-age.local.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding='utf-8')
    return path


@pytest.mark.parametrize('source', ['noirlab_iotw', 'esahubble_potw'])
@pytest.mark.parametrize('marker', [None, 'article-illustrated-v1', 'article-illustrated-v2'])
def test_science_age_extension_keeps_base_and_pinned_history(tmp_path, base_policy, source, marker):
    settings, _ = configured_sources()
    settings.root = tmp_path
    base = {**base_policy, 'max_source_age_hours': 43800}
    if marker:
        base.update(presentation_contract=marker, presentation_owner_instruction='Exact original image and bold instruction')
    base_hash = fingerprint(base)
    previous = next(p for p in registered_policies(base, settings) if p['source_id'] == source)
    historical = {'source_id': source, 'url': URLS[source], 'policy_hash': fingerprint(previous)}
    if marker:
        historical['presentation_contract'] = marker
    archive = save(settings, previous)
    archived_bytes = archive.read_bytes()
    science_age_file(tmp_path, '{"noirlab_iotw":87600,"esahubble_potw":87600}')

    expanded = registered_policies(base, settings)
    assert expanded[0] == base and fingerprint(base) == base_hash
    assert {p['source_id']: p['max_source_age_hours'] for p in expanded} == {
        'nasa_svs_articles': 43800, 'noirlab_iotw': 87600, 'esahubble_potw': 87600,
    }
    current = next(p for p in expanded if p['source_id'] == source)
    assert current == {**previous, 'max_source_age_hours': 87600}
    assert policy_for_record(base, settings, historical) == previous
    fresh = {key: value for key, value in historical.items() if key != 'policy_hash'}
    assert policy_for_record(base, settings, fresh) == current
    assert archive.read_bytes() == archived_bytes


def test_science_age_is_optional_per_registered_automatic_source(tmp_path, base_policy):
    settings, sources = configured_sources()
    settings.root = tmp_path
    science_age_file(tmp_path, '{"noirlab_iotw":87600}')
    effective = registered_policies(base_policy, settings)
    assert [item['max_source_age_hours'] for item in effective] == [8760, 87600, 8760]
    sources['noirlab_iotw']['enabled'] = False
    assert [item['source_id'] for item in registered_policies(base_policy, settings)] == [
        'nasa_svs_articles', 'esahubble_potw',
    ]
    manual = {**base_policy, 'approval_mode': 'human'}
    assert registered_policies(manual, settings) == [manual]


@pytest.mark.parametrize('contents', [
    '', '{}', '[]', 'null', '{"nasa_svs_articles":87600}', '{"unregistered":87600}',
    '{"noirlab_iotw":true}', '{"noirlab_iotw":87600.0}', '{"noirlab_iotw":"87600"}',
    '{"noirlab_iotw":71}', '{"noirlab_iotw":87601}', '{"noirlab_iotw":NaN}',
    '{"noirlab_iotw":87600,"noirlab_iotw":43800}', '{"noirlab_iotw":87600}' + ' ' * 4096,
])
def test_science_age_rejects_invalid_private_override(tmp_path, base_policy, contents):
    settings, _ = configured_sources()
    settings.root = tmp_path
    science_age_file(tmp_path, contents)
    with pytest.raises(AgentError, match='ARTICLE_SCIENCE_AGE_INVALID'):
        registered_policies(base_policy, settings)


@pytest.mark.parametrize('link_target', ['file', 'directory'])
def test_science_age_rejects_symlink_path(tmp_path, base_policy, link_target):
    settings, _ = configured_sources()
    settings.root = tmp_path / 'runtime'
    external = science_age_file(tmp_path / 'outside', '{"noirlab_iotw":87600}')
    if link_target == 'file':
        link, target = settings.root / 'data' / external.name, external
    else:
        link, target = settings.root / 'data', external.parent
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=link_target == 'directory')
    except OSError:
        pytest.skip('Symlink privilege unavailable on this host')
    with pytest.raises(AgentError, match='ARTICLE_SCIENCE_AGE_INVALID'):
        registered_policies(base_policy, settings)
