"""Independent illustrated MAX readback: SELECT/GET/fresh DOM only, never Send.

No publish/tick/reconcile/approval/upload/model calls. The profile lock is shared
with the sender; private reports/screenshots never enter source archives.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from readback_wheel_proof import verify_installed_wheel

sys.dont_write_bytecode = True


def load_verified_application(expected_version):
    # The wheel proof runs before any application imports, including __init__.
    global create_engine, func, or_, select, text, Session
    global HourlyArticleCycle, review_rubric_binding, validate_article_image
    global review_prompt_name, REVIEW_BOUNDS_V2_CONTRACT, review_protocol_trace_binding
    global automatic_evidence, check_approval, _reference_binding
    global ensure_channel, browser_session, browser_worker, load_env, Settings, fingerprint
    global Intent, HubService, BridgePost, Dispatch, DispatchAttempt, RemoteDraft
    global Target, TargetApproval, Variant, MaxBridge, PostizClient, PublicationSlot
    from sqlalchemy import create_engine, func, or_, select, text
    from sqlalchemy.orm import Session
    from max_postiz_agent import __version__, browser_worker
    from max_postiz_agent.article_cycle import HourlyArticleCycle, review_rubric_binding
    from max_postiz_agent.article_images import validate_article_image
    from max_postiz_agent.article_quality import REVIEW_BOUNDS_V2_CONTRACT, review_prompt_name
    from max_postiz_agent.automatic_policy import (
        automatic_evidence, check_approval, _reference_binding, review_protocol_trace_binding,
    )
    from max_postiz_agent.browser_runtime import ensure_channel, session as browser_session
    from max_postiz_agent.cli import load_env
    from max_postiz_agent.config import Settings, fingerprint
    from max_postiz_agent.db import Intent
    from max_postiz_agent.hub import HubService
    from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant
    from max_postiz_agent.max_bridge import MaxBridge
    from max_postiz_agent.postiz import PostizClient
    from max_postiz_agent.server_models import PublicationSlot
    require(__version__ == expected_version, 'CURRENT_INSTALLED_VERSION_MISMATCH')


ROOT = Path('/opt/max-postiz')
DIR = ROOT / 'data/reserve-20261026'
CONTRACT = 'article-illustrated-v2'
ACTIVE = {'pending', 'sending', 'unknown', 'scheduled', 'retry_wait', 'awaiting_manual'}
UNSET = object()
# SHA256 of each canonical full record in the original verified 113-record snapshot.
# The snapshot itself contains private data and is not distributed in the wheel.
HISTORICAL_RECORD_HASHES = (
    "00784acd3933f9717426baeba347fe26d838771aaafd2ddc69ccdf97d4072dfd",
    "00c7f3d12e05fc793012e60321331c4c5c87949a9d84c43730d4504f8fdc33d6",
    "04ee45e2c6a994ddc95f031991248181e1f5e45a3706cd2aeda4940534ba6dae",
    "05649470a44f23519aad8e88ef7e1d2bc92bbb39b869fa8bb4c1b19229b2cfdc",
    "07bca3890fe2c367a56bc5929b9c506636fcf1a646fd90ecf492edb693caf6e4",
    "0caf5521b4571ee2a76910ea5962dc45366b13e2cad487715129c6bc6763b46c",
    "0dcb512db860305ad1da4ed986b78429c81ff53354cfd1dffcd06650c81cab56",
    "0ec798c0fed48548cb53a2e48510abb286b2b6af667feb015ebb8fa4c303925d",
    "105455ff81ac8fda41f94d3a267a36c8585656efbfe0ffdc38bce549634037a7",
    "1408562f85b2feab169efeee63d2feca0cbf9ebdfd185a212f6600601b9349d2",
    "14c0d95b7d90540edabb2138294ae503a658baefb192ff22f101380803e69c5a",
    "15c0f550f61e81e5009b6390079f0bc9e4e196b9dba7a116b5f22f852e160730",
    "17baa0c3808ee02e0935c95ff4227932aceaca8dcb702d67d4a036b04427a671",
    "1899388e1ff130ef988af74cb1ee3c577c8c174ad3968464fc66c2cd69194d1d",
    "1a17b2e9797e68f9b4eeb5b608104d52624973111960ecb0cc9f790d02fb5d1f",
    "1d1ff810a66609d7567b3ea787ce7480319f18102f6d690bb0550ac93d82e509",
    "1f9c71baf4ff8d4e08c0a84b67070c6602641b20133670546c03f39dc6925617",
    "28e2db249c56e55dc93e6770a16a6155380d794be6f9a69f229c31eceb187f5a",
    "2bedbea28c23855a40bcc6488c22776a5f86730511a8e50bc1fd6823672212a5",
    "2cc853fb69aeb4edb32c3fcc38a7189fa8bdf4f206cbfa9ccf6faf606f4845ba",
    "31c77146fee8c76829b12e84006ffbf9f4868d6e7ed6c0a206b737a15f5e7369",
    "33f9f47420a2aae9518f063b8849a1974452f695a5963cf6f0f43ed6adb3b425",
    "3451c719de4f69955a9b71b8a038686da42e5c318c5bd0a2bd8a9d8137e723ad",
    "354f06e7cd46cea67d7274005c233a4f94377d989f583159bea8ee836354ae91",
    "356b9149d7812e803aa9c355305e263b97a68ce61603aa8820c35264126edc9e",
    "35cb45a6fb46fd154240b115172d08c6fb9a2e49cc9185ea88535401ff160ef9",
    "3628455ec287648ebbeaad8d33232aeecff770fe0cdccfd8845c26705899dc8d",
    "3f05eb8bdee98476838768a7ceacb1962ecb937a776e69cc988d66b6e45d1461",
    "424927a9d418f39cfc184d95d7ca373f120313dc9fc85eaee5c0040c253a4a1d",
    "45637fb98cb51bec2e370216b7e5e853d6b63dc092d2470c8c02e908d66f027e",
    "4687f5a91730eec1384e6f5a3296be914145bf45410a89a38cf64d2609976452",
    "4fabbc7ef5c321b7e7100a49feac8b5c68a52311f4a4cc30d6d6b794c0790713",
    "58f59f404f4d50f100eb1fe3e16fab374be8adad4ada5c709724f43b5855c81d",
    "6146a874408a9bd8836e4529bc6ec9bf2f67639eddf724e2010a9f8f29803983",
    "62e6bf85e0ffd0bbb5ae513b1ea95df39fe9c1f7b246e34ea8a3bce5c4d7df6d",
    "63c97b39ff6bbaf77f0ade94c2ed75a254faf9a502e232837a610e7117cf54f0",
    "63d4b1942f90717f216d1bc37663a9b1c1f3855e9f227c6fe4391237f8a018dd",
    "6465162d45b0b8b90d9e1c835b0398a20cc0ddfb2062c42258e600af9b0a77bd",
    "67d90246e6a668f17580cb4924fcdf0d09df6866e09948ac4a9d5e6558b0dbb2",
    "685089fa73c8754e097c39ccaae956166db1e057273bf5211656ba4113212446",
    "69343bc341e7b1f5d3b237044a629221cdabaebba632e049f550f67b20ccc900",
    "6937c26c6b9dc2338edc845e98b90384e9e008ba522db0b0e69c5a03f6fdb748",
    "6a77ac0bf4657b212fe083af152c369f58be03877b147b6329a7bb7027ffb266",
    "6b23c4ba2e19ee52787652d86e9f985b4853b983d3a7810a4ff8c52683ba1735",
    "6dc688f03338c99833c53cbc1f6299788e200d3bc8194ed325c3e26589da0b2e",
    "724b6ebcc57b2bedd4ae0562d351bb2f07852b10ecf916bc3a2556fb2a2a4553",
    "72a10c495fd25aabffeac423801e04b064fee4b24e3ffe64a3caf4468a9533b9",
    "77926525829dcc55b5548f8b96d611845dee72b79150d33c9ecf78f6b9dc79a5",
    "7a00370a460e912073a54d2bd4e575f89054de7a92bd75471cbf444b2b30aefe",
    "7c1799f3a8f62818dbcc9b976c5801d57cc04b5b79ead8d78f897b4008195505",
    "7d40d368dd3030bee58b2f2956c7e7ec2c14e669cd1b423eab6ae566250f115f",
    "7f1d9297e5e18ca577e55872eec68435e81e83ffdcf8648c9820ffde430e80b3",
    "7fb705d3f32745a35efa62a34d12a74f58811f46332758cea4bc092477b564b9",
    "808143f06cf6bcffdef59e2af9368617fd18556652b26cd3220cd6d97f68c42b",
    "819bef4cb366d54af92b2566c6a103be8c7b268e2d684dedd435062b33acf8d3",
    "85746609b6224f6aab4cfd85b17cddd40b653c2c4143245e1429b6b20fb2df9a",
    "863498b0692a7e486f5092fd369022be9f0b43c2a1eb49286bb3030f07a95748",
    "880f7a4fd1664071b0c6255ffb24f8d92874757356c7cacb46aa4e7e5e6d8284",
    "8b9a37aefa9e82bb478fcf8b1a210e5ac1be50c3746aa2b297be9acb8c4415fe",
    "8bee4887f83ae95411004b669be6af92372cf8b5902657415278e4d406ade21c",
    "8ebf4d750ce113421aeebb3e5edf7ae3c3326010f4228248506c5111857fffb3",
    "91d90a238e35f4b1dff9388644bd01f2f746e63eeee0dd475dfd001199d584c8",
    "94bbaa2e161e419933ceecda667f9a19fb9e975a751abeb8bebb4a6eaa9936bd",
    "96fca279ac1242882bd6c5f652c3ff0a49b8aa2dda3c361ff32cd2b4589f5111",
    "97393864320a4acd351ec03127acae81aa20b75d8e3e86fad68ec6456512eacb",
    "9739b417f6ee25e1a31599602af7c3a9dc684f31681baea867ee29b6d7f70f88",
    "976e292302b63787536fd050a4c45f431ba1bd259f8aa0f78b188e599f592aa0",
    "978cf856eecd0b0ac1bc02b3c34d055e41bdb94c0efbfa20a307923efa4b6b7f",
    "9793dc53b9899cd0b5d348b4c15d7ca50d88599a754dc553e0bc9ed38033bc0a",
    "99f24f571fb834c2f74ef14302ae59b4979f153d8ec938bf28f696b60e75b6e1",
    "9de259f7e74db240bb0f76eceb150455b13d2ce26ded317c74438b90c37ff471",
    "a4e45f9ca38d3451430e08cd6d8f411906f136c90e7298a8a4aaa235da54b810",
    "a5f6ffa728b50e5a36d63c8508d68d4bc068c7c88d637441234d47a2dede8467",
    "a5ffd6e975526dfe615a42a1ef79529ec9e7725061a9d410fe748707cbdce9c0",
    "a88a5a9ef799234c183ac7ed92bbe5661c63665e6fe341216d1387eb35de3725",
    "abb611c2ac2530841765cc07873be2dfc7077cbea00f9bbe692cd2817d808812",
    "ac5bdd09ecdf4cd3aae9f888231f9606182720b5d095f2fb73a0720406d9e030",
    "ad828739075bdd5f28e765ab6d5bac174c5aba784110c61bc0779ce9afbcd0b9",
    "adae7b267d29a279f9c6fe8f00aa1be88cb455ca2e4433a934efeaa45434ac8d",
    "ae7b07844b808ee96546d61fb6abe6255c1cfdd98766c8353f6eb3d2876524b1",
    "af3b5a7da23c16866b4324755f31a11c305ffce8c0954e0e2b8055cdd1713be4",
    "af8a2c53afa155aa5527cabebf65fcdf0a889b2ea26e69f92d72328e22fe9a12",
    "b18a1b91857f57336322bd7d141ba0539c075fa5e072f5912ca61e5ad1c82920",
    "b2d687db035b8b1a055a8388d18e582acb646a96d61397d49155c748fefc24a0",
    "b9c08a224bbe7d9235f10567f2d92558cf9494c5a5fb3582ce221a654fdf3889",
    "bd31b061a390a757364ed9f8c56bd5e32a3075fbe656afbb2ae154616d634c89",
    "be1d1243dc150a4135659815a304481138547674d3a44cd8fdcbe4a500f6de16",
    "beb11acd3d13028ad9432770264eb0f420b68efd5afa59473d41c813c2f25bf1",
    "bf3d6240a76c2946f6b0d4318f5fbb183d6f27c9b60c1390e1dfe706747aa6fb",
    "c44f7edd9948d01b614077111b49478f7771e7f511e4b9c708a0c9b1609999d6",
    "c5e12be0109b116b4b9547a683dd7c1f2312bfbd7e21ae67ee4e017e0526855e",
    "c63d2913082aed6e256e0b4f04218a8b2d1b5ef6069a75f1f4ad340d79421d24",
    "c8121de977a6d8ad758026a247f957ddf6143b9b3a272068e416148b6032be6a",
    "cd32c34bdc1d8e15d34ef2bb84e6e7445e9bc57db8ad6210ffd0d80244a6075c",
    "cf2f9f1468bd064fe63d56dfceb08eae847d01aeed1a823ed08b273edc9cbeb5",
    "d096bee38c29301b8bf7c5ec6d999ca74bdd23460eaf478d17ae0b76ef69a469",
    "d1189744fb4a3f06f2d231baa3c157fe3c0bb8b85c8a02676f2d74eb2ed41671",
    "d2570e278ef9d6bf08efcc497aff2574d37d9c5338dc25769ad1eb75d5e27fc7",
    "d292382d3fc273236e36e776247ffb56deff60dcca454d8b0b60987894f44182",
    "d3815aa299c5ee27a6b080ef518700c86784cf135264e629211e2826338a1ab1",
    "d53ad2f86036c85efd4ce7ff89450a52a10c9f6eb5eec91b37961d2c7c6b73ac",
    "d53dbffa2e50b417dd81a921f33f6715b1de2d9e621f4d402f1e6497e25bdab5",
    "d9879a4ee21f4017b43de967a7f1e54dc7e0ba5f7f410e5a459f6dc9cb106ddf",
    "decd6d3cfd5400e9bc0101e98d5831329f4fc00b0d3c4dd674f471b7a1d08971",
    "df59ffdc971ccdceeea4b2184ade7b3f7f9812aa9a9269a8df3049c99486f67e",
    "e08ae327704b145650e42e496249ae7be1a5070f1ed4531e3cf8c66090f1cb9e",
    "e1728a16d2a83bcac329275dbfec442603807634fe7d4fa2ef2e4c96a8180632",
    "e2ab0dfd5e9e24f4165bb70e4f5aa8f894dd37fa4dd299a9c2880b31527a3294",
    "e7c24b1ed5755f3425c875fe4d646721e32cf6385ccb46fe58933edf997b9bd6",
    "e907cb60182f8a176a8fa6c14727cb34396e4183f526057775aace258e74969e",
    "e9f9d67f1b24e92f77be65dea8e572dfcaa60347152fc0bc3dbcb4641a54c970",
    "ec72a90bdc73d9a5cdedc3fea9c8b9c73834dc5027520105e0c257d7c6cb91ab",
    "fbf8e78c70eefc8e1717e9e1b7ae1fe884e80b03175a793c878878a031d99b62",
)
HISTORICAL_RECORD_HASH_LIST_SHA256 = "0c345e1ee2d505ace7eefb7531c7d4cfcc39d88ad2bbce138bc8dbc045ae1074"


def require(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_private(path, maximum=20 * 1024 * 1024):
    require(path.resolve() == path and path.is_relative_to(ROOT / 'data') and not path.is_symlink()
        and path.is_file() and 0 < path.stat().st_size <= maximum, 'PRIVATE_PROOF_PATH_INVALID')
    return path.read_bytes()


def private(path, value):
    require(path.parent == DIR and path.resolve() == path and not path.is_symlink(), 'PRIVATE_OUTPUT_INVALID')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)



def verify_prompt_binding(prompt, trace, evidence, *, record_id, bounds_v2, validate_trace):
    """Use the signed prompt, or its exact instruction in a validated v2 trace."""
    expected = evidence.get('review_prompt_hash')
    require(isinstance(expected, str) and re.fullmatch(r'[a-f0-9]{64}', expected),
        'REVIEW_PROMPT_HASH_INVALID')
    try:
        current_hash = sha(prompt.read_text(encoding='utf-8').encode())
    except (OSError, UnicodeError):
        current_hash = None
    if current_hash == expected:
        return 'current_prompt'
    require(evidence.get('review_protocol_contract') == bounds_v2,
        'REVIEW_PROMPT_CHANGED')
    # The validator checks the trace hash, accepted evidence, complete context,
    # exact instruction SHA, original and translation, review response and parse.
    validate_trace(trace, evidence)
    raw = read_private(trace, 2 * 1024 * 1024)
    require(sha(raw) == evidence.get('trace_hash')
        and json.loads(raw.decode('utf-8').splitlines()[0]).get('record_id') == record_id,
        'REVIEW_TRACE_RECORD_MISMATCH')
    return 'verified_v2_trace'


def readback_ensure_channel(page, settings, destination, *, navigate=True):
    """Verify channel identity without asking for permission to publish."""
    ensure_channel(page, settings, destination, navigate=navigate, require_publish=False)


def new_report(record_id):
    return {'record_id': record_id, 'started_at': time.time(), 'guards': [], 'max': [],
        'future_get_checks': [], 'external_mutations_performed': 0, 'db_writes': 0,
        'model_requests': 0, 'upload_or_send_calls': 0,
        'delivery_sender_attribution': {'status': 'unknown',
            'reason': 'SENDER_LIFECYCLE_EVIDENCE_UNAVAILABLE'}}


def verify_historical_records(records):
    """Preserve each original complete record while allowing unrelated new records."""
    def canonical(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode('utf-8')

    expected = list(HISTORICAL_RECORD_HASHES)
    require(len(expected) == 113 and len(set(expected)) == 113
        and all(re.fullmatch(r'[a-f0-9]{64}', value) for value in expected)
        and sha(canonical(sorted(expected))) == HISTORICAL_RECORD_HASH_LIST_SHA256,
        'HISTORICAL_HASH_BASELINE_INVALID')
    require(isinstance(records, list) and all(isinstance(row, dict)
        and isinstance(row.get('id'), str) and row['id'] for row in records),
        'LEDGER_RECORD_IDS_INVALID')
    ids = [row['id'] for row in records]
    require(len(ids) == len(set(ids)), 'LEDGER_RECORD_IDS_NOT_UNIQUE')
    observed = {sha(canonical(row)) for row in records}
    require(set(expected) <= observed, 'OLD_113_RECORDS_CHANGED')


def exact_practical_rubric(record):
    value = record.get("review_rubric")
    require(isinstance(value, str) and value in {"hourly-practical-v1", "hourly-practical-v2"},
            "EXACT_SAVED_PRACTICAL_RUBRIC_REQUIRED")
    return value


def published_observation(db, dispatch, record_id, attempts):
    """One Send; this exact audited first dispatch may have a second observation."""
    from uuid import NAMESPACE_URL, uuid5
    from max_postiz_agent.db import Audit
    require(dispatch.attempts == 1, "ONE_PHYSICAL_SEND_REQUIRED")
    exact_reconciliation = (record_id == "81e4db37-a169-42e0-825e-0bc9252c2a24"
        or dispatch.id == "52125814-e3ff-486b-890a-a9d636389c03")
    if not exact_reconciliation:
        require(len(attempts) == 1 and attempts[0].result == "published" and attempts[0].error is None,
                "ONE_PUBLISHED_ATTEMPT_REQUIRED")
        return attempts[0]
    require(record_id == "81e4db37-a169-42e0-825e-0bc9252c2a24"
        and dispatch.id == "52125814-e3ff-486b-890a-a9d636389c03" and len(attempts) == 2,
        "EXACT_AUDITED_OBSERVATION_EXCEPTION_REQUIRED")
    unknown = [row for row in attempts if row.result == "unknown"]
    published = [row for row in attempts if row.result == "published" and row.error is None]
    require(len(unknown) == len(published) == 1, "ONE_UNKNOWN_AND_ONE_PUBLISHED_OBSERVATION_REQUIRED")
    contract = "existing-max-image-observation-no-send-v1"
    audit = db.get(Audit, str(uuid5(NAMESPACE_URL, contract + ":" + dispatch.id)))
    require(audit is not None and audit.actor == "article_reserve"
        and audit.action == "existing_max_message_reconciled" and audit.entity == dispatch.id,
        "NATIVE_NO_SEND_OBSERVATION_AUDIT_REQUIRED")
    details = audit.details
    keys = ("id", "dispatch_id", "result", "error", "created_at")
    require(details.get("contract") == contract and details.get("physical_send_count") == 1
        and details.get("new_publish_calls") == 0
        and details.get("original_unknown_attempt") == {key: getattr(unknown[0], key) for key in keys}
        and details.get("published_observation") == {key: getattr(published[0], key) for key in keys}
        and details.get("proposal", {}).get("record_id") == record_id
        and details.get("proposal", {}).get("dispatch_id") == dispatch.id
        and details.get("proposal", {}).get("snapshot_hash") == dispatch.snapshot_hash
        and details.get("journal_before_sha256") == "c07364331f263cc530571caace17a5e0ac041df0201d256f99a798244da0841b",
        "OBSERVATION_MUST_PRESERVE_ORIGINAL_UNKNOWN_PROOF")
    path = ROOT / "data/browser-publications" / (dispatch.id + ".json")
    require(path.resolve() == path and path.is_file() and sha(path.read_bytes()) == details.get("journal_after_sha256"),
        "CONFIRMED_JOURNAL_OBSERVATION_CHANGED")
    saved = json.loads(path.read_bytes())
    require(saved.get("state") == "confirmed" and saved.get("request_id") == dispatch.id
        and saved.get("text") == dispatch.snapshot["body"] and saved.get("delivery", {}).get("state") == "confirmed"
        and saved.get("delivery", {}).get("external_id") == "browser-" + dispatch.id
        and saved.get("delivery", {}).get("image_proof", {}).get("uploaded_sha256") == dispatch.snapshot["media"][0]["sha256"],
        "ONE_EXISTING_MAX_IMAGE_MESSAGE_REQUIRED")
    return published[0]


def exact_publication(db, ledger, record_id, settings, *, pending=False):
    matches = [row for row in ledger['records'] if row.get('id') == record_id]
    require(len(matches) == 1, 'ONE_EXACT_RECORD_REQUIRED')
    record = copy.deepcopy(matches[0])
    rubric = exact_practical_rubric(record)
    require(record.get('presentation_contract') == CONTRACT and record.get('reserve_contract') == 'article-reserve-v1',
        'EXACT_ILLUSTRATED_RESERVE_REQUIRED')
    require(record['state'] == ('pending' if pending else 'published')
        and (record.get('delivery_verified') in (None, False) if pending else record.get('delivery_verified') is True),
        'RECORD_DELIVERY_STATE_INVALID')
    dispatch = db.get(Dispatch, record.get('dispatch_id'))
    require(dispatch is not None and dispatch.state == ('pending' if pending else 'published')
        and dispatch.owner is None and dispatch.lease_until == 0 and dispatch.error is None
        and dispatch.verified is (not pending), 'EXACT_DISPATCH_STATE_REQUIRED')
    variant = db.get(Variant, dispatch.variant_id)
    target = db.get(Target, variant.target_id) if variant else None
    approval = db.get(TargetApproval, dispatch.approval_id)
    require(variant is not None and target is not None and target.id == record['target_id'] == 'max_test'
        and variant.id == record.get('variant_id') and target.enabled and target.platform == 'max'
        and target.route == 'max_browser' and variant.config_hash == settings.hash
        and variant.binding_hash == target.binding_hash, 'EXACT_TARGET_AND_VARIANT_REQUIRED')
    require(approval is not None and not approval.revoked and approval.actor == 'hourly_article_policy'
        and approval.variant_id == variant.id and approval.revision == dispatch.revision == variant.revision
        and approval.scheduled_at == dispatch.due_at == record.get('due_at'), 'EXACT_POLICY_APPROVAL_REQUIRED')
    core = SimpleNamespace(settings=settings, snapshot=lambda v, t: HubService.snapshot(None, v, t))
    snapshot = core.snapshot(variant, target)
    require(snapshot == dispatch.snapshot and record.get('prepared_hash') == fingerprint(snapshot)
        and dispatch.snapshot_hash == approval.snapshot_hash == fingerprint({'content': snapshot, 'due_at': dispatch.due_at}),
        'SIGNED_SNAPSHOT_CHANGED')
    require(snapshot['metadata'].get('origin') == 'hourly_article_test'
        and snapshot['metadata'].get('approval_mode') == 'automatic'
        and snapshot['metadata'].get('record_id') == record_id
        and snapshot['metadata'].get('presentation_contract') == CONTRACT
        and snapshot['body'].startswith(snapshot['title'] + '\n'), 'SIGNED_PRESENTATION_REQUIRED')
    require('Архивный научный материал.' not in snapshot['body'] and 'Машинный перевод на русский и автоматическая редактура.' not in snapshot['body'], 'LEGACY_FOOTER_REMAINS')
    media = validate_article_image(core, record, variant)
    require(len(media) == 1 and media == snapshot['media'], 'ONE_EXACT_IMAGE_REQUIRED')
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core, cycle.reserve_mode, cycle.directory = core, True, ROOT / 'data/hourly-articles'
    policy = cycle._policy_for_record(record)
    require(policy.get('approval_mode') == 'automatic' and policy.get('presentation_contract') == CONTRACT
        and policy.get('target_id') == target.id and fingerprint(policy) == record.get('policy_hash')
        == variant.metadata_json.get('policy_hash'), 'EXACT_ILLUSTRATED_POLICY_REQUIRED')
    require(review_rubric_binding(core, db, record, variant) == rubric
        and record.get('review_rubric') == variant.metadata_json.get('review_rubric') == policy.get('review_rubric') == rubric,
        'EXACT_REVIEW_RUBRIC_REQUIRED')
    evidence = automatic_evidence(db, variant)
    require(evidence is not None, 'PASSED_MACHINE_EVIDENCE_REQUIRED')
    _reference_binding(db, variant, record)
    check_approval(core, db, variant, target, dispatch, approval, evidence, policy)
    trace = ROOT / record['quality_trace']
    require(trace == cycle.directory / ('quality-' + record_id + '.local.jsonl')
        and sha(read_private(trace, 2 * 1024 * 1024)) == variant.metadata_json['machine_review_evidence']['trace_hash'],
        'EXACT_TRACE_REQUIRED')
    prompt = ROOT / 'prompts' / review_prompt_name(rubric, review_protocol_contract=record.get("review_protocol_contract"))
    verify_prompt_binding(prompt, trace, variant.metadata_json['machine_review_evidence'],
        record_id=record_id, bounds_v2=REVIEW_BOUNDS_V2_CONTRACT,
        validate_trace=review_protocol_trace_binding)
    require(db.scalar(select(func.count()).select_from(Dispatch).where(Dispatch.variant_id == variant.id)) == 1
        and db.scalar(select(func.count()).select_from(TargetApproval).where(TargetApproval.variant_id == variant.id)) == 1,
        'DUPLICATE_APPROVAL_OR_DISPATCH')
    attempts = list(db.scalars(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id)))
    if pending:
        require(dispatch.attempts == 0 and not attempts and dispatch.external_id is None
            and dispatch.permalink is None and record.get('published_at') is None,
            'FUTURE_MUST_HAVE_ZERO_ATTEMPTS')
        require(time.time() + 300 <= dispatch.due_at <= time.time() + 3700, 'FUTURE_300_SECOND_MARGIN_REQUIRED')
    else:
        published_attempt = published_observation(db, dispatch, record_id, attempts)
        require(record.get('published_at') == published_attempt.created_at, 'ONE_ACTUAL_PUBLISHED_ATTEMPT_REQUIRED')
    slots = list(db.scalars(select(PublicationSlot).where(PublicationSlot.record_id == record_id)))
    require(len(slots) == 1, 'ONE_EXACT_SLOT_REQUIRED')
    slot = slots[0]
    from max_postiz_agent.article_reserve import _IDENTITY
    require(slot.id == record.get('publication_slot_id') and slot.target_id == target.id
        and slot.dispatch_id == dispatch.id and slot.due_at == dispatch.due_at
        and slot.state == ('pending' if pending else 'published') and slot.error is None
        and slot.text_hash == record.get('reserve_prepared_hash') and slot.evidence_hash == evidence['evidence_hash']
        and slot.binding == {key: record.get(key) for key in _IDENTITY}, 'EXACT_SLOT_BINDING_REQUIRED')
    if pending:
        revision = db.execute(text("SELECT revision FROM editorial_control WHERE id='main'")).scalar_one()
        require(slot.active_target == target.id and slot.control_revision == revision
            and slot.delivered_at is None, 'FUTURE_CURRENT_FENCE_REQUIRED')
        previous = list(db.scalars(select(DispatchAttempt.created_at).join(Dispatch).join(Variant).where(
            Variant.target_id == target.id, DispatchAttempt.result == 'published')))
        require(not previous or dispatch.due_at >= max(previous) + max(3600, policy['min_gap_seconds']) + 1,
            'FUTURE_ACTUAL_GAP_INVALID')
    else:
        require(slot.active_target is None and slot.delivered_at == published_attempt.created_at,
            'ACTUAL_DELIVERY_SLOT_REQUIRED')
    link = db.get(BridgePost, record.get('postiz_id'))
    require(link is not None and link.variant_id == variant.id and link.target_id == target.id
        and link.active and link.error is None and link.mutation_owner is None and link.mutation_until == 0
        and abs(link.due_at - dispatch.due_at) < .001, 'EXACT_BRIDGE_LINK_REQUIRED')
    if pending:
        require(link.remote_state == 'QUEUE' and link.remote_hash == snapshot['metadata'].get('postiz_hash'),
            'FUTURE_LOCAL_BRIDGE_CHANGED')
    return {'record': record, 'dispatch_id': dispatch.id, 'snapshot': copy.deepcopy(snapshot),
        'snapshot_hash': dispatch.snapshot_hash, 'due_at': dispatch.due_at, 'approval_id': approval.id,
        'evidence_hash': evidence['evidence_hash'],
        'published_observation_attempt_id': None if pending else published_attempt.id,
        'slot_id': slot.id, 'postiz_id': link.post_id,
        'integration_id': link.integration_id, 'group_id': link.group_id, 'target_paused': target.paused}


def remote_exact(client, facts, state):
    remote = client.get_post(facts['postiz_id'])
    body, media, due, observed = MaxBridge.normalize(remote)
    require(remote.get('id') == facts['postiz_id'] and remote.get('state') == state
        and body == facts['snapshot']['body'] and len(media) == 1
        and abs(due - facts['due_at']) < .001
        and remote.get('group') == facts['group_id'] and remote.get('integration', {}).get('id') == facts['integration_id'],
        'REMOTE_BODY_IMAGE_DATE_OR_BINDING_CHANGED')
    signed = copy.deepcopy(remote)
    signed['state'] = 'QUEUE'
    require(MaxBridge.normalize(signed)[3] == facts['snapshot']['metadata'].get('postiz_hash'),
        'REMOTE_CHANGE_NOT_STATE_ONLY')
    return remote, {'state': state, 'postiz_id': facts['postiz_id'], 'body_sha256': sha(body.encode()),
        'image': media[0], 'remote_hash': observed, 'due_at': due}


def stored_image_get(client, remote, expected):
    _, media, _, _ = MaxBridge.normalize(remote)
    item = media[0]
    require(isinstance(item, dict) and isinstance(item.get('path'), str), 'REMOTE_IMAGE_PATH_REQUIRED')
    parsed, base = urlsplit(item['path']), urlsplit(client.base)
    allowed = {(base.scheme, base.netloc)}
    if client.public_origin:
        public = urlsplit(client.public_origin)
        allowed.add((public.scheme, public.netloc))
    require(not parsed.query and not parsed.fragment and (not parsed.scheme and not parsed.netloc
        or (parsed.scheme, parsed.netloc) in allowed)
        and unquote(parsed.path).startswith('/uploads/') and '..' not in Path(unquote(parsed.path)).parts,
        'REMOTE_IMAGE_ORIGIN_OR_PATH_INVALID')
    digest, size = hashlib.sha256(), 0
    url = base.scheme + '://' + base.netloc + parsed.path
    with client.client.stream('GET', url) as response:
        require(response.status_code == 200 and response.headers.get('content-type', '').split(';')[0].strip() == expected['mime'],
            'REMOTE_IMAGE_GET_FAILED')
        for chunk in response.iter_bytes():
            size += len(chunk)
            require(size <= 10 * 1024 * 1024, 'REMOTE_IMAGE_TOO_LARGE')
            digest.update(chunk)
    require(size > 0 and digest.hexdigest() == expected['sha256'], 'REMOTE_IMAGE_BYTES_CHANGED')
    return {'bytes': size, 'sha256': digest.hexdigest(), 'in_memory_only': True}


def browser_ledger(facts, settings):
    path = ROOT / 'data/browser-publications' / (facts['dispatch_id'] + '.json')
    raw = read_private(path, 2 * 1024 * 1024)
    saved = json.loads(raw)
    record, snapshot = facts['record'], facts['snapshot']
    media = snapshot['media'][0]
    expected = {'action': 'publish', 'config_hash': settings.hash, 'request_id': facts['dispatch_id'],
        'chat_id': snapshot['destination'], 'text': snapshot['body'],
        'attachment': {'type': 'browser-image', 'payload': {'path': media['path'], 'sha256': media['sha256']}},
        'presentation_contract': CONTRACT, 'illustration_proof_hash': record['illustration_proof_hash'],
        'illustration_dimensions': [record['illustration']['width'], record['illustration']['height']],
        'title_text': snapshot['title'], 'title_format': 'bold'}
    if saved.get('contract') == 'browser-send-journal-v1':
        expected['dispatch_snapshot_hash'] = fingerprint(snapshot)
        base = {key: expected[key] for key in ('action', 'config_hash', 'request_id', 'chat_id', 'text', 'attachment')}
        require(saved.get('send_started') is True and saved.get('phase') == 'receipt_reload'
            and saved.get('snapshot_hash') == fingerprint(snapshot)
            and saved.get('request_payload') == expected
            and saved.get('base_payload') == base
            and saved.get('base_payload_hash') == fingerprint(base)
            and saved.get('body_sha256') == sha(snapshot['body'].encode())
            and saved.get('request_attachment') == expected['attachment'],
            'NEW_SEND_JOURNAL_EXACT_REQUEST_REQUIRED')
    else:
        require(saved.get('contract') is None and 'send_started' not in saved
            and 'request_payload' not in saved, 'UNKNOWN_JOURNAL_CONTRACT')
    require(saved.get('state') == 'confirmed' and saved.get('request_id') == facts['dispatch_id']
        and saved.get('chat_id') == snapshot['destination'] and saved.get('text') == snapshot['body']
        and saved.get('content_hash') == fingerprint(expected) and saved.get('before_count') == 0
        and saved.get('attachment') is True and saved.get('attachment_type') == 'browser-image',
        'CONFIRMED_BROWSER_RECEIPT_CHANGED')
    require(all(saved.get(key) == expected[key] for key in browser_worker._PRESENTATION_FIELDS)
        and saved.get('illustration_image_sha256') == media['sha256'], 'SAVED_PRESENTATION_PROOF_CHANGED')
    delivery = saved.get('delivery', {})
    proof = delivery.get('image_proof', {})
    require(delivery.get('state') == 'confirmed' and delivery.get('external_id') == 'browser-' + facts['dispatch_id']
        and proof.get('contract') == CONTRACT and proof.get('image_before_caption') is True
        and proof.get('title_format') == 'bold' and proof.get('uploaded_sha256') == media['sha256']
        and proof.get('illustration_proof_hash') == record['illustration_proof_hash']
        and proof.get('title_proof', {}).get('title_bold') is True
        and proof.get('title_proof', {}).get('body_unbold') is True
        and proof.get('delivered_pixels_sha256_verified') is False, 'SAVED_IMAGE_OR_BOLD_PROOF_REQUIRED')
    require(saved.get('composer_title_proof', {}).get('title_bold') is True
        and saved.get('composer_title_proof', {}).get('body_unbold') is True, 'PRECLICK_BOLD_PROOF_REQUIRED')
    upload = path.with_suffix(Path(media['path']).suffix.lower())
    require(sha(read_private(upload, 10 * 1024 * 1024)) == media['sha256'], 'IMMUTABLE_UPLOADED_COPY_CHANGED')
    return path, raw, saved


def print_summary(report, output, expected_future):
    print(json.dumps({'state': report['state'], 'record_id': report['record_id'],
        'report': str(output.relative_to(ROOT)), 'report_sha256': sha(output.read_bytes()),
        'max_exact_counts': [entry['exact_message_count'] for entry in report['max']],
        'max_literal_equal': [entry['literal_text_equal'] for entry in report['max']],
        'image_above_caption': [entry['image_proof']['image_before_caption'] for entry in report['max']],
        'title_bold': [entry['image_proof']['title_proof']['title_bold'] for entry in report['max']],
        'body_unbold': [entry['image_proof']['title_proof']['body_unbold'] for entry in report['max']],
        'known_future_dispatch_id': expected_future.get('dispatch_id') if isinstance(expected_future, dict) else None,
        'browser_ledger_unchanged': report.get('browser_ledger_sha256_before') == report.get('browser_ledger_sha256_after')
            if 'browser_ledger_sha256_after' in report else False,
        'external_mutations_performed': 0, 'error': report.get('error'),
        'delivery_sender_attribution': report['delivery_sender_attribution']}, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('record_id')
    parser.add_argument('--allow-known-future', action='store_true')
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--wheel-sha256', required=True)
    args = parser.parse_args()
    require(re.fullmatch(r'[a-f0-9-]{36}', args.record_id) is not None, 'EXACT_RECORD_ID_REQUIRED')
    require(Path.cwd().resolve() == ROOT and DIR.is_dir(), 'EXISTING_VM_WORKSPACE_REQUIRED')
    os.umask(0o077)
    stamp = str(time.time_ns())
    output = DIR / ('readback-practical-v2-' + args.record_id + '-' + stamp + '.local.json')
    report = new_report(args.record_id)
    engine = client = None
    expected_published = expected_future = UNSET
    try:
        report['code_proof'] = verify_installed_wheel(args.wheel, args.wheel_sha256)
        report['code_proof']['applies_to'] = 'readback_runtime'
        load_verified_application(report['code_proof']['version'])
        load_env(ROOT)
        settings = Settings.load(ROOT)
        database_url = os.environ[settings.app['database_url_env']]
        require(database_url.startswith('postgresql'), 'POSTGRESQL_REQUIRED')
        engine = create_engine(database_url, pool_pre_ping=True)
        client = PostizClient(json.loads((ROOT / 'config/publishing.json').read_text(encoding='utf-8-sig'))['postiz'])

        def guard(label):
            nonlocal expected_published, expected_future
            with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
                with connection.begin():
                    connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                    connection.exec_driver_sql("SET LOCAL statement_timeout='10000ms'")
                    require(connection.exec_driver_sql('SHOW transaction_read_only').scalar_one() == 'on', 'READ_ONLY_REQUIRED')
                    with Session(bind=connection, autoflush=False, expire_on_commit=False) as db:
                        ledger = connection.execute(text("SELECT payload FROM editorial_article_ledger WHERE id='hourly'")).scalar_one()
                        verify_historical_records(ledger['records'])
                        report['all_113_historical_records_preserved'] = True
                        published = exact_publication(db, ledger, args.record_id, settings)
                        active = list(db.scalars(select(Dispatch).where(or_(Dispatch.state.in_(ACTIVE),
                            Dispatch.owner.is_not(None), Dispatch.lease_until != 0, Dispatch.lease_until.is_(None)))))
                        future = None
                        if active:
                            require(args.allow_known_future and len(active) == 1 and active[0].state == 'pending', 'EXTERNAL_OPERATIONS_NOT_CLEAR')
                            matches = [row for row in ledger['records'] if row.get('dispatch_id') == active[0].id]
                            require(len(matches) == 1 and matches[0]['id'] != args.record_id, 'ONE_DISTINCT_FUTURE_RECORD_REQUIRED')
                            future = exact_publication(db, ledger, matches[0]['id'], settings, pending=True)
                        active_slots = list(db.scalars(select(PublicationSlot).where(PublicationSlot.active_target.is_not(None))))
                        require((not active_slots if future is None else len(active_slots) == 1
                            and active_slots[0].id == future['slot_id']), 'OTHER_ACTIVE_OR_STAGING_SLOT')
                        require(db.scalar(select(func.count()).select_from(BridgePost).where(or_(
                            BridgePost.mutation_owner.is_not(None), BridgePost.mutation_until != 0,
                            BridgePost.mutation_until.is_(None)))) == 0, 'MUTATION_LEASE_PRESENT')
                        require(db.scalar(select(func.count()).select_from(Intent).where(Intent.state.in_(ACTIVE))) == 0,
                            'ACTIVE_LEGACY_INTENT')
                        require(db.scalar(select(func.count()).select_from(RemoteDraft).where(RemoteDraft.state.in_({'creating', 'unknown'}))) == 0,
                            'REMOTE_DRAFT_OUTCOME_UNKNOWN')
                        targets = list(db.scalars(select(Target)))
                        require(targets and all(target.paused or future is not None and target.id == 'max_test' for target in targets),
                            'OTHER_UNPAUSED_TARGET')
                        if expected_published is UNSET:
                            expected_published, expected_future = copy.deepcopy(published), copy.deepcopy(future)
                        else:
                            require(published == expected_published and future == expected_future, 'EXACT_FACTS_CHANGED_DURING_READBACK')
                        report['guards'].append({'label': label, 'at': time.time(), 'read_only': True,
                            'future_dispatch_id': future['dispatch_id'] if future else None,
                            'ledger_records': len(ledger['records'])})
                        return published, future

        facts, future = guard('before_postiz_get')
        remote, report['postiz'] = remote_exact(client, facts, 'PUBLISHED')
        report['remote_image'] = stored_image_get(client, remote, facts['snapshot']['media'][0])
        ledger_path, ledger_before, publication = browser_ledger(facts, settings)
        report['browser_ledger_sha256_before'] = sha(ledger_before)
        report['published'] = facts

        def future_get(label):
            if expected_future is not None:
                _, proof = remote_exact(client, expected_future, 'QUEUE')
                report['future_get_checks'].append({'label': label, **proof})

        future_get('before_profile_lock')
        guard('before_profile_lock')
        with browser_session(settings) as page:
            guard('profile_lock_held')
            readback_ensure_channel(page, settings, facts['snapshot']['destination'])
            for number in (1, 2):
                if number == 2:
                    guard('before_second_reload')
                    page.reload(wait_until='domcontentloaded')
                    readback_ensure_channel(page, settings, facts['snapshot']['destination'], navigate=False)
                deadline = time.monotonic() + 20
                observed = None
                while time.monotonic() < deadline:
                    matches = browser_worker._matched_posts(page, facts['snapshot']['body'], illustrated=True)
                    if matches.count() == 1:
                        observed = browser_worker.receipt(page, publication)
                        if observed is not None:
                            break
                    page.wait_for_timeout(200)
                require(matches.count() == 1 and observed is not None, 'ONE_EXACT_IMAGE_BOLD_MAX_MESSAGE_REQUIRED')
                captions = browser_worker._caption_nodes(matches.last, facts['snapshot']['body'])
                literal = next((node.inner_text() for node in captions if node.is_visible()
                    and node.inner_text() == facts['snapshot']['body']), None)
                require(literal is not None, 'MAX_CAPTION_LITERAL_MISMATCH')
                proof = observed['image_proof']
                require(proof['image_before_caption'] is True and proof['title_proof']['title_bold'] is True
                    and proof['title_proof']['body_unbold'] is True, 'MAX_IMAGE_AND_BOLD_PROOF_REQUIRED')
                screenshot = DIR / ('readback-practical-v2-' + args.record_id + '-' + stamp + '-pass' + str(number) + '.local.png')
                matches.last.screenshot(path=str(screenshot))
                screenshot.chmod(0o600)
                report['max'].append({'pass': number, 'exact_message_count': 1, 'literal_text_equal': True,
                    'body_sha256': sha(literal.encode()), 'title_text': facts['snapshot']['title'], 'image_proof': proof,
                    'private_screenshot': str(screenshot.relative_to(ROOT)), 'screenshot_sha256': sha(screenshot.read_bytes()),
                    'body': literal, 'caption_html': captions[0].evaluate('(element)=>element.outerHTML')})
        guard('after_browser_closed')
        future_get('after_browser_closed')
        guard('after_future_get')
        report['browser_ledger_sha256_after'] = sha(read_private(ledger_path, 2 * 1024 * 1024))
        require(report['browser_ledger_sha256_before'] == report['browser_ledger_sha256_after'], 'BROWSER_LEDGER_CHANGED')
        report['state'] = 'verified'
    except Exception as exc:
        safe = getattr(exc, 'code', None)
        if safe is None and isinstance(exc, ValueError) and len(exc.args) == 1:
            code = exc.args[0]
            safe = code if isinstance(code, str) and re.fullmatch(r'[A-Z0-9_]+', code) else None
        report.update(state='check_failed', error={'exception': type(exc).__name__, 'code': safe})
    finally:
        if client is not None:
            client.close()
        if engine is not None:
            engine.dispose()
        report['finished_at'] = time.time()
        private(output, report)
    print_summary(report, output, expected_future)
    raise SystemExit(0 if report['state'] == 'verified' else 1)


if __name__ == '__main__':
    main()
