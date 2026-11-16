import {channels} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {MasterStatus,EntryResolution,ResolutionBasis,OpType,OpStatus} from '../constants/enums.js';

const canWrite=()=>session.user.role!=='viewer';
const isAdmin=()=>session.user.role==='admin';
const idem=()=>(crypto.randomUUID?crypto.randomUUID():'id-'+Date.now()+'-'+Math.random().toString(16).slice(2));

export async function channelPage(){
 workspace.channelView=await channels.masters();
 workspace.channelPending=await channels.pending();
 workspace.channelOps=await channels.operations();
 const v=workspace.channelView,pending=workspace.channelPending,ops=workspace.channelOps;
 return heading('06 / CHANNEL REGISTRY','渠道主档与别名治理',
   '改名只新增按账日生效的别名，不再拆散历史收入。接账保留原名，歧义账待认领；合并同日账逐笔选用、保留弃用依据，未裁决账不供分析。',
   '<button class="button" id="refresh-channels">刷新注册 ↻</button>')
 +'<div class="channel-meta panel"><div class="section-title"><h2>渠道注册版本</h2><span>接账与合并裁决共用同一有效版本</span></div>'
 +'<div class="version-row"><strong>v'+esc(fmt(v.registry_version))+'</strong><span>'+v.masters.length+' 个主档 · '+pending.length+' 笔待认领 · '+ops.length+' 条操作记录</span></div></div>'

 +'<section class="panel"><div class="section-title"><h2>歧义待认领</h2><span>'+pending.length+' 笔未裁决，不供分析</span></div>'
 +(pending.length?'<div class="table-wrap"><table><thead><tr><th>账日</th><th>源账原名</th><th>批次/行号</th><th>状态</th><th>认领操作</th></tr></thead><tbody>'
   +pending.map(e=>'<tr><td>'+esc(e.day)+'</td><td><strong>'+esc(e.original_name)+'</strong></td><td>#'+e.dataset_id+' / 行'+(e.row_index+1)+'</td><td><span class="tag tag-pending">'+esc(EntryResolution[e.resolution_status]||e.resolution_status)+'</span></td>'
   +'<td><form class="claim-form" data-entry="'+e.id+'"><select name="master_id" required>'+v.masters.filter(m=>m.status==='active').map(m=>'<option value="'+m.id+'">'+esc(m.canonical_name)+'</option>').join('')+'</select><button class="small primary" '+(canWrite()?'':'disabled')+'>认领到此主档</button><button type="button" class="small text-button claim-new" data-entry="'+e.id+'">新建主档</button></form></td></tr>').join('')
   +'</tbody></table></div>'
 :'<div class="empty">没有歧义账。所有源账均已归属到有效主档，可正常进入分析。</div>')
 +'</section>'

 +'<section class="panel"><div class="section-title"><h2>渠道主档</h2><span>合并不直接相加，逐笔裁决</span></div>'
 +'<div class="master-grid">'+v.masters.map(m=>'<article class="master-card '+(m.status==='deprecated'?'deprecated':'')+'">'
   +'<div class="master-head"><strong>'+esc(m.canonical_name)+'</strong><span class="tag '+(m.status==='active'?'tag-active':'tag-deprecated')+'">'+esc(MasterStatus[m.status]||m.status)+'</span></div>'
   +'<p class="hint">主档 #'+m.id+' · 账 '+m.entry_count+' 笔 · 待认领 '+m.pending_count+' 笔</p>'
   +(m.deprecated_reason?'<p class="warning">弃用依据：'+esc(m.deprecated_reason)+(m.merged_into_id?' → 并入 #'+m.merged_into_id:'')+'</p>':'')
   +'<div class="master-actions"><button class="small" data-detail="'+m.id+'">别名与逐笔账</button>'
   +(isAdmin()&&m.status==='active'?'<button class="small primary" data-merge="'+m.id+'">发起合并</button>':'')
   +'</div></article>').join('')+'</div></section>'

 +'<section class="panel"><div class="section-title"><h2>登记按账日生效别名</h2><span>默认拦截同账日歧义</span></div>'
 +'<form id="alias-form" class="inline-form"><label>主档<select name="master_id">'+v.masters.filter(m=>m.status==='active').map(m=>'<option value="'+m.id+'">'+esc(m.canonical_name)+'</option>').join('')+'</select></label>'
 +'<label>别名（源账曾用名）<input name="alias_name" required minlength="1" placeholder="例如：星河Pro"></label>'
 +'<label>生效账日<input name="effective_from" type="date" required></label>'
 +'<label>失效账日（可选）<input name="effective_to" type="date"></label>'
 +'<label class="check"><input type="checkbox" name="allow_ambiguous"> 我确认该名称在此账日与其他主档并存（新账进入待认领）</label>'
 +'<button class="primary" '+(canWrite()?'':'disabled')+'>登记别名</button></form></section>'

 +'<section class="panel"><div class="section-title"><h2>追查原名</h2><span>名称 → 别名区间 → 逐笔归属链</span></div>'
 +'<form id="trace-form" class="inline-form"><label>源账原名<input name="name" required placeholder="输入接账时的原始渠道名"></label><button>追查</button></form>'
 +'<div id="trace-result"></div></section>'

 +'<section class="panel"><div class="section-title"><h2>合并 / 撤销操作记录</h2><span>幂等可恢复，重试不重复生成</span></div>'
 +'<div class="table-wrap"><table><thead><tr><th>#</th><th>类型</th><th>状态</th><th>版本</th><th>操作人</th><th>时间</th><th>说明 / 撤销</th></tr></thead><tbody>'
 +ops.map(o=>'<tr><td>'+o.id+'</td><td>'+esc(OpType[o.op_type]||o.op_type)+'</td><td><span class="tag tag-'+esc(o.status)+'">'+esc(OpStatus[o.status]||o.status)+'</span></td><td>v'+o.registry_version+'</td><td>'+esc(o.actor)+'</td><td>'+date(o.created_at)+'</td><td>'+mergeCell(o)+'</td></tr>').join('')
 +'</tbody></table></div></section>'

 +'<div id="modal-root"></div>';
}

