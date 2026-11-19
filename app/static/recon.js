const ctxEl=document.getElementById('recon-context');
const ctx=JSON.parse(ctxEl.textContent);
const msg=document.getElementById('recon-message');
function setMsg(text,bad){msg.textContent=text||'';msg.className=bad?'error':'ok';}
async function postJSON(url,body){
  const resp=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await resp.json().catch(()=>({}));
  if(resp.status>=400)throw Error(data.message||('请求失败 '+resp.status));
  return data;
}
document.getElementById('load-demo').addEventListener('click',async()=>{
  try{setMsg('正在装载/重跑演示批次…');const r=await postJSON('/api/recon/demo',{});
    location.href='/recon?batch_id='+encodeURIComponent(r.batch_id);
  }catch(e){setMsg(e.message,true);}
});
document.getElementById('submit-events').addEventListener('click',async()=>{
  let events;
  try{events=JSON.parse(document.getElementById('event-input').value);}
  catch(e){setMsg('JSON 解析失败：'+e.message,true);return;}
  if(!Array.isArray(events)||!events.length){setMsg('events 必须是非空数组',true);return;}
  try{
    setMsg('正在提交事件批次（失败将保留上一版结论）…');
    const r=await postJSON('/api/recon/events',{batch_id:ctx.batch_id,events});
    setMsg(r.reused?'同批重跑：已复用，未新增加扣减，版本 '+r.version
                   :'已生成新版本 '+r.version+'，采信 '+r.accepted+' 条事件');
    setTimeout(()=>location.reload(),700);
  }catch(e){setMsg('整批回滚，上一版对账结论保留：'+e.message,true);}
});
document.getElementById('show-ledger').addEventListener('click',async()=>{
  const box=document.getElementById('ledger-box');
  try{
    const resp=await fetch('/api/recon/batches/'+encodeURIComponent(ctx.batch_id)+'/ledger');
    const data=await resp.json();
    if(resp.status>=404)throw Error(data.message||'批次不存在');
    document.getElementById('ledger-meta').textContent='事件 '+data.events.length+' 条 · 版本 '+data.versions.length;
    box.innerHTML=data.events.map(e=>
      '<div class="ledger-event"><b>#'+e.seq+' '+e.type+'</b> · '+e.order_no+
      ' · '+e.occurred_at+'<br><span>'+e.payload+'</span><br><small>event_id: '+e.event_id+'</small></div>'
    ).join('')||'<p class="empty">暂无事件。</p>';
  }catch(e){box.innerHTML='<p class="empty">'+e.message+'</p>';}
});
