'use strict';
const $ = id => document.getElementById(id);
let media = [], year = null, album = null, editId = null, queue = [], index = 0, activeRenderer = '', playingId = null;
let fetchVersion = 0, scanWasRunning = false;
const enc = encodeURIComponent;
function el(tag, text, cls) { const n = document.createElement(tag); if(text !== undefined) n.textContent = text; if(cls)n.className = cls; return n; }
function message(text) { $('message').textContent = text; }
async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, {method, headers:{'Content-Type':'application/json','X-Requested-With':'dlna-library'},body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { if(response.status === 401)$('login').hidden = false; throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)); }
  return data;
}
function action(fn) { return async (...args) => { try { message(''); await fn(...args); } catch(e){message(e.message);} }; }
async function devices() {
  const list = await api('/devices');
  for(const [id,kind] of [['server','server'],['renderer','renderer']]) {
    const select = $(id), previous = select.value;
    select.replaceChildren(new Option(id === 'server' ? '選択してください' : 'ブラウザ（MP4）',''));
    list.filter(d=>d.kind.split(',').includes(kind)).forEach(d=>select.add(new Option(d.name + (d.model ? ' · '+d.model : ''),d.id)));
    if([...select.options].some(o=>o.value===previous))select.value=previous;
  }
}
async function refresh() {
  const version = ++fetchVersion, server = $('server').value;
  const result = server ? await api('/media?server_id='+enc(server)) : [];
  if(version !== fetchVersion)return;
  media = result; render();
}
function thumb(item) { const cover = el('button',undefined,'cover'); cover.type='button'; if(item?.thumbnail){const img=el('img'); img.src='/thumbs/'+enc(item.thumbnail);img.alt=item.title;img.loading='lazy';cover.append(img);}else cover.append(el('span','プレビュー準備中'));return cover; }
function render() {
  const nav=$('breadcrumbs');nav.replaceChildren();
  const crumb=(text,fn)=>{const b=el('button',text);b.onclick=fn;nav.append(b);};
  crumb('Top',()=>{year=null;album=null;render();});
  if(year){nav.append(el('span','›'));crumb(year,()=>{album=null;render();});}
  if(album){nav.append(el('span','›'),el('span',album));}
  if(year){ const years=[...new Set(media.map(m=>m.year))].sort(), at=years.indexOf(year);
    for(const [label,pos] of [['← 前の年',at-1],['次の年 →',at+1]])if(years[pos])crumb(label,()=>{year=years[pos];album=null;render();}); }
  const content=$('content');content.replaceChildren();
  const items=media.filter(m=>(!year||m.year===year)&&(!album||m.album===album));
  if(!items.length){content.append(el('p','動画がありません。サーバーを選択してクロールしてください。','empty'));return;}
  const grid=el('div',undefined,'grid');content.append(grid);
  if(!year || !album){const key=!year?'year':'album';const groups=Map.groupBy ? Map.groupBy(items,m=>m[key]) : items.reduce((map,m)=>map.set(m[key],[...(map.get(m[key])||[]),m]),new Map());
    for(const [name,group] of groups){const card=el('article',undefined,'card'), cover=thumb(group.find(m=>m.thumbnail)||group[0]);cover.setAttribute('aria-label',name+'を開く');cover.onclick=()=>{if(!year)year=name;else album=name;render();};const body=el('div',undefined,'card-body');body.append(el('h2',name),el('p',group.length+' 本'));card.append(cover,body);grid.append(card);}return;}
  for(const item of items){const card=el('article',undefined,'card'+(item.id===playingId?' playing':'')), cover=thumb(item);cover.setAttribute('aria-label',item.title+'を再生');cover.onclick=action(()=>start(item.id,items));const body=el('div',undefined,'card-body');body.append(el('h2',item.title));const r=item.resources[0];body.append(el('p',[r.resolution,r.mime,r.duration.split('.')[0]].filter(Boolean).join(' / ')));
    if(item.custom_title)body.append(el('p',item.source_title));
    if(item.thumb_error)body.append(el('p','サムネイル生成失敗。再クロールで再試行します。'));
    const edit=el('button','タイトル編集','edit-button');edit.onclick=()=>{editId=item.id;$('original').textContent='元の名前: '+item.source_title;$('title').value=item.custom_title||'';$('edit').showModal();};body.append(edit);card.append(cover,body);grid.append(card);}
}
async function stop(){ $('video').pause();$('video').hidden=true; if(activeRenderer)await api('/renderers/'+enc(activeRenderer)+'/stop','POST');activeRenderer='';playingId=null;$('now').textContent='停止';render(); }
async function start(id,items){await stop();items=media.filter(m=>m.year===items.find(i=>i.id===id).year);queue=items;index=items.findIndex(m=>m.id===id);activeRenderer=$('renderer').value;
  if(activeRenderer){const ids=items.slice(index).concat(items.slice(0,index)).map(m=>m.id);await api('/renderers/'+enc(activeRenderer)+'/queue','POST',{ids,repeat:$('repeat').checked});playingId=id;render();}
  else await browserPlay();
}
async function browserPlay(){const item=queue[index];if(!item)return;const r=item.resources.find(r=>r.mime==='video/mp4'||new URL(r.url).pathname.toLowerCase().endsWith('.mp4'));
  if(!r)throw new Error('この動画にはMP4リソースがありません。再生先にDLNA機器を選んでください。');
  const v=$('video');v.src=r.url;v.hidden=false;playingId=item.id;$('now').textContent=item.title+' ('+(index+1)+'/'+queue.length+')';render();await v.play();}
