"use strict";

// Состояние содержит только роль и настройки интерфейса; ключ не сохраняется в JavaScript.
const state = {authenticated:false, role:null, tab:"channels", view:"workspace", profiles:[], busy:new Set(), settings:null, section:"app", calendar:null, publicationDrafts:new Map()};
const $ = id => document.getElementById(id);
const roles = {owner:"владелец",editor:"редактор",viewer:"наблюдатель"};
const errors = {
  AUTH_REQUIRED:"Сначала подключитесь к панели с ключом доступа.", AUTH_INVALID:"Ключ неверен или сессия завершена. Подключитесь заново.",
  SESSION_EXPIRED:"Сессия завершена. Введите ключ и подключитесь заново.", ROLE_FORBIDDEN:"Для этого действия нужны права владельца или редактора.",
  AUTH_ORIGIN_FORBIDDEN:"Откройте панель по тому же адресу, по которому запущен проект.",
  BROWSER_CHANNEL_NOT_ALLOWED:"Укажите разрешённый адрес MAX для выбранного канала.", INVALID_CHAT_ID:"Укажите числовой chat_id канала MAX.",
  BROWSER_LOGIN_OR_CHANNEL_REQUIRED:"Войдите в отдельном окне MAX и убедитесь, что аккаунт имеет доступ к каналу.",
  BROWSER_PROFILE_BUSY:"Браузер проекта занят. Завершите вход или дождитесь текущей операции.",
  BROWSER_START_FAILED:"Браузер не запустился. Проверьте выбранный браузер в настройках приложения.",
  BROWSER_MODE_NOT_ENABLED:"Выберите браузерный адаптер MAX в настройках приложения.", BROWSER_DEPENDENCY_REQUIRED:"В окружении проекта не установлен Playwright.",
  IMAGE_SCHEDULE_BUSY:"Сейчас выполняется другая операция. Её результат можно проверить кнопкой состояния.",
  IMAGE_CHANNEL_PROFILE_REQUIRED:"Для этого канала ещё не настроен подбор изображений. Заполните профиль в настройках.",
  CONFIG_SCHEMA_INVALID:"Настройки не соответствуют схеме: проверьте обязательные поля, время, дни и пределы.",
  SCHEDULE_DAILY_CAP_EXCEEDED:"Число отправок в расписании больше дневного лимита. Измените часы или лимит.",
  SCHEDULE_GAP_TOO_SMALL:"Часы стоят ближе, чем разрешённый минимальный промежуток между постами.",
  CHANNEL_UNRESOLVED_PUBLICATION:"Сначала сверьте незавершённую отправку в разделе «Публикации».",
  IMAGE_SCHEDULE_UNRESOLVED_PUBLICATION:"Сначала сверьте незавершённую отправку.", IMAGE_SCHEDULE_UNRESOLVED_CORRECTION:"Сначала сверьте незавершённую правку подписи.",
  SETTINGS_DOCUMENT_INVALID:"Проверьте структуру JSON настроек.", BACKUP_CONFIG_CONTAINS_SECRET:"Значения ключей вводятся в отдельных полях ниже, а не в JSON настроек.",
  NETWORK_FAILED:"Не удалось связаться с проектом. Проверьте, что он запущен.", SERVER_RESPONSE_INVALID:"Сервер вернул некорректный ответ.",
  SOURCE_DISABLED:"Источник выключен. Включите его в настройках источников.", PANEL_OPERATION_FAILED:"Сервер не завершил операцию. Проверьте состояние проекта и параметры действия.",
  INPUT_REQUIRED:"Заполните обязательные поля.", INVALID_JSON:"Исправьте JSON перед сохранением.", VIDEO_BACKEND_DISABLED:"Генератор видео ещё не подключён в настройках приложения.",
  FILE_PATH_INVALID:"Укажите корректный путь к файлу или папке.", FILE_PATH_FORBIDDEN:"Этот служебный файл недоступен для выбора из панели.",
  PATH_OUTSIDE_PROJECT:"Путь должен находиться внутри проекта. Внешний файл можно импортировать через «Выбрать файл».",
  FILE_TYPE_INVALID:"Выберите файл подходящего типа: для видео — MP4, для workflow — JSON.", FILE_PURPOSE_INVALID:"Неизвестный тип выбора файла.",
  FILE_DIRECTORY_REQUIRED:"Выберите существующую папку проекта.", FILE_KIND_INVALID:"Неверный режим выбора файла.",
  FILE_DIRECTORY_OUTSIDE_PROJECT:"Папка должна быть внутри проекта, чтобы настройки сохранялись при переносе.",
  FILE_DIRECTORY_PRIVATE:"Служебная папка не показывается в панели. Локально её можно открыть кнопкой «В Проводнике».",
  FILE_DIRECTORY_UNAVAILABLE:"Не удалось прочитать папку. Проверьте путь и доступ к ней.", FILE_NOT_FOUND:"Файл или папка ещё не существует.",
  FILE_TOO_LARGE:"Файл превышает допустимый размер. Для видео действует лимит из настроек проекта.",
  FILE_DIALOG_BUSY:"Завершите выбор в уже открытом системном диалоге.", FILE_DESKTOP_LOCAL_ONLY:"Проводник доступен при открытии панели на компьютере проекта. Для удалённого доступа используйте «Файлы проекта».",
  FILE_EXPLORER_UNAVAILABLE:"Проводник доступен в Windows. На сервере используйте встроенный выбор «Файлы проекта»."
};

// Обратная связь расположена над разделами и остаётся видимой при прокрутке.
function notice(text, kind="idle") { $("feedback").textContent=text; $("feedback").dataset.state=kind; }
function showError(error) { notice(error.code ? (error.message || errors[error.code] || "Действие не выполнено.") : "Действие не выполнено. Проверьте введённые данные.", "error"); }
function fail(code, message) { const error=new Error(message || errors[code] || ("Действие не выполнено: "+code)); error.code=code; return error; }

