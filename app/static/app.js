'use strict';
const $ = id => document.getElementById(id);
let media = [], year = null, album = null, editId = null, queue = [], index = 0, activeRenderer = '', playingId = null;
let fetchVersion = 0, scanWasRunning = false, mediaSignature = '';
let role = null, hiddenGroups = new Set(), renderers = [];
// Playback session: `session` changes whenever playback is (re)started or stopped, so stale async steps bail out.
let session = 0, sessionPref = '', lastSkipped = 0, lastError = '';
const localFailed = new Set();  // items this browser failed to decode at runtime
const isAdmin = () => role === 'admin', showHidden = () => isAdmin() && $('show-hidden').checked;
const groupKey = (y, a = '') => y + '\0' + a;
const enc = encodeURIComponent;
function el(tag, text, cls) { const n = document.createElement(tag); if(text !== undefined) n.textContent = text; if(cls)n.className = cls; return n; }
// ---------- Toasts: toast(text, {type: 'info'|'success'|'error', detail}) ----------
const TOAST_MS = {info: 4000, success: 4000, error: 8000}, TOAST_MAX = 4, toasts = new Map();
function dismissToast(node, now = false) {
  const t = toasts.get(node); if(!t)return; clearTimeout(t.timer); toasts.delete(node);
  if(now || matchMedia('(prefers-reduced-motion: reduce)').matches){node.remove();return;}
  node.classList.add('leaving'); setTimeout(()=>node.remove(), 200);
}
function toast(text, {type = 'info', detail = ''} = {}) {
  if(!text)return null;
  const key = type+'\n'+text+'\n'+detail, last = [...toasts.keys()].at(-1);
  if(last && last.dataset.key === key){const t=toasts.get(last);t.count++;t.badge.textContent='×'+t.count;t.badge.hidden=false;t.restart();return last;}
  const node = el('div',undefined,'toast toast-'+type), body = el('div',undefined,'toast-body'), line = el('p',undefined,'toast-text');
  const icon = el('span',undefined,'toast-icon'), badge = el('span','','toast-count'), close = el('button','×','toast-close');
  node.dataset.key = key; node.setAttribute('role', type === 'error' ? 'alert' : 'status');
  icon.setAttribute('aria-hidden','true'); badge.hidden = true; close.type = 'button'; close.setAttribute('aria-label','閉じる');
  line.append(el('span',text),badge); body.append(line);
  if(detail){const d=el('details',undefined,'toast-detail');d.append(el('summary','詳細'),el('pre',detail));body.append(d);}
  node.append(icon,body,close);
  const ms = TOAST_MS[type] || TOAST_MS.info;
  // Hover or focus inside holds the timer; the remaining time resumes afterwards.
  const t = {count:1, badge, left:ms, timer:null, since:0, hover:false, focus:false,
    sync(){ if(this.hover || this.focus){ if(this.timer){clearTimeout(this.timer);this.timer=null;this.left-=Date.now()-this.since;} }
      else if(!this.timer){this.since=Date.now();this.timer=setTimeout(()=>dismissToast(node),Math.max(this.left,1500));} },
    restart(){clearTimeout(this.timer);this.timer=null;this.left=ms;this.sync();}};
  node.onmouseenter=()=>{t.hover=true;t.sync();}; node.onmouseleave=()=>{t.hover=false;t.sync();};
  node.addEventListener('focusin',()=>{t.focus=true;t.sync();});
  node.addEventListener('focusout',e=>{if(!node.contains(e.relatedTarget)){t.focus=false;t.sync();}});
  close.onclick=()=>dismissToast(node);
  toasts.set(node,t); $('toasts').append(node); t.sync();
  while(toasts.size > TOAST_MAX)dismissToast(toasts.keys().next().value, true);
  return node;
}
function message(text, detail = '') { toast(text, {detail}); }
async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, {method, headers:{'Content-Type':'application/json','X-Requested-With':'dlna-library'},body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json().catch(()=>({detail: response.statusText || '通信エラー'}));
  if (!response.ok) { if(response.status === 401)$('login').hidden = false;
    const detail = data.detail, error = new Error(typeof detail === 'string' ? detail : typeof detail?.message === 'string' ? detail.message : JSON.stringify(detail));
    error.status = response.status; error.code = detail?.code; throw error; }
  return data;
}
// 401 opens the login card instead of a toast.
function action(fn) { return async (...args) => { try { await fn(...args); } catch(e){ if(e.status !== 401)toast(e.message, {type:'error', detail:e.detail}); } }; }
const hasKind = (d, kind) => d.kind.split(',').includes(kind);
async function devices() {
  const list = await api('/devices');
  for(const [id,kind] of [['server','server'],['renderer','renderer']]) {
    const select = $(id), previous = select.value;
    select.replaceChildren(new Option(id === 'server' ? '選択してください' : 'このブラウザ',''));
    list.filter(d=>hasKind(d,kind)).forEach(d=>select.add(new Option(d.name + (d.model ? ' · '+d.model : ''),d.id)));
    if([...select.options].some(o=>o.value===previous))select.value=previous;
  }
  renderers = list.filter(d=>hasKind(d,'renderer'));
}
// Periodic refreshes skip re-rendering when nothing changed, so hover/scroll/focus survive.
async function refresh(force = true) {
  const version = ++fetchVersion, server = $('server').value;
  const all = showHidden(), query = '?server_id='+enc(server);
  const [result, hidden] = server ? await Promise.all([api('/media'+query+(all?'&include_hidden=true':'')), all ? api('/hidden'+query) : {groups:[]}]) : [[], {groups:[]}];
  if(version !== fetchVersion)return;
  const signature = server + '\n' + JSON.stringify([result, hidden]);
  if(!force && signature === mediaSignature)return;
  mediaSignature = signature; media = result; hiddenGroups = new Set(hidden.groups.map(g=>groupKey(g.year,g.album))); render();
}
function thumb(item) {
  const cover = el('button',undefined,'cover'); cover.type='button';
  if(item?.thumbnail){const img=el('img'); img.src='/thumbs/'+enc(item.thumbnail);img.alt='';img.loading='lazy';cover.append(img);}
  else cover.append(el('span','プレビュー準備中','placeholder'));
  return cover;
}
// Admin-only eye toggle; `own` is this card's own flag (it may also be hidden via its year/album).
function hideButton(label, own, path, body, cls = '') {
  const b = el('button',undefined,'hide-button'+cls+(own?' is-hidden':'')); b.type='button';
  b.title = own ? '表示する' : '非表示にする'; b.setAttribute('aria-label', label+'を'+b.title); b.setAttribute('aria-pressed', own);
  b.onclick = action(async e => { e.stopPropagation(); await api(path, own ? 'DELETE' : 'PUT', body); await refresh(); });
  return b;
}
function hiddenBadge(cover, cls) { cover.append(el('span','非表示','badge hidden-badge '+cls)); }
function emptyState(title, text) {
  const box = el('div',undefined,'empty'); box.append(el('span',undefined,'empty-icon'),el('p',title,'empty-title'),el('p',text));
  box.firstChild.setAttribute('aria-hidden','true'); return box;
}
function render() {
  // A folder that just became hidden (or vanished) is left for the nearest visible level.
  if(year && !media.some(m=>m.year===year)){year=null;album=null;}
  if(album && !media.some(m=>m.year===year&&m.album===album))album=null;
  const nav=$('breadcrumbs');nav.replaceChildren();
  const crumb=(text,fn,cls='crumb')=>{const b=el('button',text,cls);b.type='button';b.onclick=fn;nav.append(b);return b;};
  const sep=()=>{const s=el('span','›','sep');s.setAttribute('aria-hidden','true');nav.append(s);};
  const top=crumb('Top',()=>{year=null;album=null;render();},'crumb home');
  if(!year)top.setAttribute('aria-current','page');
  if(year){sep();const y=crumb(year,()=>{album=null;render();});if(!album)y.setAttribute('aria-current','page');}
  if(album){sep();const a=el('span',album,'crumb current');a.setAttribute('aria-current','page');nav.append(a);}
  if(year){ const years=[...new Set(media.map(m=>m.year))].sort(), at=years.indexOf(year);
    for(const [label,pos] of [['← 前の年',at-1],['次の年 →',at+1]])if(years[pos])crumb(label,()=>{year=years[pos];album=null;render();},'crumb step'); }
  $('player').classList.toggle('active', !!playingId);
  const content=$('content');content.replaceChildren();
  const items=media.filter(m=>(!year||m.year===year)&&(!album||m.album===album));
  if(!items.length){content.append(emptyState('動画がありません','サーバーを選択してクロールしてください。'));return;}
  const head=el('div',undefined,'section-head');
  head.append(el('h2',album||(year?year+' のアルバム':'年から探す')),el('span',items.length+' 本','count'));
  const grid=el('div',undefined,'grid');content.append(head,grid);
  if(!year || !album){const key=!year?'year':'album';const groups=Map.groupBy ? Map.groupBy(items,m=>m[key]) : items.reduce((map,m)=>map.set(m[key],[...(map.get(m[key])||[]),m]),new Map());
    grid.classList.add(key==='year'?'grid-years':'grid-albums');
    for(const [name,group] of groups){
      const own=hiddenGroups.has(key==='year'?groupKey(name):groupKey(year,name)), dim=own||group.every(m=>m.hidden_effective);
      const card=el('article',undefined,'card group'+(group.some(m=>m.id===playingId)?' playing':'')+(dim?' is-hidden':'')), cover=thumb(group.find(m=>m.thumbnail)||group[0]);
      cover.setAttribute('aria-label',name+'を開く');cover.onclick=()=>{if(!year)year=name;else album=name;render();};
      if(dim)hiddenBadge(cover,'top');
      const body=el('div',undefined,'card-body');body.append(el('h3',name),el('p',group.length+' 本','meta'));
      card.append(cover,body);
      if(isAdmin())card.append(hideButton(name,own,'/servers/'+enc($('server').value)+'/hidden',key==='year'?{year:name}:{year,album:name},' overlay'));
      grid.append(card);}
    return;}
  for(const item of items){
    const playing=item.id===playingId, card=el('article',undefined,'card'+(playing?' playing':'')+(item.hidden_effective?' is-hidden':'')), cover=thumb(item), r=item.resources[0];
    cover.setAttribute('aria-label',item.title+'を再生');cover.onclick=action(()=>start(item.id,items));
    const duration=(r?.duration||'').split('.')[0];
    if(duration)cover.append(el('span',duration,'badge duration'));
    if(playing)cover.append(el('span','再生中','badge now-badge'));
    if(item.hidden_effective)hiddenBadge(cover,'bottom');
    const body=el('div',undefined,'card-body'), heading=el('div',undefined,'title-row');heading.append(el('h3',item.title));body.append(heading);
    if(r)body.append(el('p',[r.resolution,r.mime,duration].filter(Boolean).join(' / '),'meta'));
    if(item.custom_title)body.append(el('p',item.source_title,'meta source'));
    if(item.thumb_error)body.append(el('p','サムネイル生成失敗。再クロールで再試行します。','warn'));
    if(isAdmin()){const edit=el('button',undefined,'edit-button');edit.type='button';edit.title='タイトルを編集';edit.setAttribute('aria-label',item.title+'のタイトルを編集');
      edit.onclick=()=>{editId=item.id;$('original').textContent='元の名前: '+item.source_title;$('title').value=item.custom_title||'';$('edit').showModal();};
      heading.append(edit,hideButton(item.title,!!item.hidden,'/media/'+enc(item.id)+'/hidden'));}
    card.append(cover,body);grid.append(card);}
}
// ---------- Playback routing ----------
// The 優先再生先 select is a preference: items this browser cannot play go to a DLNA renderer, and items the
// renderer cannot play (or a failed push) come back to this browser. Every automatic reroute shows a toast.
const savedRenderer={get(){try{return localStorage.getItem('dlna.renderer')||'';}catch{return '';}},set(v){try{localStorage.setItem('dlna.renderer',v);}catch{}}};
const rendererName = id => renderers.find(r=>r.id===id)?.name || '再生先';
const urlPath = r => { try { return new URL(r.url).pathname.toLowerCase(); } catch { return ''; } };
const isMp4 = r => r.mime === 'video/mp4' || urlPath(r).endsWith('.mp4');
// Best resource this browser says it can play ('probably'/'maybe'); MP4 first, as before.
function localResource(item) {
  if(!item || localFailed.has(item.id))return null;
  const v = $('video'), types = r => [r.mime, urlPath(r).endsWith('.mp4') ? 'video/mp4' : ''].filter(Boolean);
  const ok = item.resources.filter(r=>types(r).some(t=>v.canPlayType(t)));
  return ok.find(isMp4) || ok[0] || null;
}
async function canPlay(rid, item) { return (await api('/renderers/'+enc(rid)+'/plan','POST',{ids:[item.id]}))[0].playable; }
// For items this browser cannot play. Order: the session's preferred renderer, the last one chosen explicitly, then
// the rest. Renderers that know the item is incompatible are passed over (null = did not say: try it), and a failed
// push moves on to the next one. Throws when none could take it.
async function pushFallback(item, token, first, reason) {
  const saved = savedRenderer.get(), rank = r => r.id === sessionPref ? 0 : r.id === saved ? 1 : 2, failures = [];
  for(const r of [...renderers].sort((a,b)=>rank(a)-rank(b))){
    try { if(await canPlay(r.id,item) === false)continue; } catch(e) { failures.push(r.name+': '+e.message); continue; }
    if(token !== session)return;
    try { await push(r.id, token, first); } catch(e) { if(!e.code)throw e; failures.push(e.message); continue; }
    toast(reason+'ため、'+r.name+'で再生します', {detail: failures.join('\n')});
    return;
  }
  const error = new Error(unplayableAnywhere(item)); error.detail = failures.join('\n'); throw error;
}
function setTarget(label) { $('now-target').textContent = label; $('now-target').hidden = !label; }
function stopBrowser() { const v=$('video'); v.pause(); v.removeAttribute('src'); v.load(); v.hidden=true; }
async function stop(){ session++; stopBrowser();
  try { if(activeRenderer)await api('/renderers/'+enc(activeRenderer)+'/stop','POST'); }
  finally { activeRenderer='';playingId=null;$('now').textContent='停止';setTarget('');render(); } }