function mergeCell(o){
 if(o.op_type==='merge'&&o.status==='completed'){
  return esc('来源 #'+o.payload.source_id+' → 目标 #'+o.payload.target_id+'，并入 '+(o.payload.moved_entry_ids||[]).length+' 笔、保留 '+(o.payload.kept_entry_ids||[]).length+' 笔')
   +(isAdmin()?' <button class="small text-button" data-undo="'+o.id+'">撤销此合并</button>':'');
 }
 if(o.op_type==='merge'&&o.status==='undone')return '已按来源还原';
 if(o.op_type==='undo')return '还原 '+(o.payload.restored_entry_ids||[]).length+' 笔';
 return esc(o.error||'');
}

export function bindChannels(refresh){
 document.querySelector('#refresh-channels').onclick=refresh;
 document.querySelectorAll('.claim-form').forEach(f=>f.addEventListener('submit',async e=>{
  e.preventDefault();const fd=new FormData(f),entryId=f.dataset.entry;
  try{await channels.claim(entryId,{master_id:Number(fd.get('master_id'))});notify('已认领，歧义账完成归属');await refresh();}catch(err){notify(err.message,true);}
 }));
 document.querySelectorAll('.claim-new').forEach(b=>b.onclick=async()=>{
  const name=prompt('新主档规范名称（渠道稳定身份）');if(!name)return;
  try{await channels.claim(b.dataset.entry,{create_master_name:name});notify('已新建主档并完成认领');await refresh();}catch(err){notify(err.message,true);}
 });
 document.querySelectorAll('[data-detail]').forEach(b=>b.onclick=()=>openDetail(b.dataset.detail,refresh));
 document.querySelectorAll('[data-merge]').forEach(b=>b.onclick=()=>openMerge(b.dataset.merge,refresh));
 document.querySelectorAll('[data-undo]').forEach(b=>b.onclick=()=>openUndo(b.dataset.undo,refresh));
 submit(document.querySelector('#alias-form'),async f=>{
  await channels.addAlias({master_id:Number(f.get('master_id')),alias_name:f.get('alias_name'),effective_from:f.get('effective_from'),effective_to:f.get('effective_to')||null,allow_ambiguous:f.has('allow_ambiguous')&&f.get('allow_ambiguous')==='on'});
  notify('别名已登记，注册版本已递增');await refresh();
 });
 submit(document.querySelector('#trace-form'),async f=>{
  try{const r=await channels.trace(f.get('name'));renderTrace(r);}catch(err){notify(err.message,true);}
 });
}