// Настоящие HTTP-ошибки, отсутствие JSON и потеря входа не превращаются в молчащий обработчик.
async function api(path, body, key) {
  const headers={"Content-Type":"application/json"};
  if (key) headers.Authorization="Bearer "+key;
  let response;
  try { response=await fetch(path,{method:body===undefined?"GET":"POST",credentials:"same-origin",headers,body:body===undefined?undefined:JSON.stringify(body)}); }
  catch (_) { throw fail("NETWORK_FAILED"); }
  let value;
  try { value=await response.json(); } catch (_) { throw fail("SERVER_RESPONSE_INVALID"); }
  if (!response.ok) {
    const code=value.code || (response.status===422?"CONFIG_SCHEMA_INVALID":"HTTP_"+response.status);
    if (response.status===401) resetSession();
    throw fail(code);
  }
  return value;
}

// Чтение и редактирование имеют разные разрешения; занятая кнопка не отправляет повторный запрос.
function syncButtons() {
  for (const button of document.querySelectorAll("button")) {
    if (["login","file-picker-close"].includes(button.id)) {button.disabled=state.busy.has(button);continue;}
    if (button.id==="logout") {button.disabled=!state.authenticated || state.busy.has(button);continue;}
    const allowed=!button.dataset.role || button.dataset.role===state.role || (button.dataset.role==="editor" && state.role==="owner");
    button.disabled=!state.authenticated || !allowed || state.busy.has(button);
    button.title=!state.authenticated?"Подключитесь к панели":!allowed?"Недостаточно прав для этого действия":"";
  }
}
function resetSession() {
  state.authenticated=false; state.role=null; state.profiles=[]; state.settings=null; state.publicationDrafts.clear();
  $("content").replaceChildren(); $("credentials-fields").replaceChildren();
  if($("file-picker").open){$("file-picker").close();filePicker=null;}
  $("status").textContent="Сессия завершена. Подключитесь к панели."; syncButtons();
}
function requireInput(value, message) { if(!String(value).trim())throw fail("INPUT_REQUIRED",message);return String(value).trim(); }
function parseJSON(text) {try{return JSON.parse(text);}catch(_){throw fail("INVALID_JSON");}}

// Каждая операция показывает начало, итог и ошибку; обновление списка не скрывает её результат.
async function perform(button, task, success) {
  if (state.busy.has(button)) return;
  state.busy.add(button); syncButtons(); button.setAttribute("aria-busy","true");
  notice("Выполняется: "+button.textContent.trim()+"…","working");
  try {
    const value=await task();
    $("result").textContent=JSON.stringify(value===undefined?{state:"done"}:value,null,2);
    notice(typeof success==="function"?success(value):(success || summary(value)),"success");
    return value;
  } catch(error) {showError(error);if($("file-picker").open)$("file-picker-feedback").textContent=error.message || "Выбор не выполнен.";return undefined;}
  finally {state.busy.delete(button);button.removeAttribute("aria-busy");syncButtons();}
}
function bind(id, task, success) {const button=$(id);button.onclick=()=>perform(button,task,success);}
function summary(value) {
  if(value && value.state==="selected")return value.imported?"Файл скопирован в проект, путь подставлен в поле.":"Путь подставлен в поле. Сохраните изменения отдельной кнопкой.";
  if(value && value.state==="cancelled")return "Выбор отменён. Значение поля сохранено.";
  if(value && value.state==="explorer_opened")return value.directory?"Папка открыта в Проводнике на компьютере проекта.":"Папка открыта в Проводнике на компьютере проекта; выбранный файл выделен.";
  if(value && value.state==="file_picker_opened")return "Выберите файл или папку в открытом списке проекта.";
  if(value && typeof value.paused==="boolean")return value.paused?"Канал поставлен на паузу.":"Канал возобновлён.";
  if(value && value.queued)return "Задание добавлено в очередь проекта.";
  if(value && value.scheduled)return "Черновик одобрен и добавлен в календарь.";
  if(value && value.state==="already_logged_in")return "Вход в MAX уже выполнен. Выбранный канал доступен для публикации.";
  if(value && value.state==="login_window_requested")return "Открыто отдельное окно MAX. Войдите по QR-коду, затем нажмите «Проверить браузер».";
  if(value && value.publish===true)return "Браузер работает. Доступ к выбранному каналу и право публикации подтверждены.";
  if(value && value.schedule) return "Расписание: "+value.schedule.state+(value.schedule.next_run_at?". Следующий запуск: "+new Date(value.schedule.next_run_at).toLocaleString("ru-RU",{timeZone:value.schedule.timezone}):".");
  if(value && value.restart_required && (value.restart_required===true || value.restart_required.length))return "Настройки сохранены. Для смены хранилища перезапустите проект; расписания остановлены.";
  if(value && value.state==="saved")return "Настройки сохранены. Проверьте подключения и включите нужные расписания.";
  if(value && value.scan_state)return "Сбор: "+value.scan_state+". В подборке "+value.top.length+" изображений.";
  if(value && value.state==="ready")return "Сбор завершён. В подборке "+value.top.length+" изображений; отправка не выполнялась.";
  if(value && value.next_run_at)return "Следующий запуск: "+new Date(value.next_run_at).toLocaleString("ru-RU",{timeZone:value.timezone});
  if(value && value.state==="not_due")return "Время следующей отправки ещё не наступило.";
  if(value && value.state==="already_checked")return "Этот интервал уже обработан; повторной отправки нет.";
  return "Готово. Результат доступен в «Последнем ответе сервера».";
}

// Профили выбираются из конфигурации; уже подключённый канал имеет приоритет при первом входе.
async function loadChannels() {
  state.profiles=await api("/api/v1/channel-profiles");
  for (const id of ["channel","calendar-channel","profile-channel","image-channel"]) {
    const select=$(id), old=select.value;
    const rows=id==="image-channel"?state.profiles.filter(row=>row.schedule.state!=="not_configured"):state.profiles;
    select.replaceChildren();
    for(const row of rows){const option=document.createElement("option");option.value=row.channel.id;option.textContent=row.channel.name+" ("+row.channel.id+")";select.append(option);}
    const preferred=rows.find(row=>row.channel.id===old)||rows.find(row=>row.chat_id)||rows[0];
    if(preferred)select.value=preferred.channel.id;
  }
  selectChannel();
}
function profile(id=$("channel").value) {const value=state.profiles.find(row=>row.channel.id===id);if(!value)throw fail("INPUT_REQUIRED","Выберите профиль канала.");return value;}
function selectChannel() {
  const row=state.profiles.find(item=>item.channel.id===$("channel").value);
  $("chat").value=row && row.chat_id?row.chat_id:"";
  $("channel-context").textContent=row?(row.chat_id?"Подключён MAX "+row.chat_id+". Канал "+(row.paused?"на паузе между отправками.":"активен."):"Профиль ещё не подключён к MAX. Укажите разрешённый chat_id."):"Настройте первый канал.";
}
function chatId(){return requireInput($("chat").value,"Укажите chat_id выбранного канала MAX.");}
function channelPath(action){return "/api/v1/channels/"+encodeURIComponent($("channel").value)+"/"+action;}

