"use strict";
const el=id=>document.getElementById(id);
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.code||data.safe_message||'REQUEST_FAILED');return data;}
function status(value,error=false){el('status').textContent=value;el('status').className=error?'error':'';}
function node(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
function button(parent,label,fn){const b=node('button',label);b.onclick=async()=>{b.disabled=true;try{await fn();status('Готово');await refresh();}catch(e){status(e.message,true);}finally{b.disabled=false;}};parent.append(b);return b;}

async function renderVariant(v,state){
 const card=node('article');card.className='card';
 const meta={...v.metadata_json},target=state.targets.find(t=>t.id===v.target_id),platform=target?.platform;
 const hourly=meta.origin==='hourly_article_test',editable=['owner','editor'].includes(state.role);
 const postizTextCanonical=hourly&&Boolean(meta.postiz_post_id);
 const terminal=['published','abandoned'].includes(v.state);
 card.append(node('h3',`${v.target_id}: ${v.title}`),node('p',`Версия ${v.revision} · ${v.state}`));
 let details=null;
 if(hourly){
  try{details=await api(`/api/hub/variants/${v.id}/article-review`);}
  catch(error){details={machine_review_current:false,status:'details_unavailable'};card.append(node('p',error.message));}
  card.append(node('p',`Адресат: ${v.target_id} · ${target?.platform||'платформа недоступна'} · канал ${target?.destination||'недоступен — обновите карточку'}`));
  card.append(node('p','DRAFT в Postiz → дата и QUEUE в календаре Postiz → финальное «Согласовать» в Hub. После правки в Postiz нажмите «Получить правки Postiz» и повторите проверку. Человек сравнивает оригинал и перевод; машинная проверка не заменяет его решение.'));
  if(postizTextCanonical)card.append(node('p','Редактируйте текст в Postiz, затем получите правки и повторите Ministral + LanguageTool. В Hub текст показан только для сравнения и согласования.'));
 }
 const comparison=node('div');comparison.className=hourly?'article-comparison':'';card.append(comparison);
 if(hourly){
  const source=node('section');source.className='article-source';source.append(node('h4','Оригинал (English)'),node('h5',details.source_title||'Оригинал недоступен'),node('pre',details.source_body||''));
  try{const url=new URL(details.source_url);if(['http:','https:'].includes(url.protocol)){const link=node('a','Открыть источник');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';source.append(link);}}catch{}
  comparison.append(source);
 }
 const editor=node('div');editor.className='article-editor';comparison.append(editor);
 if(hourly)editor.append(node('h4','Перевод и правка (Русский)'));
 const title=node('input');title.value=v.title;title.readOnly=postizTextCanonical;title.setAttribute('aria-label','Заголовок');editor.append(title);
 const area=node('textarea');area.value=v.body;area.readOnly=postizTextCanonical;area.setAttribute('aria-label','Текст публикации');editor.append(area);
 let media=[...v.media],updateReviewControls=()=>{};
 const tags=node('input'),community=node('input');
 if(platform==='pikabu'){tags.value=(meta.tags||[]).join(', ');tags.setAttribute('aria-label','Теги через запятую');community.value=meta.community||'';community.placeholder='Сообщество (необязательно)';editor.append(tags,community);}
 const dirty=()=>area.value!==v.body||title.value!==v.title||JSON.stringify(media)!==JSON.stringify(v.media)||(platform==='pikabu'&&(tags.value!==(meta.tags||[]).join(', ')||community.value!==(meta.community||'')));
 const files=node('p','Вложения: '+media.length),upload=node('input');upload.type='file';upload.accept='image/png,image/jpeg,image/webp,video/mp4';upload.setAttribute('aria-label','Добавить вложение');editor.append(files);
 if(hourly)editor.append(node('p','Почасовой сценарий NASA использует только текст, без вложений.'));else editor.append(upload);
 upload.onchange=async()=>{if(!upload.files[0])return;try{const form=new FormData();form.append('file',upload.files[0]);const r=await fetch('/api/hub/media',{method:'POST',body:form,credentials:'same-origin'});const item=await r.json();if(!r.ok)throw Error(item.code||'Файл не принят');media.push(item);files.textContent='Вложения: '+media.length;updateReviewControls();status('Вложение добавлено. Сохраните новую версию.');}catch(error){status(error.message,true);}};
 const clear=node('button','Убрать вложения');clear.onclick=()=>{media=[];files.textContent='Вложения: 0';updateReviewControls();};if(!hourly||media.length)editor.append(clear);
 const save=button(editor,'Сохранить новую версию',()=>api(`/api/hub/variants/${v.id}/revise`,{revision:v.revision,body:area.value,title:title.value,media,...(platform==='pikabu'?{metadata:{tags:tags.value.split(',').map(t=>t.trim()).filter(Boolean),community:community.value}}:{})}));save.disabled=!editable||terminal||postizTextCanonical;
 if(!hourly)button(editor,'Адаптировать локальной LLM',()=>api(`/api/hub/variants/${v.id}/adapt`,{}));
 const date=node('input');date.type='datetime-local';date.setAttribute('aria-label','Дата и время в часовом поясе компьютера');
 if(meta.postiz_post_id){
  if(Number.isFinite(meta.postiz_due_at)){const when=new Date(meta.postiz_due_at*1000);date.step='0.001';date.value=new Date(when.getTime()-when.getTimezoneOffset()*60000).toISOString().slice(0,23);}
  date.readOnly=true;card.append(node('p',`Postiz: ${meta.postiz_state}. Дата задаётся в календаре Postiz.`));
  if(meta.origin==='postiz_max_bridge'&&!v.sources.length&&state.role==='owner')button(card,'Подтвердить авторский текст',()=>api(`/api/hub/variants/${v.id}/attest-original`,{revision:v.revision,content_hash:v.content_hash}));
 }else if(hourly){date.readOnly=true;card.append(node('p','Сначала создайте DRAFT, затем выберите дату в Postiz и получите правки.'));}
 card.append(date);
 let confirmation=null,machineState=null,recheck=null;
 if(hourly){
  machineState=node('p');machineState.setAttribute('role','status');card.append(machineState);
  const machineStatus=details.machine_review_status||(details.machine_review_current?'passed':'unconfirmed');
  card.append(node('p',`Статус машинной проверки: ${machineStatus}`));
  if(!details.machine_review_current){const blocked=node('p','Машинная проверка отклонила/не подтверждена; отправка заблокирована.');blocked.className='error';blocked.setAttribute('role','alert');card.append(blocked);}
  const reviewIssues=Array.isArray(details.machine_review_issues)?details.machine_review_issues.filter(issue=>typeof issue==='string'):[];
  if(reviewIssues.length){const issues=node('ul');issues.className='error';issues.setAttribute('aria-label','Замечания машинной проверки');for(const issue of reviewIssues)issues.append(node('li',issue));card.append(issues);}
  recheck=button(card,'Повторно проверить перевод: Ministral + LanguageTool',()=>{if(dirty())throw Error('Сначала сохраните изменения');return api(`/api/hub/variants/${v.id}/recheck-article`,{revision:v.revision,content_hash:v.content_hash});});
  const label=node('label');label.className='human-review';confirmation=node('input');confirmation.type='checkbox';confirmation.checked=false;confirmation.autocomplete='off';
  label.append(confirmation,document.createTextNode(' Я лично сравнил(а) полный английский оригинал и русский текст этой версии, проверил(а) адресата и дату и согласую публикацию.'));card.append(label);
 }
 const approval=button(card,'Согласовать и поставить в план',()=>{
  if(dirty())throw Error('Сначала сохраните изменения');if(!date.value)throw Error('Выберите дату и время');
  if(hourly&&(!details.machine_review_current||!confirmation.checked))throw Error('Нужны актуальная машинная проверка и личное согласование');
  if(hourly&&meta.postiz_state!=='QUEUE')throw Error('Выберите дату в календаре Postiz и получите состояние QUEUE');
  return api(`/api/hub/variants/${v.id}/approve`,{revision:v.revision,content_hash:v.content_hash,due_at:new Date(date.value).getTime()/1000,...(hourly?{human_review_confirmed:confirmation.checked}:{})});
 });
 if(hourly){
  updateReviewControls=()=>{
   const changed=dirty();if(changed)confirmation.checked=false;
   confirmation.disabled=!editable||terminal||changed||!details.machine_review_current;
   recheck.disabled=!editable||terminal||changed;
   machineState.textContent=changed?'Правки ещё не сохранены. После сохранения повторите машинную проверку.':details.machine_review_current?'Ministral + LanguageTool: проверка относится к текущему тексту. Требуется решение человека.':postizTextCanonical?'Машинная проверка устарела или отсутствует. Получите правки Postiz и выполните повторную проверку.':'Машинная проверка устарела или отсутствует. Сохраните текст и выполните повторную проверку.';
   machineState.className=details.machine_review_current&&!changed?'badge':'error';
   approval.disabled=confirmation.disabled||!confirmation.checked||meta.postiz_state!=='QUEUE';
  };
  for(const input of [title,area,tags,community])input.addEventListener('input',updateReviewControls);
  confirmation.addEventListener('change',updateReviewControls);updateReviewControls();
 }else approval.disabled=!editable||terminal;
 if(target?.route==='postiz'||(target?.route==='max_browser'&&target.options?.postiz_integration_id)){
  if(!meta.postiz_post_id){const stage=button(card,'Создать DRAFT в Postiz',()=>api(`/api/hub/variants/${v.id}/postiz`,{}));stage.disabled=!editable||terminal;}
  const sync=button(card,'Получить правки Postiz',()=>api(target.route==='max_browser'?'/api/hub/max-bridge/sync':`/api/hub/variants/${v.id}/sync`,{}));sync.disabled=!editable;
  if(state.postiz_public_origin){const link=node('a','Открыть Postiz: редактор и календарь');link.href=state.postiz_public_origin+'/';link.target='_blank';link.rel='noopener noreferrer';card.append(link);}
 }
 return card;
}

async function refresh(){const state=await api('/api/hub/state');await refreshReddit(state);el('login').hidden=true;el('main').hidden=false;el('runtime').textContent=`База: ${state.database} · Модель: ${state.model} (режим ${state.model_mode}) · Postiz: ${state.postiz_configured?'настроен':'ожидает подключения'}`;
el('tasks').replaceChildren();if(!state.tasks.length)el('tasks').append(node('p','Задачи задаются в настройках редакции.'));for(const task of state.tasks){const c=node('article');c.append(node('h3',task.id),node('p',`${task.enabled?'Включена':'Отключена'} · ${task.error||'без ошибок'} · попыток: ${task.attempts}`));if(task.enabled&&task.error)button(c,'Повторить после устранения причины',()=>api(`/api/hub/tasks/${task.id}/resume`,{}));el('tasks').append(c);}
el('targets').replaceChildren();for(const t of state.targets){const c=node('div',`${t.id} · ${t.platform} · ${t.route} · ${!t.enabled?'отключён':t.paused?'на паузе':'активен'}`);if(t.enabled)button(c,t.paused?'Снять паузу':'Пауза',()=>api(`/api/hub/targets/${t.id}/pause`,{paused:!t.paused}));el('targets').append(c);}
const cards=await Promise.all(state.variants.map(v=>renderVariant(v,state)));el('variants').replaceChildren(...cards);if(!cards.length)el('variants').append(node('p','Черновики появятся после сбора материалов и обработки моделью.'));
el('dispatches').replaceChildren();for(const d of state.dispatches){const c=node('article');c.className='card';c.append(node('h3',`${d.snapshot.target_id} · ${d.state}`),node('p',new Date(d.due_at*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' (Москва)'),node('p',d.error||''));if(d.permalink){const a=node('a','Публикация');a.href=d.permalink;a.rel='noopener';a.target='_blank';c.append(a);}if(d.state==='awaiting_manual'){for(const name of ['post.txt','post.html','post.json']){const a=node('a',name+' ');a.href=`/api/hub/dispatches/${d.id}/export/${name}`;c.append(a);}const u=node('input');u.placeholder='https://pikabu.ru/story/...';c.append(u);button(c,'Подтвердить ручную публикацию',()=>api(`/api/hub/dispatches/${d.id}/attest`,{revision:d.revision,snapshot_hash:d.snapshot_hash,url:u.value}));}if(['unknown','scheduled'].includes(d.state))button(c,'Сверить результат',()=>api(`/api/hub/dispatches/${d.id}/reconcile`,{}));if(['pending','scheduled','awaiting_manual','retry_wait'].includes(d.state))button(c,'Отменить',()=>api(`/api/hub/dispatches/${d.id}/cancel`,{}));el('dispatches').append(c);}}
el('signin').onclick=async()=>{try{const r=await fetch('/api/v1/session/login',{method:'POST',headers:{'Authorization':'Bearer '+el('key').value},credentials:'same-origin'});if(!r.ok)throw Error('AUTH_INVALID');el('key').value='';await refresh();status('Вход выполнен');}catch(e){status(e.message,true);}};
el('refresh').onclick=()=>refresh().catch(e=>status(e.message,true));el('tick').onclick=async()=>{try{await api('/api/hub/tick',{});await refresh();}catch(e){status(e.message,true);}};
el('search').onclick=async()=>{try{const rows=await api('/api/hub/archive?q='+encodeURIComponent(el('query').value));el('archive').replaceChildren();for(const row of rows){const c=node('article');c.append(node('h3',row.title),node('pre',row.text));el('archive').append(c);}}catch(e){status(e.message,true);}};
const redditStates={awaiting_text:'Нужен исходный текст',awaiting_rights:'Нужно основание для использования текста',queued:'В очереди перевода',processing:'Перевод и проверка',ready:'Черновик готов',failed:'Ошибка обработки',rejected:'Материал отклонён',needs_attention:'Обработка прервалась — требуется проверка'};
async function refreshReddit(state){
const data=await api('/api/hub/reddit');
const mailStates={not_configured:'ожидает подключения',configured:'настроена',disabled:'выключена',polling:'проверяется',ok:'подключена',mail_needs_review:'есть письма для ручной проверки'};
el('reddit-runtime').textContent=`Перевод: ${data.options.processing_enabled?'включён':'на паузе'} · Почта IFTTT: ${mailStates[data.mail.state]||data.mail.state} · Автопубликация: выключена`;
if(!el('reddit-mail-user').value&&data.mail.username)el('reddit-mail-user').value=data.mail.username;
const selector=el('reddit-target'),previous=selector.value;selector.replaceChildren();
for(const target of state.targets.filter(t=>t.enabled&&t.platform==='max')){const option=node('option',target.id);option.value=target.id;selector.append(option);}if(previous)selector.value=previous;
el('reddit-add').hidden=state.role==='viewer';el('reddit-mail-settings').hidden=state.role!=='owner';
el('reddit-mail-enabled').checked=data.mail.enabled;
el('reddit-items').replaceChildren();
if(!data.items.length)el('reddit-items').append(node('p','Пока нет материалов. Добавьте текст вручную или настройте IFTTT.'));
for(const item of data.items){
 const card=node('article');card.className='card';card.append(node('h3',item.title),node('p',`${item.origin==='ifttt'?'IFTTT':'Ручной отбор'} · ${redditStates[item.state]||item.state}`));
 const link=node('a','Оригинал Reddit');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);
 if(item.error)card.append(node('p',item.error));
 if(item.state==='ready'){card.append(node('p','Перевод доступен ниже в разделе «Черновики и согласование».'));}
 else if(!['processing','needs_attention'].includes(item.state)&&state.role!=='viewer'){
  const title=node('input');title.value=item.title;title.setAttribute('aria-label','Заголовок исходного материала');
  const text=node('textarea');text.value=item.original;text.maxLength=8500;text.setAttribute('aria-label','Исходный английский текст');card.append(title,text);
  button(card,'Сохранить исходный текст',()=>api(`/api/hub/reddit/items/${item.id}/text`,{title:title.value,original:text.value}));
  if(state.role==='owner'&&item.state!=='queued'){
   const evidence=node('input');evidence.placeholder='Основание: разрешение автора / лицензия / собственный текст';evidence.setAttribute('aria-label','Основание для перевода и публикации');card.append(evidence);
   button(card,'Подтвердить основание и отправить на перевод',()=>{if(title.value!==item.title||text.value!==item.original)throw Error('Сначала сохраните исходный текст');if(!item.original)throw Error('Сначала добавьте английский текст');return api(`/api/hub/reddit/items/${item.id}/rights`,{evidence:evidence.value,source_hash:item.source_hash});});
  }
 }
 if(state.role==='owner'&&['failed','needs_attention'].includes(item.state))button(card,'Повторить обработку после проверки',()=>api(`/api/hub/reddit/items/${item.id}/resume`,{}));
 el('reddit-items').append(card);
}}
el('reddit-add').onsubmit=async event=>{event.preventDefault();const submit=event.submitter;submit.disabled=true;try{const result=await api('/api/hub/reddit/items',{title:el('reddit-title').value,original:el('reddit-original').value,url:el('reddit-url').value,target_id:el('reddit-target').value});status(result.duplicate?'Этот материал уже есть в общей очереди':'Материал добавлен');if(!result.duplicate)event.target.reset();await refresh();}catch(error){status(error.message,true);}finally{submit.disabled=false;}};
el('reddit-mail-form').onsubmit=async event=>{event.preventDefault();try{await api('/api/hub/reddit/mail',{username:el('reddit-mail-user').value,app_password:el('reddit-mail-password').value,enabled:el('reddit-mail-enabled').checked});el('reddit-mail-password').value='';el('reddit-mail-template').textContent='';status('Подключение сохранено');await refresh();}catch(error){el('reddit-mail-password').value='';status(error.message,true);}};
el('reddit-template').onclick=async()=>{try{const data=await api('/api/hub/reddit/mail/template');el('reddit-mail-template').textContent=`Получатель: ${data.recipient}\nТема:\n${data.subject}\n\nТело письма:\n${data.body}`;}catch(error){status(error.message,true);}};
el('reddit-mail-poll').onclick=async()=>{try{const data=await api('/api/hub/reddit/mail/poll',{});status(`Принято материалов: ${data.accepted}`);await refresh();}catch(error){status(error.message,true);await refresh();}};
refresh().catch(()=>status('Введите локальный ключ доступа из .env.local'));
