"""Small dependency-free browser dashboard over the operator API."""

from __future__ import annotations

import html
import json

LOGIN_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Dark Factory sign in</title>
<style>body{font:16px system-ui;background:#0d1014;color:#e7edf5;max-width:32rem;margin:12vh auto;padding:2rem}input,button{font:inherit;padding:.7rem;margin:.3rem 0;width:100%;box-sizing:border-box}button{background:#6ea8fe;border:0;border-radius:.3rem}#error{color:#ff8e8e}</style>
<h1>Dark Factory</h1><p>Enter the operator credential for this controller.</p>
<form id="login"><input id="token" type="password" autocomplete="current-password" required><input id="actor" value="operator" required><button>Open dashboard</button></form><p id="error"></p>
<script>login.onsubmit=async(e)=>{e.preventDefault();let r=await fetch('/operator/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token.value,actor:actor.value})});let j=await r.json();if(j.ok)location='/operator/dashboard';else error.textContent=j.error||'Sign in failed'};</script></html>"""


def dashboard_html(csrf: str, actor: str) -> str:
    """Render a shell; data and mutations still use revision-aware JSON commands."""
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Dark Factory tasks</title>
<style>
:root{{--bg:#0d1014;--panel:#151a21;--line:#28313d;--fg:#e7edf5;--muted:#93a1b2;--blue:#6ea8fe;--mint:#66d9a8;--amber:#f5c26b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui}}header{{padding:1rem 1.4rem;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}}main{{display:grid;grid-template-columns:minmax(16rem,28rem) 1fr;min-height:calc(100vh - 3.7rem)}}aside,section{{padding:1rem;overflow:auto}}aside{{border-right:1px solid var(--line)}}button,input,select{{font:inherit;color:var(--fg);background:var(--panel);border:1px solid var(--line);border-radius:.3rem;padding:.55rem}}button{{cursor:pointer}}button:hover{{border-color:var(--blue)}}.root,.task{{display:block;width:100%;text-align:left;margin:.45rem 0;padding:.8rem;background:var(--panel);border:1px solid var(--line);border-radius:.45rem}}.root[aria-current=true]{{border-color:var(--blue)}}.muted{{color:var(--muted)}}.now{{color:var(--mint)}}.next{{color:var(--blue)}}.human{{color:var(--amber)}}.row{{display:flex;gap:.5rem;align-items:center}}.grow{{flex:1}}.task.done{{opacity:.65}}.task.waiting{{border-left:3px solid var(--amber)}}#new-task{{display:grid;grid-template-columns:1fr auto auto;gap:.5rem;margin:1rem 0}}@media(max-width:720px){{main{{display:block}}aside{{border:0;border-bottom:1px solid var(--line)}}}}
</style>
<header><strong>dark-factory · task supervisor</strong><span>{html.escape(actor)}</span></header>
<main><aside><div id="roots"></div></aside><section><div id="detail" class="muted">Select work.</div></section></main>
<script>
const csrf={json.dumps(csrf)};let snapshot=null,selected=null;
async function view(id){{let r=await fetch('/operator/view'+(id?'?work_item_id='+encodeURIComponent(id):''));if(r.status===401){{location='/operator/login';return}}let j=await r.json();snapshot=j.result;render();}}
async function command(cmd,p){{let r=await fetch('/operator',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},body:JSON.stringify({{cmd,...p}})}});let j=await r.json();if(!j.ok)throw Error(j.error);await view(selected);return j.result}}
function el(tag,text,cls){{let n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n}}
function render(){{let roots=document.querySelector('#roots');roots.replaceChildren();for(let item of snapshot.items){{let b=el('button',item.title,'root');b.setAttribute('aria-current',item.id===selected);b.onclick=()=>{{selected=item.id;view(selected)}};let c=item.task_counts;b.append(el('div',`${{c.complete}}/${{c.total}} tasks · ${{item.stage}}`,'muted'));roots.append(b)}}let item=snapshot.items.find(x=>x.id===selected);if(item)renderDetail(item)}}
function renderDetail(item){{let d=document.querySelector('#detail');d.replaceChildren();d.append(el('h1',item.title));let counts=item.task_counts;d.append(el('p',`${{counts.complete}} of ${{counts.total}} tasks complete · revision ${{item.task_revision}}`,'muted'));for(let a of item.current_actions)d.append(el('p','Now · '+a.title,'now'));for(let a of item.next_actions)d.append(el('p','Next · '+a.title,a.owner_kind==='human'?'human':'next'));
let form=el('form');form.id='new-task';let input=el('input');input.placeholder='Add a task';let owner=el('select');for(let x of [['agent','Agent'],['human','Me']]){{let o=el('option',x[1]);o.value=x[0];owner.append(o)}}let add=el('button','Add');form.append(input,owner,add);form.onsubmit=async e=>{{e.preventDefault();try{{await command('task-add',{{work_item_id:item.id,title:input.value,owner_kind:owner.value,expected_revision:item.task_revision,idempotency_key:crypto.randomUUID()}})}}catch(x){{alert(x.message)}}}};d.append(form);
let tools=el('div',undefined,'row');for(let [label,cmd] of [['Export tasks.md','task-export'],['Import tasks.md','task-import']]){{let b=el('button',label);b.onclick=async()=>{{try{{let p={{work_item_id:item.id}};if(cmd==='task-import')p.expected_revision=item.task_revision;let r=await command(cmd,p);alert(r.pending_path?'Human edits preserved at '+r.pending_path:r.path||'Imported')}}catch(x){{alert(x.message)}}}};tools.append(b)}}d.append(tools);
for(let task of item.tasks){{let row=el('div',undefined,'task '+task.state);let line=el('div',undefined,'row');let box=document.createElement('input');box.type='checkbox';box.checked=task.state==='done';box.disabled=box.checked||task.owner_kind==='controller'||!!task.job_id;box.onchange=async()=>{{let evidence='';if(task.manual_gate)evidence=prompt('Evidence for the exact candidate')||'';try{{await command('task-complete',{{work_item_id:item.id,task_id:task.id,expected_revision:item.task_revision,evidence}})}}catch(x){{box.checked=false;alert(x.message)}}}};line.append(box,el('strong',task.title,'grow'),el('span',task.owner_kind,'muted'));row.append(line);let meta=[task.state,task.verification,task.delivery_state,task.waiting_reason].filter(Boolean).join(' · ');row.append(el('div',meta,'muted'));d.append(row)}}}}
view();setInterval(()=>view(selected),3000);
</script></html>"""
