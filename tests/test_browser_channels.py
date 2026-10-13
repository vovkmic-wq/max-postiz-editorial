import json
from contextlib import contextmanager

import pytest

from max_postiz_agent import browser_channels
from max_postiz_agent.browser_channels import create_channel
from max_postiz_agent.config import AgentError, Settings
from test_browser_publishing import browser_config


# Операция creation_browser: данного модуля.
@pytest.fixture
def creation_browser(project, monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    settings = browser_config(project)
    html = '''<meta charset="utf-8"><div id="start"><button>Настройки</button>
    <button onclick="document.querySelector('#menu').hidden=false">Начать общение</button>
    <div role="menu" id="menu" hidden><button onclick="openCreation()">Создать канал</button></div></div>
    <div id="form" hidden><input placeholder="Название"><textarea placeholder="Описание"></textarea>
    <button onclick="created()">Создать канал</button></div>
    <div id="created" hidden><p>Приватный канал создан</p>
    <button onclick="history.pushState({},'', '/-456');channel()">Продолжить</button></div>
    <div id="channel" hidden><h2 id="main-header-title"></h2>
    <div contenteditable role="textbox" aria-placeholder="Пост" style="min-height:24px"></div></div>
    <script>
    function show(id){for(const el of document.body.children)if(el.tagName==='DIV')el.hidden=el.id!==id;}
    function channel(){show('channel');document.querySelector('h2').textContent='Окно чата с '+localStorage.name;}
    function openCreation(){show('form');}
    function created(){localStorage.name=document.querySelector('input').value;
      localStorage.count=Number(localStorage.count||0)+1;
      show('created');}
    window.onload=()=>{if(location.pathname==='/-456')channel();};
    </script>'''
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel='chrome', headless=True, chromium_sandbox=True)
        page = browser.new_page(viewport={'width':1280,'height':900})
        page.set_default_timeout(3000)
        page.route('https://web.max.ru/**', lambda route: route.fulfill(body=html, content_type='text/html'))
        # Операция opened: данного модуля.
        @contextmanager
        def opened(*args):
            yield page
        monkeypatch.setattr(browser_channels, 'session', opened)
        yield settings, page
        browser.close()


# Проверяем сценарий: private channel creation reload and repeat do not create twice.
def test_private_channel_creation_reload_and_repeat_do_not_create_twice(creation_browser):
    settings, page = creation_browser
    result = create_channel(settings, 'Новый канал', bind_channel=True)
    assert result['state'] == 'confirmed' and result['chat_id'] == '-456'
    current = Settings.load(settings.root)
    assert current.app['max']['browser']['channels']['-456'] == 'Новый канал'
    assert current.hash != settings.hash
    assert create_channel(current, 'Новый канал', bind_channel=True) == result
    assert page.evaluate('localStorage.count') == '1'
    assert settings.topic == current.topic


# Проверяем сценарий: lost result never recreates and can recover exact channel.
def test_lost_result_never_recreates_and_can_recover_exact_channel(creation_browser, monkeypatch):
    settings, page = creation_browser
    verify = browser_channels.verify_created
    # Операция lost: данного модуля.
    def lost(*args):
        raise RuntimeError('connection lost after creation')
    monkeypatch.setattr(browser_channels, 'verify_created', lost)
    assert create_channel(settings, 'Тест')['state'] == 'unknown'
    # No address was confirmed before the simulated interruption.
    receipt = next((settings.root / 'data/browser-channels').glob('*.json'))
    saved = json.loads(receipt.read_text(encoding='utf-8'))
    saved.pop('chat_id', None)
    receipt.write_text(json.dumps(saved), encoding='utf-8')
    assert create_channel(settings, 'Тест')['state'] == 'unknown'
    assert page.evaluate('localStorage.count') == '1'
    monkeypatch.setattr(browser_channels, 'verify_created', verify)
    assert create_channel(settings, 'Тест', recover_chat_id='-456')['state'] == 'confirmed'
    assert page.evaluate('localStorage.count') == '1'


# Проверяем сценарий: recovery requires saved creation intent.
def test_recovery_requires_saved_creation_intent(creation_browser):
    settings, _ = creation_browser
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match='CREATION_RECEIPT_REQUIRED'):
        create_channel(settings, 'Не создавали', recover_chat_id='-456')


# Проверяем сценарий: invalid creation fields never open browser.
@pytest.mark.parametrize('name,description', [('', ''), ('a'*201, ''), ('Тест', 'a'*401)])
def test_invalid_creation_fields_never_open_browser(project, monkeypatch, name, description):
    settings = browser_config(project)
    monkeypatch.setattr(browser_channels, 'session', lambda *a: pytest.fail('browser opened'))
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match='CHANNEL_FIELDS_INVALID'):
        create_channel(settings, name, description)


# Проверяем сценарий: binding rejects concurrent configuration change.
def test_binding_rejects_concurrent_configuration_change(project):
    settings = browser_config(project)
    app = json.loads((project / 'config/app.json').read_text())
    app['max']['browser']['channels']['-123'] = 'Изменённый'
    (project / 'config/app.json').write_text(json.dumps(app))
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match='CONFIG_CHANGED'):
        browser_channels.bind(settings, {'chat_id':'-456','name':'Новый'})