async function start(id,items){ await stop().catch(()=>{});
  queue=media.filter(m=>m.year===items.find(i=>i.id===id).year);index=queue.findIndex(m=>m.id===id);sessionPref=$('renderer').value;
  await route(session, true); }
const unplayableAnywhere = item => '「'+item.title+'」はこのブラウザで再生できない形式で、再生できるDLNA機器も見つかりません。';
// Routes queue[index]. `first`: the user's own click (a renderer then gets the whole rotated year queue).
async function route(token, first = false) {
  const item = queue[index]; if(!item || token !== session)return;
  if(!sessionPref){
    if(localResource(item))return browserPlay(token);
    return pushFallback(item, token, first, item.resources.some(isMp4) ? 'このブラウザで再生できない形式の' : 'MP4がない');
  }
  const name = rendererName(sessionPref), playable = await canPlay(sessionPref, item); if(token !== session)return;
  if(playable === false)
    return toBrowser(token, name+'が対応していない形式のため、このブラウザで再生します', name+'が対応していない形式で、このブラウザでも再生できません。');
  try { await push(sessionPref, token, first); }
  catch(e) { if(!e.code || token !== session)throw e;  // push_failed / unplayable: the renderer could not start it
    await toBrowser(token, name+'で再生できなかったため、このブラウザで再生します', e.message+'（このブラウザでも再生できない形式です）'); }
}
async function toBrowser(token, info, error) { if(!localResource(queue[index]))throw new Error(error); toast(info); await browserPlay(token); }
// Hands the queue from `index` to a renderer; the server advances it from there (no hand-back to the browser).
async function push(rid, token, first) {
  const rest = queue.slice(index), ids = (first || $('repeat').checked ? rest.concat(queue.slice(0,index)) : rest).map(m=>m.id);
  stopBrowser();
  await api('/renderers/'+enc(rid)+'/queue','POST',{ids,repeat:$('repeat').checked});
  if(token !== session)return;
  activeRenderer=rid;playingId=queue[index].id;lastSkipped=0;lastError='';
  setTarget(rendererName(rid));$('now').textContent=queue[index].title;render();
}
async function browserPlay(token){const item=queue[index],r=localResource(item);if(!r||token!==session)return;
  activeRenderer='';const v=$('video');v.src=r.url;v.hidden=false;playingId=item.id;setTarget('このブラウザ');
  $('now').textContent=item.title+' ('+(index+1)+'/'+queue.length+')';render();
  try{await v.play();}catch(e){if(e.name!=='NotSupportedError'&&e.name!=='AbortError')throw e;}}  // unsupported: see onerror