function renderTrace(r){
 const box=document.querySelector('#trace-result');
 box.innerHTML='<h3>'+esc(r.name)+'</h3><div class="two-columns"><div><p class="eyebrow">别名生效区间（'+r.aliases.length+'）</p>'
  +(r.aliases.length?'<div class="table-wrap"><table><thead><tr><th>主档</th><th>生效起</th><th>生效止</th><th>来源</th></tr></thead><tbody>'
   +r.aliases.map(a=>'<tr><td>#'+a.master_id+' '+esc(a.master_name||'')+'</td><td>'+esc(a.effective_from)+'</td><td>'+esc(a.effective_to||'至今')+'</td><td>'+esc(a.origin)+'</td></tr>').join('')+'</tbody></table></div>':'<p class="empty">无别名</p>')
  +'</div><div><p class="eyebrow">逐笔归属（'+r.entries.length+'）</p>'
  +(r.entries.length?'<div class="table-wrap"><table><thead><tr><th>账日</th><th>原名</th><th>归属主档</th><th>依据</th><th>版本</th></tr></thead><tbody>'
   +r.entries.map(e=>'<tr><td>'+esc(e.day)+'</td><td>'+esc(e.original_name)+'</td><td>#'+esc(e.master_id??'')+' '+esc(e.master_name||'待认领')+'</td><td>'+esc(ResolutionBasis[e.resolution_basis]||EntryResolution[e.resolution_status]||e.resolution_status)+'</td><td>v'+e.registry_version+'</td></tr>').join('')
   +'</tbody></table></div>':'<p class="empty">无逐笔账</p>')+'</div></div>';
}

async function openDetail(masterId,refresh){
 const d=await channels.master(masterId);
 const root=document.querySelector('#modal-root');
 root.innerHTML=modal('<h2>'+esc(d.canonical_name)+' <small>#'+d.id+' · '+esc(MasterStatus[d.status])+'</small></h2>'
  +(d.deprecated_reason?'<p class="warning">'+esc(d.deprecated_reason)+'</p>':'')
  +'<p class="eyebrow">按账日生效别名（'+d.aliases.length+'）</p><div class="table-wrap"><table><thead><tr><th>别名</th><th>生效起</th><th>生效止</th><th>来源</th></tr></thead><tbody>'
  +d.aliases.map(a=>'<tr><td>'+esc(a.alias_name)+'</td><td>'+esc(a.effective_from)+'</td><td>'+esc(a.effective_to||'至今')+'</td><td>'+esc(a.origin)+'</td></tr>').join('')+'</tbody></table></div>'
  +'<p class="eyebrow">逐笔源账（'+d.counts.total+'，待认领 '+d.counts.pending+'）</p><div class="table-wrap scroll400"><table><thead><tr><th>账日</th><th>原名</th><th>状态</th><th>依据</th><th>版本</th></tr></thead><tbody>'
  +d.entries.map(e=>'<tr><td>'+esc(e.day)+'</td><td>'+esc(e.original_name)+'</td><td>'+esc(EntryResolution[e.resolution_status]||e.resolution_status)+'</td><td>'+esc(ResolutionBasis[e.resolution_basis]||'')+'</td><td>v'+e.registry_version+'</td></tr>').join('')+'</tbody></table></div>');
 bindModalClose(root,refresh);
}