$('video').onended=action(async()=>{index++;if(index>=queue.length){if(!$('repeat').checked){await stop();return;}index=0;}await browserPlay();});
$('video').onerror=()=>message('ブラウザで再生できません。動画形式・MediaServerへの接続を確認するか、DLNA機器を選んでください。');
$('discover').onclick=action(async()=>{$('discover').disabled=true;try{const result=await api('/discover','POST');await devices();message(result.devices.length+' 台を検出'+(result.errors.length?'（一部の機器情報を取得できませんでした）':''));}finally{$('discover').disabled=false;}});
$('add').onsubmit=action(async e=>{e.preventDefault();await api('/devices','POST',{url:$('location').value});await devices();});
$('server').onchange=action(async()=>{year=null;album=null;await refresh();if($('server').value){await api('/servers/'+enc($('server').value)+'/crawl','POST');scanWasRunning=true;}});
$('crawl').onclick=action(async()=>{if(!$('server').value)throw new Error('MediaServerを選択してください');await api('/servers/'+enc($('server').value)+'/crawl','POST');scanWasRunning=true;});
$('title-form').onsubmit=action(async e=>{e.preventDefault();await api('/media/'+editId+'/title','PATCH',{title:$('title').value});$('edit').close();await refresh();});
$('cancel').onclick=()=>$('edit').close();
$('stop').onclick=action(stop);
$('pause').onclick=action(async()=>{if(activeRenderer)await api('/renderers/'+enc(activeRenderer)+'/pause','POST');else $('video').pause();});
$('play').onclick=action(async()=>{if(activeRenderer)await api('/renderers/'+enc(activeRenderer)+'/play','POST');else if($('video').src)await $('video').play();});
$('login').onsubmit=action(async e=>{e.preventDefault();await api('/login','POST',{token:$('token').value});$('token').value='';$('login').hidden=true;await devices();});
async function poll(){try{if($('server').value){const state=await api('/servers/'+enc($('server').value)+'/crawl');$('crawl-status').textContent=({idle:'待機',scanning:'動画を取得中',thumbnails:'サムネイル生成中',complete:'クロール完了',failed:'クロール失敗',interrupted:'中断'})[state.state]+' · '+state.count+' 本 / サムネイル '+state.thumbnails+' 件'+(state.errors.length?' · エラー '+state.errors.length+' 件':'');$('crawl-status').title=state.errors.join('\n');
    const running=['scanning','thumbnails'].includes(state.state);if(running||scanWasRunning)await refresh();scanWasRunning=running;}
  if(activeRenderer){const state=await api('/renderers/'+enc(activeRenderer)+'/state');$('now').textContent=[state.title,state.state,state.position+'/'+state.duration,state.error].filter(Boolean).join(' · ');if(playingId!==state.media_id){playingId=state.media_id;render();}}
  }catch(e){message(e.message);}finally{setTimeout(poll,2500);}}
action(devices)();poll();
