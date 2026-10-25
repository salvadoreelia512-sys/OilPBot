/* No external JavaScript dependencies; charts are rendered as accessible SVG. */
const $ = id => document.getElementById(id);
let direction = 'long', result = null, quote = null, busy = false, generation = 0;
const fmt = value => Number(value).toFixed(2);
const labels = ['1 hour', '3 hours', '5 hours', '1 day', '1 week'];
const escapeText = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, {...options, signal: AbortSignal.timeout(70000)}); }
    catch { throw new Error('The oil service could not be reached. It may be waking up; retry shortly.'); }
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('The host did not return an API response. Check the backend deployment.');
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the target price and leverage, then retry.');
    return data;
}
function setError(message) { $('error').textContent = message; $('error').hidden = !message; }
function clearResults() {
    result = null;
    $('cards').innerHTML = labels.map(label => `<div class="card"><span>${label}</span><strong>—</strong></div>`).join('');
    $('probability-chart').innerHTML = '<p class="muted">Enter a target to see estimated probabilities.</p>';
    $('projection-chart').innerHTML = '<p class="muted">Your projected range will appear here.</p>';
    $('summary').textContent = '';
    $('closed-note').hidden = true;
}
function showQuote(value) {
    if (!Number.isFinite(value.price) || !Number.isFinite(Date.parse(value.timestamp_utc))) throw new Error('The source returned an invalid quote.');
    if (quote && Date.parse(value.checked_at_utc) < Date.parse(quote.checked_at_utc)) return;
    quote = value;
    $('price').textContent = '$' + fmt(value.price);
    const age = Math.max(0, Math.round((Date.now() - Date.parse(value.timestamp_utc))/60000));
    $('quote-time').textContent = 'Quote: ' + new Date(value.timestamp_utc).toLocaleString() + ` · ${age} min old`;
    const status = {fresh:'Recent quote', delayed:'Delayed quote · checked automatically', market_closed:'Market closed · last session quote', unavailable:'Quote too old · waiting for an update'};
    $('status').textContent = status[value.status] || 'Quote unavailable';
    $('status').dataset.status = value.status;
}
function svg(content, description, height = 190) {
    return `<svg viewBox="0 0 640 ${height}" role="img" aria-label="${escapeText(description)}">${content}</svg>`;
}
function probabilityChart(rows) {
    return svg(rows.map((row,i) => {
        const x = 35 + i * 124, height = row.probability * 120;
        return `<rect x="${x}" y="${145-height}" width="70" height="${Math.max(1,height)}" rx="5" fill="#6fe0bf" opacity=".8"/><text x="${x+35}" y="${132-height}" text-anchor="middle">${(row.probability*100).toFixed(1)}%</text><text x="${x+35}" y="170" text-anchor="middle">${escapeText(row.label)}</text>`;
    }).join(''), rows.map(r => `${r.label}: ${(r.probability*100).toFixed(1)} percent`).join(', '));
}
function projectionChart(data, hours) {
    const points = data.projection.filter(p => p.hours <= hours);
    if (points.length < 2) return '';
    let min = Math.min(data.target_price,...points.map(p=>p.low)), max = Math.max(data.target_price,...points.map(p=>p.high));
    const pad = Math.max(.1,(max-min)*.12); min -= pad; max += pad;
    const x = h => 58 + h/hours*550, y = p => 190-(p-min)/(max-min)*155;
    const path = key => points.map((p,i)=>`${i?'L':'M'}${x(p.hours).toFixed(2)},${y(p[key]).toFixed(2)}`).join(' ');
    const band = path('high') + ' ' + [...points].reverse().map(p=>`L${x(p.hours).toFixed(2)},${y(p.low).toFixed(2)}`).join(' ')+' Z';
    let grid='';
    for(let i=0;i<4;i++){ const p=min+(max-min)*i/3; grid+=`<line x1="58" x2="608" y1="${y(p)}" y2="${y(p)}" stroke="#27303d"/><text x="50" y="${y(p)+4}" text-anchor="end">$${fmt(p)}</text>`; }
    return svg(`${grid}<path d="${band}" fill="#6fe0bf" opacity=".12"/><path d="${path('median')}" fill="none" stroke="#6fe0bf" stroke-width="2"/><line x1="58" x2="608" y1="${y(data.target_price)}" y2="${y(data.target_price)}" stroke="#e3c78e" stroke-dasharray="5 5"/><text x="58" y="215">Now</text><text x="608" y="215" text-anchor="end">+${hours} hours</text><text x="608" y="${Math.max(20,y(data.target_price)-6)}" text-anchor="end">Target $${fmt(data.target_price)}</text>`, `Projected median with 10 to 90 percentile range over ${hours} hours; not an actual future price path.`,235);
}
function render(data) {
    result = data; showQuote(data.quote);
    $('cards').innerHTML = data.horizons.map(row=>`<div class="card"><span>${escapeText(row.label)}</span><strong>${(row.probability*100).toFixed(1)}%</strong></div>`).join('');
    $('probability-chart').innerHTML = probabilityChart(data.horizons);
    $('projection-chart').innerHTML = projectionChart(data,Number($('chart-horizon').value));
    $('summary').textContent = `Move to target: ${data.price_change_percent.toFixed(2)}% · Implied ${data.leverage}× return: +${data.leveraged_return_percent.toFixed(2)}% before costs`;
    $('model-note').textContent = data.model.basis + '. ' + (data.model.delay_notice || 'A model range is not the actual future path.');
    $('closed-note').hidden = data.quote.market_open;
    $('closed-note').textContent = 'The market is closed. Horizons with no scheduled trading have no modeled movement. The quote above is the last session price.';
}
async function calculate(event) {
    event?.preventDefault(); if(busy) return;
    const id = ++generation;
    const target = Number($('target').value), leverage = Number($('leverage').value);
    clearResults(); setError('');
    if(!Number.isFinite(target) || target<=0 || target>1000000 || leverage<1 || leverage>100){setError('Enter a valid positive target and leverage from 1× to 100×.');return;}
    busy=true; $('submit').disabled=true; $('submit').textContent='Calculating…';
    try {
        const data=await api('/api/forecast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_price:target,direction,leverage})});
        if(id===generation) render(data);
    } catch(error){ if(id===generation) setError(error.message); }
    finally {busy=false; $('submit').disabled=false; $('submit').textContent='Calculate probability';}
}
async function refreshQuote() {
    try { const data=await api('/api/market'); showQuote(data.quote); if(data.source_warning) setError('Source refresh failed; showing the last verified snapshot.'); }
    catch(error){ $('status').textContent='Source unavailable · retry shortly'; if(!quote) setError(error.message); }
    finally {setTimeout(refreshQuote,60000);}
}
for(const side of ['long','short']) $(''+side).addEventListener('click',()=>{direction=side;generation++;clearResults();$('long').setAttribute('aria-pressed',String(side==='long'));$('short').setAttribute('aria-pressed',String(side==='short'));});
$('target').addEventListener('input',()=>{generation++;clearResults();});
$('leverage').addEventListener('input',()=>{generation++;$('leverage-label').textContent=$('leverage').value+'×';clearResults();});
$('chart-horizon').addEventListener('change',()=>{if(result)$('projection-chart').innerHTML=projectionChart(result,Number($('chart-horizon').value));});
$('forecast-form').addEventListener('submit',calculate);
setInterval(()=>{
    if(result && Date.now()-Date.parse(result.model.as_of_utc)>60000) clearResults();
    if(quote && quote.market_open && Date.now()-Date.parse(quote.timestamp_utc)>900000){$('status').textContent='Quote too old · waiting for an update';clearResults();}
},1000);
refreshQuote();