// Next item of a browser session; an item nobody can play is reported and passed over.
async function advance(token){
  for(let tries=0;tries<queue.length&&token===session;tries++){
    index++;if(index>=queue.length){if(!$('repeat').checked){await stop();return;}index=0;}
    try{await route(token);return;}catch(e){if(e.status===401)throw e;toast(e.message,{type:'error',detail:e.detail});}
  }
  if(token===session)await stop();
}
$('video').onended=action(()=>advance(session));
$('video').onerror=action(async()=>{const v=$('video'),code=v.error?.code,token=session,item=queue[index];
  if(!v.getAttribute('src')||activeRenderer||!item||item.id!==playingId)return;
  if(code!==MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED&&code!==MediaError.MEDIA_ERR_DECODE)
    throw new Error('ブラウザで再生できません。MediaServerへの接続を確認してください。');
  // e.g. HEVC in an MP4 the browser cannot decode: never try it here again, send it to a renderer.
  localFailed.add(item.id);stopBrowser();
  try{await pushFallback(item,token,false,'このブラウザで再生できない形式の');}
  catch(e){if(token===session){playingId=null;setTarget('');$('now').textContent='停止';render();}throw e;}});
$('renderer').onchange=()=>{if($('renderer').value)savedRenderer.set($('renderer').value);};
// Renderer session: follow the server-side queue; the item the user picked failing to start falls back here.
async function rendererState(rid, state, token){
  const name=rendererName(rid);
  $('now').textContent=[state.title,state.state,state.position+'/'+state.duration].filter(Boolean).join(' · ');
  if((state.skipped||0)>lastSkipped)toast(name+'で再生できない動画を '+(state.skipped-lastSkipped)+' 本スキップしました');
  lastSkipped=state.skipped||0;
  if(state.media_id&&playingId!==state.media_id){playingId=state.media_id;const i=queue.findIndex(m=>m.id===playingId);if(i>=0)index=i;render();}
  if(state.state!=='ERROR'||!state.error||state.error===lastError)return;
  lastError=state.error;
  const i=queue.findIndex(m=>m.id===state.media_id);
  if(state.failure==='unplayable'&&i>=0){index=i;activeRenderer='';
    if(localResource(queue[i])){toast(name+'で再生できなかったため、このブラウザで再生します');await browserPlay(token);return;}
    playingId=null;setTarget('');$('now').textContent='停止';render();
    throw new Error(name+'で再生できず、このブラウザでも再生できない形式です。');}
  toast(state.error,{type:'error'});
}
$('discover').onclick=action(async()=>{$('discover').disabled=true;try{const result=await api('/discover','POST');await devices();
  const count=kind=>result.devices.filter(d=>hasKind(d,kind)).length;
  toast('MediaServer '+count('server')+' 台・再生先 '+count('renderer')+' 台を検出'+(result.errors.length?'（一部の機器情報を取得できませんでした）':''),
    {type:result.errors.length?'info':'success',detail:result.errors.map(String).join('\n')});
}finally{$('discover').disabled=false;}});
$('add').onsubmit=action(async e=>{e.preventDefault();await api('/devices','POST',{url:$('location').value});await devices();});
// Starting is idempotent on the server: a running crawl is joined, never duplicated or cancelled.
// Switching servers only changes what is displayed; other crawls keep running in the background.
async function startCrawl(){const server=$('server').value;const state=await api('/servers/'+enc(server)+'/crawl','POST');if(server===$('server').value){showCrawl(state);scanWasRunning=true;}return state;}
// The last selected MediaServer is remembered per browser (storage may be unavailable, e.g. private mode).
const savedServer={get(){try{return localStorage.getItem('dlna.server')||'';}catch{return '';}},set(v){try{localStorage.setItem('dlna.server',v);}catch{}}};
async function selectServer(){year=null;album=null;showCrawl(null);await refresh();if($('server').value)await startCrawl();}
$('server').onchange=action(async()=>{savedServer.set($('server').value);await selectServer();});
$('crawl').onclick=action(async()=>{if(!$('server').value)throw new Error('MediaServerを選択してください');await startCrawl();});
// Admin-only source-folder picker: browse containers, then pin one (or return to auto-detection).
let rootPath = [], rootVersion = 0;
const rootLabel = r => (r.auto ? '自動: ' : '手動: ') + r.title;
async function loadRoot(){const server=$('server').value, here=rootPath.at(-1), nav=$('root-path'), list=$('root-list'), version=++rootVersion;
  nav.replaceChildren();
  rootPath.forEach((p,i)=>{if(i){const s=el('span','›','sep');s.setAttribute('aria-hidden','true');nav.append(s);}
    const b=el('button',p.title,'crumb'+(i?'':' home'));b.type='button';if(i===rootPath.length-1)b.setAttribute('aria-current','page');
    b.onclick=()=>{rootPath=rootPath.slice(0,i+1);loadRoot();};nav.append(b);});
  $('root-choose').textContent=rootPath.length>1?'このフォルダから取得':'ルート全体から取得';
  list.replaceChildren(el('li','読み込み中…','root-empty'));list.setAttribute('aria-busy','true');
  try{const children=await api('/servers/'+enc(server)+'/containers?parent='+enc(here.id));
    if(version!==rootVersion)return;
    list.replaceChildren(...children.map(c=>{const li=el('li'),b=el('button',undefined,'root-item');b.type='button';
      b.append(el('span',c.title,'root-title'),el('span',c.child_count==null?'':c.child_count+' 件','root-count'));
      b.onclick=()=>{rootPath.push({id:c.id,title:c.title});loadRoot();};li.append(b);return li;}));
    if(!children.length)list.append(el('li','サブフォルダはありません','root-empty'));
  }catch(e){if(version===rootVersion)list.replaceChildren(el('li',e.message,'root-empty warn'));}
  finally{if(version===rootVersion)list.setAttribute('aria-busy','false');}}
