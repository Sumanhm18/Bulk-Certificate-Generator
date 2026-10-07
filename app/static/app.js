'use strict';
const $ = (id) => document.getElementById(id);
let jobId = null, offset = 0, timer = null, revision = 0, loading = false;
const limit = 25;
const terminal = new Set(['completed', 'partial_failure', 'failed']);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const today = new Date();
$('issued-on').value = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
function notice(message, error = false) { $('notice').textContent = message; $('notice').className = error ? 'error' : ''; $('notice').hidden = false; }
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(e => `${e.loc.join('.')}: ${e.msg}`).join('; ') : data.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}
function parseRecipients() {
  let data;
  try { data = JSON.parse($('recipients').value); } catch { throw new Error('Recipients must be valid JSON. Use sample to see the format.'); }
  if (!Array.isArray(data) || data.length < 1 || data.length > 10000 || data.some(x => !x || typeof x !== 'object' || Array.isArray(x))) {
    throw new Error('Enter an array of 1–10,000 recipient objects.');
  }
  return data;
}
$('recipients').addEventListener('input', () => {
  try { $('recipient-count').textContent = `· ${parseRecipients().length}`; } catch { $('recipient-count').textContent = ''; }
});
$('sample').addEventListener('click', () => {
  $('title').value = 'Python Fundamentals'; $('organization').value = 'Learning Academy';
  $('recipients').value = JSON.stringify([{name:'Ada Lovelace',email:'ada@example.com'},{name:'Grace Hopper',email:'grace@example.com'}], null, 2);
  $('recipients').dispatchEvent(new Event('input'));
});
$('import-file').addEventListener('change', async (event) => {
  const file = event.target.files[0]; if (!file) return;
  try {
    if (file.size > 5 * 1024 * 1024) throw new Error('Choose a JSON or CSV file smaller than 5 MB.');
    const text = await file.text();
    const data = file.name.toLowerCase().endsWith('.csv') ? parseCSVRecipients(text) : JSON.parse(text);
    const recipients = Array.isArray(data) ? data : data.recipients;
    if (!Array.isArray(recipients)) throw new Error('The file must contain a recipient array or a request object with recipients.');
    $('recipients').value = JSON.stringify(recipients, null, 2);
    parseRecipients();
    if (!Array.isArray(data)) {
      for (const key of ['title','organization']) if (typeof data[key] === 'string') $(key).value = data[key];
      if (typeof data.issued_on === 'string') $('issued-on').value = data.issued_on;
    }
    $('recipients').dispatchEvent(new Event('input'));
    notice(`Imported ${recipients.length} recipients.`);
  } catch (error) { notice(error.message, true); }
  event.target.value = '';
});
$('create-form').addEventListener('submit', async (event) => {
  event.preventDefault(); $('submit').disabled = true;
  try {
    const result = await api('/jobs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:$('title').value,organization:$('organization').value,issued_on:$('issued-on').value,recipients:parseRecipients()})});
    notice('Job accepted. Certificates are being generated.');
    track(result.id);
    loadHistory();
  } catch (error) { notice(error.message, true); }
  finally { $('submit').disabled = false; }
});
function track(id) {
  if (!uuid.test(id)) { notice('Enter a valid job UUID.', true); return; }
  clearTimeout(timer); revision++; loading = false; jobId = id; offset = 0;
  $('job-id').value = id;
  const url = new URL(location.href); url.searchParams.set('job', id); history.replaceState(null,'',url);
  $('job').hidden = true; $('empty').hidden = false;
  refresh();
}
$('lookup-form').addEventListener('submit', event => { event.preventDefault(); track($('job-id').value.trim()); });
function formatError(error) {
  if (!error) return '';
  try { return JSON.parse(error).map(e => `${e.field}: ${e.message}`).join('; '); } catch { return error; }
}
function renderRows(items) {
  $('results-body').replaceChildren();
  for (const item of items) {
    const row = document.createElement('tr');
    const recipient = document.createElement('td'); recipient.textContent = item.name || `Recipient #${item.position + 1}`;
    const email = document.createElement('small'); email.textContent = item.email || 'Invalid recipient data'; recipient.append(email);
    const outcome = document.createElement('td'); const badge = document.createElement('span'); badge.className = `badge ${item.status}`; badge.textContent = item.status; outcome.append(badge);
    if (item.error) { const error = document.createElement('div'); error.className='error-detail'; error.textContent=formatError(item.error); outcome.append(error); }
    const download = document.createElement('td');
    if (item.download_url) { const link = document.createElement('a'); link.href=item.download_url; link.textContent='PDF ↓'; download.append(link); } else { download.textContent='—'; }
    row.append(recipient, outcome, download); $('results-body').append(row);
  }
}
async function refresh() {
  if (!jobId || loading) return;
  clearTimeout(timer); loading = true; const current = revision, id = jobId;
  $('previous').disabled = true; $('next').disabled = true;
  try {
    const [job, page] = await Promise.all([api(`/jobs/${id}`),api(`/jobs/${id}/certificates?offset=${offset}&limit=${limit}`)]);
    if (current !== revision) return;
    $('job').hidden=false; $('empty').hidden=true;
    $('job-title').textContent=job.title; $('job-meta').textContent=`${job.organization} · Issued ${job.issued_on}`;
    $('active-id').textContent=`Job ${job.id}`;
    $('job-status').textContent=job.status.replaceAll('_',' '); $('job-status').className=`badge ${job.status}`;
    for (const key of ['total','succeeded','failed']) $(key).textContent=job[key];
    $('progress').value=job.progress_percent; $('percentage').textContent=`${job.progress_percent}%`;
    $('progress-label').textContent=`${job.processed} of ${job.total} processed`;
    const done=terminal.has(job.status);
    $('archive').hidden=!done; $('archive').href=`/jobs/${id}/archive`;
    $('retry').hidden=!(done && job.failed>0); $('retry').disabled=false;
    $('polling-label').textContent=done ? 'Job finished. Validation errors require corrected data in a new job.' : 'Updating automatically every second…';
    renderRows(page.items);
    $('page-label').textContent=`${offset+1}–${offset+page.items.length} of ${job.total}`;
    $('previous').disabled=offset===0; $('next').disabled=offset+limit>=job.total;
    if (!done) timer=setTimeout(refresh,1000);
    else loadHistory();
  } catch (error) {
    if (current === revision) { notice(error.message,true); $('polling-label').textContent='Updates paused. Select Refresh to try again.'; }
  } finally { if (current === revision) loading=false; }
}
$('refresh').addEventListener('click', refresh);
$('previous').addEventListener('click', () => { if (!loading) {offset=Math.max(0,offset-limit);refresh();} });
$('next').addEventListener('click', () => { if (!loading) {offset+=limit;refresh();} });
$('retry').addEventListener('click', async () => {
  const id=jobId, current=revision; $('retry').disabled=true;
  try {
    const result=await api(`/jobs/${id}/retry`,{method:'POST'});
    if (current === revision) {notice(`Requeued ${result.retried} generation failures.`); track(id);}
  } catch(error) {if(current===revision) notice(error.message,true);}
  finally {if(current===revision) $('retry').disabled=false;}
});
const saved = new URL(location.href).searchParams.get('job');


let historyOffset = 0, historyLoading = false;
async function loadHistory() {
  if (historyLoading) return;
  historyLoading = true;
  for (const id of ['history-previous','history-next','history-refresh']) $(id).disabled=true;
  try {
    const page = await api(`/jobs?offset=${historyOffset}&limit=10`);
    $('history-body').replaceChildren();
    for (const job of page.items) {
      const row=document.createElement('tr');
      const title=document.createElement('td'); title.textContent=job.title;
      const org=document.createElement('small');org.textContent=job.organization;title.append(org);
      const created=document.createElement('td'); created.textContent=new Date(job.created_at).toLocaleString();
      const state=document.createElement('td');const badge=document.createElement('span');badge.className=`badge ${job.status}`;badge.textContent=job.status.replaceAll('_',' ');state.append(badge);
      const total=document.createElement('td');total.textContent=job.total;
      const action=document.createElement('td');const open=document.createElement('button');open.className='text-button';open.textContent='Open →';open.setAttribute('aria-label',`Open job ${job.title}`);open.addEventListener('click',()=>{track(job.id);$('lookup-form').scrollIntoView({behavior:'smooth',block:'center'});});action.append(open);
      row.append(title,created,state,total,action);$('history-body').append(row);
    }
    $('history-message').textContent=page.total ? '' : 'No jobs yet. Create your first batch above.';
    $('history-page').textContent=page.total ? `${historyOffset+1}–${historyOffset+page.items.length} of ${page.total}` : '0 jobs';
    $('history-previous').disabled=historyOffset===0;
    $('history-next').disabled=historyOffset+10>=page.total;
  } catch(error) {$('history-message').textContent=`Could not load jobs: ${error.message}`;}
  finally {historyLoading=false;$('history-refresh').disabled=false;}
}
$('history-refresh').addEventListener('click',loadHistory);
$('history-previous').addEventListener('click',()=>{historyOffset=Math.max(0,historyOffset-10);loadHistory();});
$('history-next').addEventListener('click',()=>{historyOffset+=10;loadHistory();});
loadHistory();

if (saved) track(saved);