// Вход повторно с пустым полем сохраняет действующую сессию вместо её удаления.
bind("login",async()=>{
  const key=$("token").value.trim();
  let value;
  try {value=key?await api("/api/v1/session/login",{},key):await api("/api/v1/session");}
  finally {$("token").value="";}
  state.authenticated=true;state.role=value.role;
  $("status").textContent="Подключено: "+roles[state.role]+". Вход сохраняется при обновлении страницы.";
  await loadChannels(); await refresh();return value;
},"Вход выполнен. Выберите канал и действие.");
bind("logout",async()=>{await api("/api/v1/session/logout",{});resetSession();return {authenticated:false};},"Вы вышли из панели.");
$("token").onkeydown=event=>{if(event.key==="Enter")$("login").click();};
$("channel").onchange=selectChannel;

// Списки очищаются при смене вкладки; старые записи не выдаются за новый результат.
function selectView(view) {
  state.view=view;
  $("workspace-panel").hidden=view!=="workspace";$("calendar-panel").hidden=view!=="calendar";$("settings-panel").hidden=view!=="settings";
  $("publications-help").hidden=view!=="workspace" || state.tab!=="publications";
  for(const button of document.querySelectorAll("#tabs button"))button.classList.toggle("active",button.dataset.tab===state.tab && view==="workspace" || button.dataset.view===view);
}
async function refresh() {
  const tab=state.tab;
  $("content").replaceChildren();
  $("publications-count").textContent="";
  let rows;
  // Черновики добавляют читаемый текст к журналу отправок; оба запроса только читают данные.
  if(tab==="publications"){
    const result=await Promise.all([api("/api/v1/publications"),api("/api/v1/drafts")]);
    // Медленный ответ прежней вкладки не должен заменить содержимое уже выбранного раздела.
    if(state.tab!==tab || state.view!=="workspace")return {section:tab};
    rows=result[0];state.publicationDrafts=new Map(result[1].map(draft=>[draft.id,draft]));
    // Сначала показываем последние попытки, независимо от того, удалась отправка или нет.
    rows.sort((a,b)=>(b.send_started_at || b.published_at || 0)-(a.send_started_at || a.published_at || 0));
    const confirmed=rows.filter(row=>row.state==="confirmed").length, rejected=rows.filter(row=>row.state==="rejected").length;
    $("publications-count").textContent="Всего попыток: "+rows.length+" · Опубликовано: "+confirmed+" · Отклонено: "+rejected+" · В процессе или требуют проверки: "+(rows.length-confirmed-rejected)+". Показана история всех каналов.";
  }else rows=await api("/api/v1/"+tab);
  if(state.tab!==tab || state.view!=="workspace")return {section:tab};
  if(!rows.length){const empty=document.createElement("p");empty.className="empty";empty.textContent="В этом разделе пока нет записей.";$("content").append(empty);}
  for(const row of rows)renderCard(row);
  syncButtons();return {section:state.tab,records:rows.length};
}
bind("refresh",refresh,"Список обновлён.");
for(const button of document.querySelectorAll("[data-tab]"))button.onclick=()=>perform(button,async()=>{
  state.tab=button.dataset.tab;selectView("workspace");$("heading").textContent=button.textContent;return refresh();
},"Раздел обновлён.");

// Кнопки записей пользуются теми же обработчиками, что и основные действия.
function cardButton(card,label,task,role="owner",refreshAfter=true) {
  const button=document.createElement("button");button.textContent=label;button.dataset.role=role;
  button.onclick=()=>perform(button,async()=>{const value=await task();if(refreshAfter)await refresh();return value;});card.append(button);return button;
}
function input(card,placeholder,value="",multiline=false) {
  // Подпись остаётся видимой после ввода текста, когда подсказка внутри поля исчезает.
  const label=document.createElement("label");label.textContent=placeholder;
  const element=document.createElement(multiline?"textarea":"input");element.placeholder=placeholder;element.value=value;label.append(element);card.append(label);return element;
}

// Рядом с любым полем пути доступны системный выбор, список проекта и открытие Проводника.
function fileButton(target,label,task){
  const button=document.createElement("button");button.type="button";button.textContent=label;button.dataset.role="owner";
  button.onclick=()=>perform(button,task);target.append(button);return button;
}
function fileControls(element,{kind="file",purpose="file",onSelect=null}={}){
  const row=document.createElement("div");row.className="file-controls";element.parentElement.after(row);
  row.dataset.pathKey=element.dataset.pathKey || element.placeholder;
  const apply=value=>{element.value=value.path;if(onSelect)onSelect(value);element.dispatchEvent(new Event("change",{bubbles:true}));};
  const options=()=>({kind,purpose,path:element.value.trim()});
  fileButton(row,kind==="directory"?"Выбрать папку…":"Выбрать файл…",async()=>{
    const value=await api("/api/v1/files/choose",options());
    if(value.state==="selected")apply(value);
    else if(value.state==="unavailable")return openFilePicker(options(),apply);
    return value;
  });
  fileButton(row,"Файлы проекта",()=>openFilePicker(options(),apply));
  fileButton(row,"В Проводнике",()=>api("/api/v1/files/explorer",{path:element.value.trim()}));
  syncButtons();return row;
}