async function saveRoot(body){const server=$('server').value;await api('/servers/'+enc(server)+'/crawl-root','PUT',body);$('root-dialog').close();
  const state=await startCrawl();
  if(state&&state.root&&(body.id?state.root.id!==body.id||state.root.auto:!state.root.auto))message('実行中のクロールが終わった後に「再クロール」すると、新しい取得元が使われます。');}
$('root-open').onclick=action(async()=>{const server=$('server').value;if(!server)throw new Error('MediaServerを選択してください');
  const info=await api('/servers/'+enc(server)+'/crawl-root');
  $('root-mode').textContent='現在の設定 — '+(info.id?rootLabel({auto:false,title:info.title}):info.last?.auto?rootLabel(info.last):'自動（次回のクロールで判定）');
  $('root-auto').disabled=!info.id;rootPath=[{id:'0',title:'ルート'}];$('root-dialog').showModal();await loadRoot();});
$('root-choose').onclick=action(()=>{const here=rootPath.at(-1);return saveRoot({id:here.id,title:rootPath.length>1?here.title:'ルート（すべて）'});});
$('root-auto').onclick=action(()=>saveRoot({id:null}));
$('root-cancel').onclick=()=>{rootVersion++;$('root-dialog').close();};
$('title-form').onsubmit=action(async e=>{e.preventDefault();await api('/media/'+editId+'/title','PATCH',{title:$('title').value});$('edit').close();await refresh();});
$('cancel').onclick=()=>$('edit').close();
$('stop').onclick=action(stop);
$('pause').onclick=action(async()=>{if(activeRenderer)await api('/renderers/'+enc(activeRenderer)+'/pause','POST');else $('video').pause();});
$('play').onclick=action(async()=>{if(activeRenderer)await api('/renderers/'+enc(activeRenderer)+'/play','POST');else if($('video').src)await $('video').play();});
$('login').onsubmit=async e=>{e.preventDefault();$('login-error').textContent='';
  try{await api('/login','POST',{token:$('token').value});}catch(err){$('login-error').textContent=err.message;return;}
  $('token').value='';$('login').hidden=true;await action(boot)();};
