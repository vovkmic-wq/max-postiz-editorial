"""One-time source UI upgrade; no runtime credentials are read."""
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "src/max_postiz_agent/hub_static/app.js"
text = path.read_text(encoding="utf-8")
old = "const area=node('textarea');area.value=v.body;c.append(area);button(c,'Сохранить новую версию',()=>api(`/api/hub/variants/${v.id}/revise`,{revision:v.revision,body:area.value}));"
new = """const title=node('input');title.value=v.title;title.setAttribute('aria-label','Заголовок');c.append(title);
const area=node('textarea');area.value=v.body;area.setAttribute('aria-label','Текст публикации');c.append(area);
let meta={...v.metadata_json},media=[...v.media];const platform=state.targets.find(t=>t.id===v.target_id)?.platform;
const tags=node('input'),community=node('input');if(platform==='pikabu'){tags.value=(meta.tags||[]).join(', ');tags.setAttribute('aria-label','Теги через запятую');community.value=meta.community||'';community.placeholder='Сообщество (необязательно)';c.append(tags,community);}
const files=node('p','Вложения: '+media.length);const upload=node('input');upload.type='file';upload.accept='image/png,image/jpeg,image/webp,video/mp4';upload.setAttribute('aria-label','Добавить вложение');c.append(files,upload);
upload.onchange=async()=>{if(!upload.files[0])return;try{const form=new FormData();form.append('file',upload.files[0]);const r=await fetch('/api/hub/media',{method:'POST',body:form,credentials:'same-origin'});const item=await r.json();if(!r.ok)throw Error(item.code||'Файл не принят');media.push(item);files.textContent='Вложения: '+media.length;status('Вложение добавлено. Сохраните новую версию.');}catch(e){status(e.message,true);}};
const clear=node('button','Убрать вложения');clear.onclick=()=>{media=[];files.textContent='Вложения: 0';};c.append(clear);
button(c,'Сохранить новую версию',()=>api(`/api/hub/variants/${v.id}/revise`,{revision:v.revision,body:area.value,title:title.value,media,...(platform==='pikabu'?{metadata:{tags:tags.value.split(',').map(t=>t.trim()).filter(Boolean),community:community.value}}:{})}));"""
if old in text:
    text = text.replace(old, new)
    text = text.replace("if(area.value!==v.body)", "if(area.value!==v.body||title.value!==v.title||JSON.stringify(media)!==JSON.stringify(v.media)||(platform==='pikabu'&&(tags.value!==(meta.tags||[]).join(', ')||community.value!==(meta.community||''))))")
    text = text.replace("date.type='datetime-local';", "date.type='datetime-local';date.setAttribute('aria-label','Дата и время в часовом поясе компьютера');")
    path.write_text(text, encoding="utf-8")