// Общий диалог запоминает целевое поле; одна и та же навигация обслуживает видео и настройки.
let filePicker=null;
async function openFilePicker(options,apply){
  if($("file-picker").open)throw fail("FILE_DIALOG_BUSY");
  filePicker={...options,apply};
  $("file-picker-title").textContent=options.kind==="directory"?"Выбор папки в проекте":"Выбор файла в проекте";
  $("file-picker-context").textContent=options.purpose==="video"?"Показаны папки и ролики MP4.":options.purpose==="workflow"?"Показаны папки и графы JSON. Граф загрузится вместе с выбранным путём.":options.purpose==="private"?"Выбирается путь к файлу доступа. Его содержимое не выводится в панели.":"Файлы и папки на компьютере, где запущен проект.";
  $("file-picker-feedback").textContent="";$("file-picker-list").replaceChildren();
  $("file-picker-folder").hidden=options.kind!=="directory";$("file-picker").showModal();
  await browseFiles(options.path);return {state:"file_picker_opened"};
}
async function browseFiles(path){
  const value=await api("/api/v1/files?path="+encodeURIComponent(path)+"&purpose="+filePicker.purpose);
  filePicker.path=value.path;filePicker.parent=value.parent;
  $("file-picker-path").textContent="Папка проекта: "+(value.path==="."?"корень":value.path);
  $("file-picker-list").replaceChildren();$("file-picker-feedback").textContent=value.truncated?"Показаны первые 1000 записей. Уточните папку.":"";
  for(const item of value.entries){
    // Для выбора папки файлы не показываем; каждый файл выбирается одним нажатием.
    if(filePicker.kind==="directory" && !item.directory)continue;
    const button=fileButton($("file-picker-list"),(item.directory?"Папка: ":"Файл: ")+item.name,()=>item.directory?browseFiles(item.path):selectFile(item.path));
    button.className="file-entry";button.dataset.path=item.path;
  }
  if(!$("file-picker-list").children.length){const empty=document.createElement("p");empty.textContent="В этой папке нет подходящих файлов или подпапок.";$("file-picker-list").append(empty);}
  syncButtons();return {state:"file_picker_opened"};
}
async function selectFile(path){
  const value=await api("/api/v1/files/select",{path,kind:filePicker.kind,purpose:filePicker.purpose});
  filePicker.apply(value);$("file-picker").close();filePicker=null;return value;
}
bind("file-picker-root",()=>browseFiles(""));
bind("file-picker-up",()=>browseFiles(filePicker.parent || ""));
bind("file-picker-folder",()=>selectFile(filePicker.path));
bind("file-picker-close",()=>{$("file-picker").close();filePicker=null;return {state:"cancelled"};});
$("file-picker").oncancel=()=>{filePicker=null;notice("Выбор отменён. Значение поля сохранено.");};

