"""Отменённый до модели источник остаётся в корпусе по exact durable input."""
import copy
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm.attributes import flag_modified

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Material, Story
from max_postiz_agent.server_models import EditorialPreparationClaim, PublicationSlot
from max_postiz_agent.server_nasa import _grams, _used_narratives
from test_illustrated_article_flow import illustrated  # noqa: F401

pytest_plugins = ['test_hourly_reserve_controller']


@pytest.fixture
def cancelled_image(illustrated, monkeypatch):  # noqa: F811 - imported pytest fixture
    import max_postiz_agent.article_images as images
    value = illustrated

    def reject(*_args):
        raise AgentError('ARTICLE_IMAGE_SIZE_INVALID', 409)

    monkeypatch.setattr(images, 'prepare_article_image', reject)
    assert value.controller.prepare_once()['phase'] == 'no_source'
    record = value.cycle._load()['records'][0]
    assert record['state'] == 'cancelled' and not value.events and not value.posts and not value.writes
    assert value.store.active_preparation() is None
    return value


def test_native_pre_model_cancellation_retains_full_source_and_performs_only_select(cancelled_image):
    value = cancelled_image
    ledger = value.cycle._load()
    before = copy.deepcopy(ledger)
    statements = []

    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(value.core.db.engine, 'before_cursor_execute', observed)
    try:
        narratives = _used_narratives(value.core, ledger)
        from max_postiz_agent.server_science import _used_narratives as science_narratives
        assert science_narratives(value.core, ledger) == narratives
    finally:
        event.remove(value.core.db.engine, 'before_cursor_execute', observed)
    assert narratives == [value.article['original']]
    grams = _grams(value.article['original'] + ' A separate URL does not make this a different article.')
    assert len(grams & _grams(narratives[0])) / len(grams) >= .40
    assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements)
    assert value.cycle._load() == before
    assert not (value.core.settings.root / ledger['records'][0]['quality_trace']).exists()
    assert not value.events and not value.posts and not value.writes


@pytest.mark.parametrize('damage', [
    'missing_input', 'raw_input', 'input_hash', 'source_hash', 'url', 'published_at', 'input_path',
    'duplicate_json_key', 'model_unknown', 'model_started', 'partial_image', 'extra_attempt',
    'wrong_state', 'unknown_error', 'trace', 'claim_token', 'claim_unknown', 'missing_binding',
    'missing_cancel_audit', 'missing_bound_audit', 'model_audit', 'false_zero_model_audit',
    'partial_material', 'publication_slot', 'partial_story',
])
def test_missing_corrupt_unknown_or_external_proof_never_uses_input_fallback(cancelled_image, damage):
    value = cancelled_image
    ledger = value.cycle._load()
    record = ledger['records'][0]
    path = value.core.settings.root / record['input_file']
    if damage == 'missing_input':
        path.unlink()
    elif damage == 'raw_input':
        path.write_bytes(path.read_bytes() + b' ')
    elif damage == 'input_hash':
        record['input_file_hash'] = '0' * 64
    elif damage == 'source_hash':
        record['original_hash'] = '0' * 64
    elif damage == 'url':
        record['url'] += 'changed/'
    elif damage == 'published_at':
        record['source_published_at'] += 1
    elif damage == 'input_path':
        record['input_file'] = 'data/hourly-articles/../hourly-articles/' + path.name
    elif damage == 'duplicate_json_key':
        raw = b'{"url":' + json.dumps(value.article['url']).encode() + b',' + path.read_bytes().lstrip()[1:]
        path.write_bytes(raw)
        record['input_file_hash'] = hashlib.sha256(raw).hexdigest()
    elif damage == 'model_unknown':
        record['model_result_unknown'] = True
    elif damage == 'model_started':
        record['preparation_attempts'][0]['model_started_at'] = value.stamp[0]
    elif damage == 'partial_image':
        record['illustration'] = {}
    elif damage == 'extra_attempt':
        record['preparation_attempts'].append(copy.deepcopy(record['preparation_attempts'][0]))
    elif damage == 'wrong_state':
        record['state'] = 'needs_attention'
    elif damage == 'unknown_error':
        record['error'] = 'OPENCODE_GENERATION_FAILED'
    elif damage == 'trace':
        trace = value.core.settings.root / record['quality_trace']
        trace.write_text(json.dumps({'source_untrusted': {'body': value.article['original']}}), encoding='utf-8')
    else:
        with value.core.db.session.begin() as session:
            claim = session.get(EditorialPreparationClaim, record['reserve_claim']['id'])
            if damage == 'claim_token':
                claim.token = 'different-token'
            elif damage == 'claim_unknown':
                claim.state, claim.active_key = 'blocked_unknown', 'models'
            elif damage == 'missing_binding':
                session.delete(session.get(Audit, str(uuid5(NAMESPACE_URL,
                    'max-postiz:reserve-binding:' + record['id']))))
            elif damage in {'missing_cancel_audit', 'false_zero_model_audit'}:
                audit = session.scalar(select(Audit).where(Audit.entity == record['id'],
                    Audit.action == 'reserve_image_rejected_before_models'))
                if damage == 'missing_cancel_audit':
                    session.delete(audit)
                else:
                    audit.details = {**audit.details, 'model_calls': False}
                    # Python considers False == 0; force the JSON tamper to disk.
                    flag_modified(audit, 'details')
            elif damage == 'missing_bound_audit':
                session.delete(session.scalar(select(Audit).where(Audit.entity == claim.id,
                    Audit.action == 'reserve_preparation_bound')))
            elif damage == 'model_audit':
                session.add(Audit(actor='article_quality', action='hourly_article_prepare_attempt',
                    entity=record['id'], details={'attempt': 1}))
            elif damage == 'partial_material':
                session.add(Material(source_id=record['source_id'], external_id=record['url'], story_id='partial',
                    title=value.article['title'], text='partial changed source', url=record['url'],
                    text_hash='1' * 64, published_at=value.article['published_at'], language='en'))
            elif damage == 'publication_slot':
                session.add(PublicationSlot(target_id=record['target_id'], slot_at=1, record_id=record['id'],
                    text_hash='1' * 64, evidence_hash='2' * 64, binding={},
                    control_revision=claim.control_revision, due_at=value.stamp[0]))
            else:
                session.add(Story(title='partial', text='partial',
                    text_hash=fingerprint({'hourly_article': record['id']})))
    value.cycle._save(ledger)
    before = copy.deepcopy(value.cycle._load())
    with pytest.raises(AgentError, match='NASA_DEDUP_CORPUS_INCOMPLETE'):
        _used_narratives(value.core, before)
    assert value.cycle._load() == before
    assert not value.events and not value.posts and not value.writes


