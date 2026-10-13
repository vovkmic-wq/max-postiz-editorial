import json
from contextlib import contextmanager

from max_postiz_agent import browser_corrections as corrections
from max_postiz_agent.config import fingerprint
from test_browser_publishing import browser_config, browser_page as browser_page_fixture

browser_page = browser_page_fixture


# Проверяем сценарий: saved unknown caption edit is only reconciled.
def test_saved_unknown_caption_edit_is_only_reconciled(project, browser_page, monkeypatch):
    settings = browser_config(project)
    payload = {"intent_id": "test-edit", "revision": 1, "chat_id": "-123",
               "old_body": "Старая подпись", "body": "Новая подпись"}
    path = project / "data/browser-corrections/test-edit-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"state": "unknown", "content_hash": fingerprint(payload)}))
    monkeypatch.setattr(corrections, "authorize", lambda *a: None)
    # Операция opened: данного модуля.
    @contextmanager
    def opened(*a):
        yield browser_page
    monkeypatch.setattr(corrections, "session", opened)
    # A saved unknown operation never enters edit mode or sends a new message.
    assert corrections.edit(settings, payload)["state"] == "unknown"
    assert browser_page.get_by_role("listitem").count() == 0


# Проверяем сценарий: caption edit changes existing post and preserves attachment after reload.
def test_caption_edit_changes_existing_post_and_preserves_attachment_after_reload(project, browser_page, monkeypatch):
    settings = browser_config(project)
    html = '''<meta charset="utf-8"><h2 id="main-header-title">Окно чата с Тест</h2>
    <main><div id="posts"></div><div id="menu" role="menu" hidden><button onclick="edit()">Редактировать</button></div>
    <div contenteditable role="textbox" aria-placeholder="Пост"></div>
    <button aria-label="Отправить сообщение" onclick="saveEdit()">Save</button></main>
    <script>
    const editor=document.querySelector('[contenteditable]');let inputBody='';let dirty=false;
    editor.oninput=()=>{dirty=true;inputBody=editor.innerText};
    function render(){document.querySelector('#posts').innerHTML='';
      const item=document.createElement('div');item.setAttribute('role','listitem');
      const text=document.createElement('span');text.textContent=localStorage.getItem('caption')||'Старая подпись';
      text.oncontextmenu=e=>{e.preventDefault();document.querySelector('#menu').hidden=false};
      item.append(text);const image=document.createElement('img');image.src='data:image/png;base64,iVBORw0KGgo=';
      item.append(image);document.querySelector('#posts').append(item)}
    function edit(){editor.textContent=localStorage.getItem('caption')||'Старая подпись';document.querySelector('#menu').hidden=true;dirty=false}
    function saveEdit(){if(dirty){localStorage.setItem('caption',inputBody);editor.textContent='';render()}}
    render();</script>'''
    browser_page.route("https://web.max.ru/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    # Операция opened: данного модуля.
    @contextmanager
    def opened(*a):
        yield browser_page
    monkeypatch.setattr(corrections, "session", opened)
    authorizations = []
    monkeypatch.setattr(corrections, "authorize", lambda *a: authorizations.append(True))
    payload = {"intent_id": "caption-edit", "revision": 1, "chat_id": "-123",
               "old_body": "Старая подпись", "body": "Новая подпись"}
    result = corrections.edit(settings, payload)
    assert result["state"] == "confirmed"
    assert len(authorizations) == 2
    assert browser_page.get_by_role("listitem").count() == 1
    assert browser_page.get_by_role("listitem").locator("img").count() == 1
    assert browser_page.get_by_text("Новая подпись", exact=True).count() == 1
    monkeypatch.setattr(corrections, "session", lambda *a: (_ for _ in ()).throw(AssertionError("reopened")))
    assert corrections.edit(settings, payload)["state"] == "confirmed"