// Ищем именно локальные пути, сохраняя адреса URL и имена переменных окружения обычным текстом.
function filePaths(value,prefix=[]){
  const result=[];
  if(!value || typeof value!=="object")return result;
  for(const [key,item] of Object.entries(value)){
    const keys=prefix.concat(key);
    if(/^(path|directory)$|_(path|dir)$/.test(key) && (typeof item==="string" || item===null) && !/^\w+:\/\//.test(item || ""))
      result.push({keys,value:item || "",kind:/^(directory)$|_dir$/.test(key)?"directory":"file",purpose:key==="workflow_path"?"workflow":/access_path$/.test(key)?"private":"file"});
    else if(item && typeof item==="object")result.push(...filePaths(item,keys));
  }
  return result;
}
function fileLink(target,path,label="Файл проекта"){
  if(!path)return;
  const row=document.createElement("div");row.className="file-link";const text=document.createElement("span");text.textContent=label+": ";row.append(text);
  const button=fileButton(row,path,()=>api("/api/v1/files/explorer",{path}));button.className="path-link";button.title="Открыть папку и выделить файл в Проводнике на компьютере проекта";target.append(row);
}

// Поля путей в настройках меняют тот же JSON; выбранный workflow обновляет и граф, и его адрес.
const pathLabels={topic_path:"Файл тематики каналов",sources_path:"Файл источников",state_dir:"Папка состояния",media_dir:"Папка медиафайлов",logs_dir:"Папка журналов",profile_dir:"Профиль браузера MAX",workflow_path:"Файл workflow ComfyUI",isolated_state_dir:"Данные OpenCode",runtime_dir:"Установка OpenCode",local_access_path:"Файл доступа OpenCode"};
function settingsPaths(){
  const target=$("settings-paths");target.replaceChildren();
  const value=parseJSON($("settings-json").value),paths=filePaths(value);target.hidden=!paths.length;
  if(!paths.length)return;
  const heading=document.createElement("h3");heading.textContent="Пути к файлам и папкам";
  const help=document.createElement("p");help.className="hint";help.textContent="Выбор подставляет относительный путь в настройки. Внешний файл копируется внутрь проекта. «В Проводнике» открывает папку на компьютере проекта.";target.append(heading,help);
  for(const item of paths){
    const key=item.keys[item.keys.length-1],field=input(target,(pathLabels[key] || "Путь")+" ("+state.section+"."+item.keys.join(".")+")",item.value);
    field.dataset.pathKey=item.keys.join(".");
    const apply=selected=>{
      const documentValue=parseJSON($("settings-json").value);let parent=documentValue;
      for(const part of item.keys.slice(0,-1))parent=parent[part];
      parent[key]=selected.path || (item.purpose==="workflow"?null:"");
      $("settings-json").value=JSON.stringify(documentValue,null,2);state.settings[state.section]=documentValue;
      if(item.purpose==="workflow" && selected.document)state.settings.workflow=selected.document;
      if(item.purpose==="workflow" && !selected.path)state.settings.workflow=null;
    };
    field.onchange=()=>apply({path:field.value.trim()});fileControls(field,{kind:item.kind,purpose:item.purpose,onSelect:apply});
  }
}

// Состояние отправки переводим отдельно от данных сервера: исходные значения остаются в журнале.
const publicationStates={
  confirmed:["Опубликовано","Проект подтвердил появление поста в канале MAX."],
  rejected:["Отправка отклонена","Эта попытка не завершилась публикацией. Причину можно проверить в журнале проекта."],
  pending:["Ожидает отправки","Запись отправки создана, подтверждения публикации пока нет."],
  sending:["Отправляется","Отправка началась, итог ещё не подтверждён. Сверка проверяет её результат без повторной отправки."],
  unknown:["Результат неизвестен","Пост мог появиться в MAX. Сверьте результат, прежде чем предпринимать новую отправку."],
  retry_wait:["Ожидает повторной попытки","Публикация не подтверждена; дальнейший запуск зависит от расписания и правил проекта."]
};

// Комментарии добавляем только к отображаемой служебной записи, а не к редактируемому JSON.
const publicationFields={
  id:"Номер попытки отправки (intent_id); нужен при проверке результата.",
  draft_id:"Номер связанного черновика: откуда взяты текст и вложение.",
  channel_id:"Профиль канала в проекте; его название показано на карточке.",
  state:"Результат: confirmed — опубликовано, rejected — отклонено, unknown — неизвестен, sending — отправляется, pending — ожидает, retry_wait — ожидание повторной попытки.",
  external_id:"Служебный идентификатор сообщения или браузерной отправки; не обязательно ссылка на пост.",
  permalink:"Прямая ссылка на пост, если MAX её вернул; null означает, что ссылки нет.",
  send_started_at:"Когда началась отправка: секунды от 01.01.1970 UTC. Читаемая дата показана выше.",
  published_at:"Время подтверждения публикации в секундах от 01.01.1970 UTC; null — подтверждения нет.",
  body_hash:"Контрольный отпечаток текста при отправке: помогает обнаруживать изменения.",
  retry_after:"Служебная отметка времени ограничения повторной попытки; не расписание следующего поста."
};
function publicationCode(row){
  const lines=["{"];const entries=Object.entries(row);
  entries.forEach(([key,value],index)=>{
    lines.push("  // "+(publicationFields[key] || "Дополнительное служебное поле."));
    lines.push("  "+JSON.stringify(key)+": "+JSON.stringify(value)+(index<entries.length-1?",":""));
  });
  return lines.concat("}").join("\n");
}

// Любое окно с кодом имеет подпись и назначение; журнал отправки по умолчанию свёрнут.
const dataTitles={channels:"Данные профиля канала",sources:"Состояние источника",videos:"Данные видеопроекта",assets:"Данные готового видео",drafts:"Данные черновика","source-media":"Данные исходного материала",jobs:"Данные фонового задания",campaigns:"Данные продвижения",audit:"Запись журнала"};
function recordDetails(card,row){
  const publication=state.tab==="publications";
  const details=document.createElement("details"), title=document.createElement("summary");
  details.open=!publication;
  title.textContent=publication?"Техническая запись отправки — JSON с русскими комментариями":(dataTitles[state.tab] || "Данные записи")+" (JSON)";
  const help=document.createElement("p");help.className="hint";
  help.textContent=publication?"Только для просмотра. Строки // объясняют каждое поле; null означает отсутствие значения. Это служебная запись, её не нужно редактировать или вставлять в настройки.":"Только для просмотра. id — номер записи, state/status — состояние, channel_id — профиль канала. Действия выполняются кнопками, а не правкой этого блока.";
  const data=document.createElement("pre");data.setAttribute("aria-label",title.textContent+", только для просмотра");
  data.textContent=publication?publicationCode(row):JSON.stringify(row,null,2);
  details.append(title,help,data);card.append(details);
}

// Даты отображаем в часовом поясе профиля канала, а не компьютера, где открыт браузер.
function publicationDate(stamp,zone){
  if(!stamp)return "Не зафиксировано";
  const date=new Date(stamp*1000);if(!Number.isFinite(date.getTime()))return "Некорректная дата";
  return date.toLocaleString("ru-RU",{timeZone:zone})+" · "+zone;
}
function publicationFact(target,label,value){
  const term=document.createElement("dt"),description=document.createElement("dd");
  term.textContent=label;description.textContent=value;target.append(term,description);
}

// Карточка показывает текущий текст связанного поста; история отправки сохраняет исходный отпечаток.
function renderPublication(card,row){
  const draft=state.publicationDrafts.get(row.draft_id), current=draft && draft.current || {};
  const channel=state.profiles.find(item=>item.channel.id===row.channel_id);
  const zone=channel?channel.channel.publication.timezone:"Europe/Moscow";
  const status=publicationStates[row.state] || ["Неизвестное состояние","Проверьте техническую запись и журнал проекта."];
  card.classList.add("publication-card");card.dataset.state=row.state;
  const heading=document.createElement("h3");heading.textContent=current.title || "Публикация без заголовка";
  const badge=document.createElement("span");badge.className="publication-state";badge.textContent=status[0];card.append(heading,badge);
  const facts=document.createElement("dl");facts.className="publication-facts";
  publicationFact(facts,"Канал",channel?channel.channel.name:row.channel_id);
  publicationFact(facts,row.state==="confirmed"?"Опубликовано":"Начало попытки",publicationDate(row.state==="confirmed"?row.published_at:row.send_started_at,zone));
  publicationFact(facts,"Материал",current.image?"Изображение с подписью":current.video?"Видео с подписью":draft?"Текстовый пост":"Черновик недоступен");card.append(facts);
  const explanation=document.createElement("p");explanation.className="hint";explanation.textContent=status[1];card.append(explanation);
  // Предпросмотр читает уже сохранённый файл через защищённый маршрут; отправки он не запускает.
  if(current.image){
    const media=document.createElement("details"),title=document.createElement("summary"),image=document.createElement("img");
    title.textContent="Изображение публикации";image.alt="Сохранённое изображение: "+heading.textContent;image.loading="lazy";
    image.src="/api/v1/drafts/"+encodeURIComponent(row.draft_id)+"/image";
    image.onerror=()=>{image.hidden=true;const hint=document.createElement("p");hint.className="hint";hint.textContent="Локальный файл предпросмотра недоступен. Состояние отправки показано выше.";media.append(hint);};
    media.append(title,image);card.append(media);
    fileLink(card,current.image.path,"Сохранённое изображение");
  }
  if(current.body){
    const text=document.createElement("details"),title=document.createElement("summary"),body=document.createElement("p");
    title.textContent="Текущий текст поста";body.className="publication-body";body.textContent=current.body;text.append(title,body);card.append(text);
  }
  if(current.video)fileLink(card,current.video.path,"Сохранённое видео");
  // Адрес канала берём из подключённого профиля; он работает и без прямой ссылки на пост.
  if(channel && /^-?\d+$/.test(channel.chat_id || "")){
    const link=document.createElement("a");link.textContent="Открыть канал в MAX";link.href="https://web.max.ru/"+channel.chat_id;link.target="_blank";link.rel="noopener noreferrer";card.append(link);
  }
}
function renderCard(row) {
  const card=document.createElement("article");card.className="card";
  if(state.tab==="publications")renderPublication(card,row);
  recordDetails(card,row);
  if(state.tab!=="publications")for(const item of filePaths(row).slice(0,30))fileLink(card,item.value,item.keys.join("."));
  if(state.tab==="sources")cardButton(card,"Собрать источник",()=>api("/api/v1/sources/"+row.id+"/collect",{}));
  if(state.tab==="videos"){
    for(const pair of [["approve-generation","Одобрить генерацию"],["poll","Проверить результат"],["assemble","Собрать ролик"],["cancel","Остановить проект"]])
      cardButton(card,pair[1],()=>api("/api/v1/videos/"+row.id+"/"+pair[0],{revision:row.revision,plan_hash:row.plan_hash}));
    const index=input(card,"Номер сцены","0");index.type="number";index.min="0";
    const path=input(card,"Путь готовой сцены"),rights=input(card,"Права на сцену");
    fileControls(path,{purpose:"video"});
    cardButton(card,"Импортировать готовую сцену",()=>api("/api/v1/videos/"+row.id+"/import-scene",{index:Number(index.value),path:requireInput(path.value,"Укажите путь сцены."),rights_basis:requireInput(rights.value,"Укажите права на сцену.")}));
  }
  if(state.tab==="source-media"){
    const caption=input(card,"Подпись подборки","",true);
    cardButton(card,"Подготовить подборку",()=>api("/api/v1/curate",{channel_id:$("channel").value,media_id:row.id,caption:requireInput(caption.value,"Введите подпись подборки.")}));
  }
  if(state.tab==="assets")cardButton(card,"Просмотреть ролик",async()=>{
    const response=await fetch("/api/v1/assets/"+row.id+"/preview",{credentials:"same-origin"});
    if(!response.ok)throw fail("VIDEO_PREVIEW_FAILED","Предпросмотр ролика недоступен.");
    const video=document.createElement("video");video.controls=true;video.width=280;
    const url=URL.createObjectURL(await response.blob());video.src=url;video.onloadeddata=()=>URL.revokeObjectURL(url);card.append(video);
    return {state:"preview_ready"};
  },"",false);
  if(state.tab==="drafts" && row.state==="published"){
    const caption=input(card,"Опубликованная подпись",row.current.body,true);
    cardButton(card,"Исправить опубликованную подпись",async()=>{
      const items=await api("/api/v1/publications"),intent=items.find(item=>item.draft_id===row.id && item.state==="confirmed");
      if(!intent)throw fail("PUBLICATION_NOT_CONFIRMED","Подтверждённая публикация не найдена.");
      return api("/api/v1/publications/"+intent.id+"/correct",{revision:row.revision,body:requireInput(caption.value,"Введите новую подпись.")});
    });
  }
  if(state.tab==="drafts" && row.state!=="published"){
    const path=input(card,"Путь MP4"),rights=input(card,"Основание права на публикацию");
    fileControls(path,{purpose:"video"});
    cardButton(card,"Приложить MP4",()=>api("/api/v1/drafts/"+row.id+"/video",{path:requireInput(path.value,"Укажите путь MP4."),rights_basis:requireInput(rights.value,"Укажите права на видео.")}));
    cardButton(card,row.commercial?"Снять коммерческую отметку":"Коммерческий материал",()=>api("/api/v1/drafts/"+row.id+"/commercial",{revision:row.revision,commercial:!row.commercial}),"editor");
    const body=input(card,"Текст черновика",row.current.body,true);
    cardButton(card,"Сохранить новую ревизию",()=>api("/api/v1/drafts/"+row.id+"/revise",{revision:row.revision,body:requireInput(body.value,"Введите текст.")}),"editor");
    cardButton(card,"Одобрить в календарь",()=>api("/api/v1/drafts/"+row.id+"/approve",{revision:row.revision,content_hash:row.content_hash}),"editor");
    cardButton(card,"Отклонить",()=>api("/api/v1/drafts/"+row.id+"/reject",{revision:row.revision}),"editor");
  }
  if(state.tab==="publications" && ["sending","unknown"].includes(row.state))
    cardButton(card,"Сверить результат",()=>api("/api/v1/publications/"+row.id+"/reconcile",{}));
  $("content").append(card);
}
for(const name of ["pause","resume","bootstrap"])bind(name,async()=>{const value=await api(channelPath(name),{});await loadChannels();await refresh();return value;});
bind("connect",async()=>{const value=await api(channelPath("connect"),{chat_id:chatId()});await loadChannels();await refresh();return value;},"Подключение канала проверено и сохранено.");
bind("growth",()=>api(channelPath("growth-plan"),{}));
bind("report",()=>api("/api/v1/reports/"+encodeURIComponent($("channel").value)),"Отчёт готов.");
bind("validate",()=>api("/api/v1/config/validate",{}),"Действующая конфигурация прошла проверку.");
bind("activate",async()=>{const result=await api("/api/v1/config/activate",{});await loadChannels();return result;},"Конфигурация применена.");
bind("generate-plan",async()=>{const value=await api("/api/v1/videos",{channel_id:$("channel").value,idea:requireInput($("video-idea").value,"Введите идею ролика.")});await refresh();return value;});
bind("create-plan",async()=>{const value=await api("/api/v1/videos",{channel_id:$("channel").value,plan:parseJSON($("video-plan").value)});await refresh();return value;});
bind("browser-login",()=>api("/api/v1/browser/login",{chat_id:chatId()}));
bind("browser-check",()=>api("/api/v1/browser/check",{chat_id:chatId()}));

// Сбор, состояние и управление расписанием используют выбранный профиль изображений.
const imageChannel=()=>encodeURIComponent(requireInput($("image-channel").value,"Настройте профиль изображений."));
for(const name of ["status","scan","pause","resume","tick"])bind("images-"+name,async()=>{
  const channel=imageChannel();let value;
  if(name==="status")value=await api("/api/v1/images/status?channel_id="+channel);
  else if(name==="scan")value=await api("/api/v1/images/scan",{all_sources:true,channel_id:decodeURIComponent(channel)});
  else value=await api("/api/v1/images/schedule/"+name+"?channel_id="+channel,{});
  $("images-result").textContent=JSON.stringify(value,null,2);return value;
});

// Календарь отображает даты из сервера в часовом поясе канала, без зависимости от часов браузера.
const dayNames=["Пн","Вт","Ср","Чт","Пт","Сб","Вс"];
for(let index=0;index<7;index++){
  const label=document.createElement("label");label.className="checkbox";const box=document.createElement("input");
  box.type="checkbox";box.value=String(index);box.checked=true;label.append(box,document.createTextNode(dayNames[index]));$("schedule-days").append(label);
}
async function loadCalendar() {
  const channel=requireInput($("calendar-channel").value,"Выберите канал.");
  const date=$("calendar-date").value;
  const value=await api("/api/v1/calendar?channel_id="+encodeURIComponent(channel)+(date?"&start="+date:""));
  state.calendar=value;$("calendar-date").value=value.week_start;
  $("holiday-enabled").checked=value.holiday_search_enabled;
  $("holiday-info").textContent="Каталог: "+value.holiday_count+" праздничных дат · проверенный год "+value.holiday_catalog_year+". Названия используются только в первом запуске дня.";
  $("calendar-range").textContent=value.week_start+" — "+value.week_end+" · "+value.timezone;
  $("calendar-grid").replaceChildren();$("calendar-detail").hidden=true;
  for(const day of value.days){
    const card=document.createElement("article");card.className="calendar-day"+(day.is_today?" today":"");
    const heading=document.createElement("h3");heading.textContent=day.weekday;
    const dateLabel=document.createElement("div");dateLabel.className="calendar-date";dateLabel.textContent=day.date.slice(8)+"."+day.date.slice(5,7);card.append(heading,dateLabel);
    for(const holiday of day.holidays || []){
      const label=document.createElement("div");label.className="holiday-tag";label.textContent=holiday.name;label.title="Поисковый тег: "+holiday.name;card.append(label);
    }
    for(const event of day.events){
      const button=document.createElement("button");button.className="calendar-event";button.dataset.state=event.state;
      const time=document.createElement("time");time.textContent=event.time;button.append(time,document.createTextNode(event.title));
      if(event.holiday_tags && event.holiday_tags.length){const tag=document.createElement("small");tag.textContent="Праздничный поиск: "+event.holiday_tags.join(", ");button.append(tag);}
      button.onclick=()=>perform(button,()=>showEvent(event),"Показаны сведения о публикации.");card.append(button);
    }
    if(!day.events.length){const text=document.createElement("p");text.className="hint";text.textContent="Нет публикаций";card.append(text);}
    $("calendar-grid").append(card);
  }
  const plan=value.schedule.plan || {kind:"weekly"};
  $("schedule-kind").value=plan.kind;$("schedule-interval").value=plan.interval_hours || "4";
  $("schedule-timezone").value=value.timezone;$("schedule-times").value=value.schedule.times.join(", ");
  for(const input of $("schedule-days").querySelectorAll("input"))input.checked=value.schedule.weekdays.includes(Number(input.value));
  $("schedule-gap").value=value.publication_policy.min_gap_minutes;$("schedule-limit").value=value.publication_policy.max_posts_per_day;
  $("schedule-enabled").checked=value.schedule.enabled===true; scheduleMode();syncButtons();return value;
}
async function showEvent(event){
  const target=$("calendar-detail");target.replaceChildren();target.hidden=false;
  const title=document.createElement("h3");title.textContent=event.time+" · "+event.title;
  const text=document.createElement("pre");text.textContent=event.body+"\n\nСостояние: "+event.state;target.append(title,text);
  if(event.kind==="publication" && event.image){
    const response=await fetch("/api/v1/drafts/"+event.id+"/image",{credentials:"same-origin"});
    if(response.ok){const image=document.createElement("img");image.alt="Изображение публикации";const url=URL.createObjectURL(await response.blob());image.src=url;image.onload=()=>URL.revokeObjectURL(url);target.append(image);}
  }
  return {event_id:event.id,state:event.state};
}
function scheduleMode(){const weekly=$("schedule-kind").value==="weekly";$("schedule-days").disabled=!weekly;$("schedule-times").disabled=!weekly;$("schedule-interval").disabled=weekly;}
$("schedule-kind").onchange=scheduleMode;
bind("calendar-tab",async()=>{selectView("calendar");return loadCalendar();},"Календарь загружен.");
bind("calendar-refresh",loadCalendar,"Календарь обновлён.");
bind("calendar-today",async()=>{$("calendar-date").value="";return loadCalendar();},"Показана текущая неделя.");
for(const name of ["prev","next"])bind("calendar-"+name,async()=>{
  const base=new Date($("calendar-date").value+"T12:00:00Z");base.setUTCDate(base.getUTCDate()+(name==="prev"?-7:7));$("calendar-date").value=base.toISOString().slice(0,10);return loadCalendar();
},"Неделя изменена.");
$("calendar-channel").onchange=()=>perform($("calendar-refresh"),loadCalendar,"Календарь канала загружен.");
$("calendar-date").onchange=()=>perform($("calendar-refresh"),loadCalendar,"Неделя загружена.");
bind("schedule-save",async()=>{
  const kind=$("schedule-kind").value, interval=Number($("schedule-interval").value);
  const times=kind==="interval"?Array.from({length:24/interval},(_,n)=>String(n*interval).padStart(2,"0")+":00"):$("schedule-times").value.split(",").map(value=>value.trim()).filter(Boolean);
  const weekdays=kind==="interval"?[0,1,2,3,4,5,6]:[...$("schedule-days").querySelectorAll("input:checked")].map(input=>Number(input.value));
  const value=await api("/api/v1/calendar/"+encodeURIComponent($("calendar-channel").value)+"/schedule",{kind,interval_hours:interval,times,weekdays,timezone:$("schedule-timezone").value.trim(),enabled:$("schedule-enabled").checked,min_gap_minutes:Number($("schedule-gap").value),max_posts_per_day:Number($("schedule-limit").value)});
  await loadChannels();await loadCalendar();return value;
},"Расписание сохранено. Следующее время отправки показано в календаре.");

// Праздники обновляются по фактическому каталогу; повторного праздничного запуска в день нет.
bind("holidays-toggle",async()=>{const value=await api("/api/v1/holidays/config",{enabled:$("holiday-enabled").checked});await loadCalendar();return value;},"Настройка праздничного поиска сохранена.");
bind("holidays-update",async()=>{const value=await api("/api/v1/holidays/update",{});await loadCalendar();return value;},"Каталог праздников обновлён.");

// Поля направления обновляют тот же полный профиль, который доступен в JSON.
async function loadProfile(){
  const row=profile($("profile-channel").value),documentValue={channel:row.channel,images:row.images};
  $("channel-profile-json").value=JSON.stringify(documentValue,null,2);
  $("profile-name").value=row.channel.name;$("profile-theme").value=row.channel.theme;$("profile-audience").value=row.channel.audience;
  $("profile-queries").value=row.images?row.images.queries.join(", "):"";$("profile-positive").value=row.images?row.images.local_prompts[0]:"";
  $("profile-negative").value=row.images?row.images.local_prompts[1]:"";$("profile-caption").value=row.images?row.images.caption_title:"";
  return documentValue;
}
for(const id of ["profile-name","profile-theme","profile-audience","profile-queries","profile-positive","profile-negative","profile-caption"])$(id).oninput=()=>{
  try{
    const value=parseJSON($("channel-profile-json").value);value.channel.name=$("profile-name").value;value.channel.theme=$("profile-theme").value;value.channel.audience=$("profile-audience").value;
    if(!value.images)value.images={queries:[],local_prompts:["",""],caption_title:""};
    value.images.queries=$("profile-queries").value.split(",").map(text=>text.trim()).filter(Boolean);
    value.images.local_prompts=[$("profile-positive").value,$("profile-negative").value];value.images.caption_title=$("profile-caption").value;
    $("channel-profile-json").value=JSON.stringify(value,null,2);
  }catch(error){showError(error);}
};
bind("channel-profiles",loadProfile,"Профиль загружен. Измените поля и примените его.");
$("profile-channel").onchange=()=>perform($("channel-profiles"),loadProfile,"Профиль загружен.");
bind("channel-apply",async()=>{const value=parseJSON($("channel-profile-json").value);const result=await api("/api/v1/channel-profiles",value);await loadChannels();await loadProfile();return result;},"Профиль сохранён. Канал и его расписание на паузе до проверки.");

// Настройки сохраняются цельным проверяемым документом; переход между разделами не теряет правки.
const sectionHelp={app:"Адаптер MAX, разрешённые адресаты браузера, локальные сервисы, OpenCode, видео, бюджеты и пути установки.",topic:"Темы, аудитории, рубрики, ограничения, язык и правила всех каналов.",sources:"Источники редакции, интервалы сбора и основания прав.",image_sources:"Бесплатные сайты, поисковые запросы и общие пороги просмотров, лайков и размеров.",image_channels:"Описания локальной CLIP, персональные фильтры, темы и расписания.",workflow:"Граф ComfyUI; путь задаётся в app.video.workflow_path. null означает, что граф не выбран.",holidays:"Названия, даты, дополнительные теги и подтверждённые ежегодные правила. Каталог можно дополнить или отключить праздничный поиск."};
function storeSection(){if(!state.settings)throw fail("INPUT_REQUIRED","Сначала загрузите настройки.");state.settings[state.section]=parseJSON($("settings-json").value);}
function displaySection(){state.section=$("settings-section").value;$("settings-json").value=JSON.stringify(state.settings[state.section],null,2);$("settings-help").textContent=sectionHelp[state.section] || "";settingsPaths();}
$("settings-json").onchange=()=>{try{settingsPaths();}catch(error){showError(error);}};
async function loadSettings(){
  const value=await api("/api/v1/settings");state.settings=value.configuration;displaySection();
  $("credentials-fields").replaceChildren();
  for(const item of value.credentials){
    const label=document.createElement("label");label.textContent=item.name+" · "+(item.configured?"настроен":"не настроен");
    const input=document.createElement("input");input.type="password";input.autocomplete="off";input.dataset.credential=item.name;input.placeholder="Новое значение; пустое — оставить прежнее";label.append(input);$("credentials-fields").append(label);
  }
  return {state:"loaded",sections:Object.keys(value.configuration)};
}
bind("settings-tab",async()=>{selectView("settings");await loadProfile();return loadSettings();},"Настройки загружены.");
bind("settings-load",loadSettings,"Настройки загружены.");
$("settings-section").onchange=()=>{
  try{storeSection();displaySection();}
  catch(error){$("settings-section").value=state.section;showError(error);}
};
bind("settings-format",async()=>{const value=parseJSON($("settings-json").value);$("settings-json").value=JSON.stringify(value,null,2);return {state:"formatted"};},"JSON отформатирован.");
bind("settings-validate",async()=>{storeSection();return api("/api/v1/settings/validate",state.settings);},"Изменения прошли проверку.");
bind("settings-save",async()=>{storeSection();const value=await api("/api/v1/settings/save",state.settings);await loadChannels();return value;});
bind("credentials-save",async()=>{
  const values={};for(const input of document.querySelectorAll("[data-credential]"))if(input.value)values[input.dataset.credential]=input.value;
  if(!Object.keys(values).length)throw fail("INPUT_REQUIRED","Введите хотя бы одно новое значение ключа.");
  try{return await api("/api/v1/settings/credentials",values);}
  finally{for(const input of document.querySelectorAll("[data-credential]"))input.value="";}
},"Новые значения сохранены локально. При замене ключа панели подключитесь заново.");

// При загрузке страницы восстанавливаем роль из HttpOnly-cookie; ожидаемое отсутствие входа не является ошибкой.
async function restoreSession(){
  syncButtons();
  try{
    const value=await api("/api/v1/session");state.authenticated=true;state.role=value.role;
    $("status").textContent="Подключено: "+roles[state.role]+". Сессия восстановлена.";
    await loadChannels();selectView("workspace");await refresh();notice("Панель подключена. Выберите действие.","success");
  }catch(error){
    if(!["AUTH_REQUIRED","AUTH_INVALID","SESSION_EXPIRED"].includes(error.code))showError(error);
    else notice("Введите ключ доступа и нажмите «Подключиться».");
  }finally{syncButtons();}
}
window.addEventListener("unhandledrejection",()=>notice("Не удалось завершить действие. Обновите панель или проверьте состояние сервера.","error"));
restoreSession();