async function openMerge(sourceId,refresh){
 const all=workspace.channelView.masters;
 const source=all.find(m=>m.id===sourceId);
 const detail=await channels.master(sourceId);
 const resolvable=detail.entries.filter(e=>e.resolution_status!=='pending');
 if(detail.counts.pending){notify('该主档有待认领账，认领完成后才能合并',true);return;}
 const root=document.querySelector('#modal-root');
 root.innerHTML=modal('<h2>逐笔裁决合并</h2><p class="warning">金额不会自动相加。同一账日的每一笔都要明确选用：并入目标主档，或保留在原主档。</p>'
  +'<form id="merge-form"><label>来源主档（将被裁决）<input value="'+esc(source.canonical_name)+' #'+source.id+'" disabled></label>'
  +'<label>并入目标主档<select name="target_id">'+all.filter(m=>m.id!==sourceId&&m.status==='active').map(m=>'<option value="'+m.id+'">'+esc(m.canonical_name)+' #'+m.id+'</option>').join('')+'</select></label>'
  +'<p class="eyebrow">逐笔选用（'+resolvable.length+' 笔）</p><div class="table-wrap scroll400"><table><thead><tr><th>账日</th><th>原名</th><th>当前依据</th><th>裁决</th></tr></thead><tbody>'
  +resolvable.map(e=>'<tr><td>'+esc(e.day)+'</td><td>'+esc(e.original_name)+'</td><td>'+esc(ResolutionBasis[e.resolution_basis]||EntryResolution[e.resolution_status])+'</td>'
   +'<td><select name="d-'+e.entry_id+'"><option value="target">并入目标主档</option><option value="source">保留在原主档</option></select></td></tr>').join('')
  +'</tbody></table></div><button class="primary">执行合并（幂等键自动生成）</button></form>');
 bindModalClose(root,refresh);
 submit(document.querySelector('#merge-form'),async f=>{
  const decisions={};resolvable.forEach(e=>decisions[e.entry_id]=f.get('d-'+e.entry_id));
  try{const op=await channels.merge({source_id:sourceId,target_id:Number(f.get('target_id')),decisions,idempotency_key:idem()});
   notify('合并完成，注册版本 v'+op.registry_version);closeModal(root);await refresh();}
  catch(err){notify(err.message,true);}
 });
}

async function openUndo(mergeOpId,refresh){
 const op=workspace.channelOps.find(o=>o.id===mergeOpId);
 if(!confirm('撤销合并 #'+mergeOpId+'？系统将按来源逐笔还原；若合并后存在无法确定原属的新账，撤销会被阻止并列出待认领项。'))return;
 try{
  const r=await channels.undo({merge_operation_id:mergeOpId,idempotency_key:idem()});
  notify('撤销完成，已还原 '+((r.payload.restored_entry_ids||[]).length)+' 笔，版本 v'+r.registry_version);await refresh();
 }catch(err){
  // 阻断时后端返回结构化 detail（含待认领清单），api 客户端把对象 detail 字符串化
  const match=err.message.match(/\{[\s\S]*\}$/);
  if(match){try{const d=JSON.parse(match[0]);if(d&&d.pending_claim){notify('撤销被阻止：'+d.pending_claim.length+' 笔新增账无法确定原属，请到待认领区处理',true);await refresh();return;}}catch(_){}}
  notify(err.message,true);
 }
}

function modal(inner){return '<div class="modal-backdrop"><div class="modal"><button class="modal-close" type="button">×</button>'+inner+'</div></div>';}
function closeModal(root){root.innerHTML='';}
function bindModalClose(root,refresh){
 root.querySelector('.modal-backdrop').addEventListener('click',e=>{if(e.target.classList.contains('modal-backdrop'))closeModal(root);});
 root.querySelector('.modal-close').addEventListener('click',()=>closeModal(root));
}