$('logout').onclick=action(async()=>{await api('/logout','POST');location.reload();});
$('show-hidden').onchange=action(()=>refresh());
async function boot(){const me=await api('/me');role=me.role;
  $('role').textContent=isAdmin()?'管理者':'ゲスト';$('role').dataset.role=role;$('account').hidden=false;$('logout').hidden=!me.auth;
  $('show-hidden-wrap').hidden=!isAdmin();$('root-open').hidden=!isAdmin();$('add').closest('details').hidden=!isAdmin();if(!isAdmin())$('show-hidden').checked=false;
  await devices();const saved=savedServer.get();
  if(saved&&!$('server').value&&[...$('server').options].some(o=>o.value===saved)){$('server').value=saved;await selectServer();}else await refresh();}
const crawlLabels={idle:'待機',scanning:'動画を取得中',thumbnails:'サムネイル生成中',complete:'クロール完了',failed:'クロール失敗',interrupted:'中断'};
const isRunning=state=>!!state&&['scanning','thumbnails'].includes(state.state);
function showCrawl(state){const status=$('crawl-status'),button=$('crawl'),running=isRunning(state);
  if(state){const parts=[crawlLabels[state.state]||state.state,state.count+' 本'];
    if(state.count)parts[1]+='（新規 '+state.new+(state.updated?'・更新 '+state.updated:'')+'・既存 '+state.skipped+'）';
    if(state.thumbs_total)parts.push('サムネイル '+state.thumbnails+'/'+state.thumbs_total);
    if(state.errors.length)parts.push('エラー '+state.errors.length+' 件');
    status.textContent=parts.join(' · ');status.title=[isAdmin()&&state.root?'取得元 '+rootLabel(state.root):'',...state.errors].filter(Boolean).join('\n');status.dataset.state=state.state;}
  else{status.textContent='';status.title='';delete status.dataset.state;}
  status.setAttribute('aria-busy',running);$('server-spinner').hidden=!running;
  button.textContent=running?'取得中…':'再クロール';button.classList.toggle('busy',running);button.setAttribute('aria-busy',running);
  button.title=running?'バックグラウンドで取得中です（押しても重複して実行されません）':'';}
async function poll(){try{
  const server=$('server').value;
  if(server){const state=await api('/servers/'+enc(server)+'/crawl');
    if(server===$('server').value){showCrawl(state);const running=isRunning(state);if(running||scanWasRunning)await refresh(false);scanWasRunning=running;}}
  else showCrawl(null);
  if(activeRenderer){const rid=activeRenderer,token=session,state=await api('/renderers/'+enc(rid)+'/state');
    if(rid===activeRenderer&&token===session)await rendererState(rid,state,token);}
  }catch(e){if(e.status!==401)toast(e.message,{type:'error'});}finally{setTimeout(poll,2500);}}
// Keep page content clear of the fixed player dock, whose height changes when the video is shown.
if(window.ResizeObserver)new ResizeObserver(()=>document.documentElement.style.setProperty('--dock-h',$('player').offsetHeight+'px')).observe($('player'),{box:'border-box'});
action(boot)();poll();