def test_a_stale_or_forged_ledger_copy_does_not_match_the_durable_record(cancelled_image):
    value = cancelled_image
    ledger = value.cycle._load()
    ledger['records'][0]['input_file_hash'] = '0' * 64
    with pytest.raises(AgentError, match='NASA_DEDUP_CORPUS_INCOMPLETE'):
        _used_narratives(value.core, ledger)


def test_legacy_quality_trace_remains_a_valid_exact_source_without_input_fallback(cancelled_image):
    value = cancelled_image
    trace = value.core.settings.root / 'data/hourly-articles/legacy-dedup.local.jsonl'
    trace.write_text(json.dumps({'context': {'source_untrusted': {'body': value.article['original']}}}), encoding='utf-8')
    ledger = {'records': [{'id': 'legacy', 'state': 'cancelled', 'url': value.article['url'],
        'original_hash': value.cycle._load()['records'][0]['original_hash'],
        'quality_trace': str(trace.relative_to(value.core.settings.root)).replace('\\', '/')} ]}
    assert _used_narratives(value.core, ledger) == [value.article['original']]


def test_ordinary_cancelled_record_without_input_or_trace_still_blocks(cancelled_image):
    value = cancelled_image
    ledger = {'records': [{'id': 'legacy', 'state': 'cancelled', 'url': value.article['url'],
        'original_hash': value.cycle._load()['records'][0]['original_hash'],
        'quality_trace': 'data/hourly-articles/missing.local.jsonl'}]}
    with pytest.raises(AgentError, match='NASA_DEDUP_CORPUS_INCOMPLETE'):
        _used_narratives(value.core, ledger)


def test_legacy_material_remains_valid_without_requiring_new_pre_model_evidence(cancelled_image):
    value = cancelled_image
    original_hash = value.cycle._load()['records'][0]['original_hash']
    with value.core.db.session.begin() as session:
        session.add(Material(source_id='legacy_source', external_id='legacy-material', story_id='legacy-story',
            title=value.article['title'], text=value.article['original'], url=value.article['url'],
            text_hash='2' * 64, published_at=value.article['published_at'], language='en'))
    ledger = {'records': [{'id': 'legacy', 'state': 'published', 'url': value.article['url'],
        'original_hash': original_hash}]}
    assert _used_narratives(value.core, ledger) == [value.article['original']]


@pytest.fixture
def retired_pre_model(illustrated, monkeypatch):  # noqa: F811 - imported pytest fixture
    import max_postiz_agent.article_images as images
    value = illustrated

    def reject(*_args):
        raise AgentError('ARTICLE_IMAGE_PROOF_INVALID', 409)

    monkeypatch.setattr(images, 'prepare_article_image', reject)
    with pytest.raises(AgentError, match='ARTICLE_IMAGE_PROOF_INVALID'):
        value.controller.prepare_once()
    ledger = value.cycle._load()
    record = ledger['records'][0]
    assert record['state'] == 'needs_attention' and not value.events and not value.posts and not value.writes
    contract = 'pre-model-source-proof-validation-retirement-v1'
    stamp = record['preparation_attempts'][0]['finished_at'] + 1
    proof = {'contract': contract, 'classification': 'KNOWN_LOCAL_SOURCE_PROOF_REJECTION_BEFORE_IMAGE_AND_MODELS',
        'forensic_sha256': 'a' * 64, 'source_capture_sha256': 'b' * 64, 'proposal_sha256': 'c' * 64,
        'before_archive': 'data/retirement-fixture.before.local.json', 'at': stamp,
        'original_error_retained': record['error'], 'source_remains_ever_used': True,
        'no_retry_or_publication_of_record': True, 'source_url': record['url'],
        **{k: record[k] for k in ('source_id', 'source_published_at', 'input_hash', 'original_hash', 'policy_hash', 'input_file_hash')}}
    record.update(state='failed', pre_model_validation_retirement=proof)
    value.cycle._save(ledger)
    with value.core.db.session.begin() as session:
        claim = session.get(EditorialPreparationClaim, record['reserve_claim']['id'])
        assert claim.state == 'blocked_unknown' and claim.error == record['error']
        claim.state, claim.active_key, claim.updated_at = 'failed_pre_model_validation', None, stamp
        for suffix, action in (('claim', 'exact_pre_model_validation_retirement_claim'),
                               ('retired', 'exact_pre_model_validation_failure_retired')):
            session.add(Audit(id=str(uuid5(NAMESPACE_URL, contract + ':' + record['id'] + ':' + suffix)),
                actor='article_reserve', action=action, entity=record['id'], at=stamp,
                details={**proof, 'record_id': record['id'], 'preparation_claim_id': claim.id,
                         'model_calls': 0, 'image_requests': 0}))
    return value


def test_typed_retired_pre_model_source_is_dedup_only_and_select_only(retired_pre_model):
    value = retired_pre_model
    before = copy.deepcopy(value.cycle._load())
    statements = []

    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(value.core.db.engine, 'before_cursor_execute', observed)
    try:
        assert _used_narratives(value.core, before) == [value.article['original']]
        from max_postiz_agent.server_science import _used_narratives as science_narratives
        assert science_narratives(value.core, before) == [value.article['original']]
    finally:
        event.remove(value.core.db.engine, 'before_cursor_execute', observed)
    assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements)
    assert value.cycle._load() == before and value.store.active_preparation() is None
    assert not value.events and not value.posts and not value.writes


@pytest.mark.parametrize('damage', [
    'no_marker', 'contract', 'classification', 'original_error', 'state', 'unknown', 'extra_attempt',
    'model_started', 'repair', 'image_claim', 'image_proof', 'image_part', 'trace', 'raw_input',
    'input_native_hash', 'original_hash', 'published_at', 'url', 'input_path', 'claim_state', 'claim_active',
    'claim_token', 'claim_time', 'binding_audit', 'missing_claim_audit', 'missing_retired_audit',
    'audit_actor', 'audit_id', 'audit_entity', 'audit_false_zero', 'audit_image_requests', 'audit_extra_key',
    'partial_material', 'slot', 'model_audit', 'forensic_hash', 'source_capture_hash', 'before_archive',
])
def test_typed_retirement_never_waives_missing_unknown_tampered_or_external_evidence(retired_pre_model, damage):
    value = retired_pre_model
    ledger = value.cycle._load()
    record = ledger['records'][0]
    proof = record['pre_model_validation_retirement']
    path = value.core.settings.root / record['input_file']
    if damage == 'no_marker':
        del record['pre_model_validation_retirement']
    elif damage in {'contract', 'classification', 'original_error', 'forensic_hash', 'source_capture_hash', 'before_archive'}:
        key = {'original_error': 'original_error_retained', 'forensic_hash': 'forensic_sha256',
               'source_capture_hash': 'source_capture_sha256'}.get(damage, damage)
        proof[key] = 'invalid'
    elif damage == 'state':
        record['state'] = 'cancelled'
    elif damage == 'unknown':
        record['model_result_unknown'] = True
    elif damage == 'extra_attempt':
        record['preparation_attempts'].append(copy.deepcopy(record['preparation_attempts'][0]))
    elif damage == 'model_started':
        record['preparation_attempts'][0]['model_started_at'] = value.stamp[0]
    elif damage == 'repair':
        record['automatic_repair_attempts'] = [{'state': 'failed'}]
    elif damage in {'image_claim', 'image_proof', 'image_part'}:
        suffix = {'image_claim': '.claim.local.json', 'image_proof': '.proof.local.json', 'image_part': '.part'}[damage]
        directory = value.core.settings.root / 'data/article-images'
        directory.mkdir(exist_ok=True)
        (directory / (record['id'] + suffix)).write_text('{}')
    elif damage == 'trace':
        (value.core.settings.root / record['quality_trace']).write_text('{}')
    elif damage == 'raw_input':
        path.write_bytes(path.read_bytes() + b' ')
    elif damage in {'input_native_hash', 'original_hash', 'published_at', 'url', 'input_path'}:
        key = {'input_native_hash': 'input_hash', 'published_at': 'source_published_at', 'input_path': 'input_file'}.get(damage, damage)
        record[key] = 'tampered'
    else:
        with value.core.db.session.begin() as session:
            claim = session.get(EditorialPreparationClaim, record['reserve_claim']['id'])
            audit = session.scalar(select(Audit).where(Audit.entity == record['id'],
                Audit.action == 'exact_pre_model_validation_failure_retired'))
            if damage == 'claim_state':
                claim.state = 'blocked_unknown'
            elif damage == 'claim_active':
                claim.active_key = 'models'
            elif damage == 'claim_token':
                claim.token = 'tampered'
            elif damage == 'claim_time':
                claim.updated_at += 1
            elif damage == 'binding_audit':
                session.delete(session.get(Audit, str(uuid5(NAMESPACE_URL, 'max-postiz:reserve-binding:' + record['id']))))
            elif damage == 'missing_claim_audit':
                session.delete(session.scalar(select(Audit).where(Audit.entity == record['id'],
                    Audit.action == 'exact_pre_model_validation_retirement_claim')))
            elif damage == 'missing_retired_audit':
                session.delete(audit)
            elif damage in {'audit_actor', 'audit_id', 'audit_entity'}:
                setattr(audit, damage.removeprefix('audit_'), 'tampered')
            elif damage in {'audit_false_zero', 'audit_image_requests', 'audit_extra_key'}:
                changes = {'audit_false_zero': {'model_calls': False}, 'audit_image_requests': {'image_requests': 1},
                           'audit_extra_key': {'extra': True}}[damage]
                audit.details = {**audit.details, **changes}
                flag_modified(audit, 'details')
            elif damage == 'partial_material':
                session.add(Material(source_id=record['source_id'], external_id=record['url'], story_id='partial',
                    title=value.article['title'], text='partial', url=record['url'], text_hash='1' * 64,
                    published_at=value.article['published_at'], language='en'))
            elif damage == 'slot':
                session.add(PublicationSlot(target_id=record['target_id'], slot_at=1, record_id=record['id'],
                    text_hash='1' * 64, evidence_hash='2' * 64, binding={}, control_revision=claim.control_revision,
                    due_at=value.stamp[0]))
            elif damage == 'model_audit':
                session.add(Audit(actor='article_quality', action='hourly_article_prepare_attempt',
                    entity=record['id'], details={'attempt': 1}))
    value.cycle._save(ledger)
    before = copy.deepcopy(value.cycle._load())
    with pytest.raises(AgentError, match='NASA_DEDUP_CORPUS_INCOMPLETE'):
        _used_narratives(value.core, before)
    assert value.cycle._load() == before and not value.events and not value.posts and not value.writes
