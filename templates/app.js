/* No external JavaScript dependencies; charts are rendered as accessible SVG. */
const $ = id => document.getElementById(id);
let direction = 'long', result = null, quote = null, busy = false, generation = 0;
const fmt = value => Number(value).toFixed(2);
const labels = ['1 hour', '3 hours', '5 hours', '1 day', '1 week', '1 month'];
const escapeText = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, {cache: 'no-store', ...options, signal: AbortSignal.timeout(70000)}); }
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
    $('outlook').textContent='';$('evidence').textContent='';$('news').replaceChildren();
}
function showQuote(value) {
    if (!Number.isFinite(value.price) || !Number.isFinite(Date.parse(value.timestamp_utc))) throw new Error('The source returned an invalid quote.');
    if (quote && Date.parse(value.checked_at_utc) < Date.parse(quote.checked_at_utc)) return;
    quote = value;
    $('price').textContent = '$' + fmt(value.price);
    const age = Math.max(0, Math.round((Date.now() - Date.parse(value.timestamp_utc))/60000));
    $('quote-time').textContent = 'Quote: ' + new Date(value.timestamp_utc).toLocaleString() + ` · ${age} min old · checking every 10 seconds`;
    const status = {fresh:'Recent quote', delayed:'Delayed quote · checked automatically', market_closed:'Market closed · last session quote', unavailable:'Quote too old · waiting for an update'};
    $('status').textContent = status[value.status] || 'Quote unavailable';
    $('status').dataset.status = value.status;
}
function svg(content, description, height = 190) {
    return `<svg viewBox="0 0 640 ${height}" role="img" aria-label="${escapeText(description)}">${content}</svg>`;
}
function probabilityChart(rows) {
    const pts=rows.map((r,i)=>({x:45+i*(550/Math.max(1,rows.length-1)),y:145-r.probability*110,row:r}));
    return svg(`<path d="${pts.map((p,i)=>`${i?'L':'M'}${p.x},${p.y}`).join(' ')}" fill="none" stroke="#6fe0bf" stroke-width="2"/>`+pts.map(p=>`<circle cx="${p.x}" cy="${p.y}" r="4" fill="#6fe0bf"/><text x="${p.x}" y="${p.y-12}" text-anchor="middle">${(p.row.probability*100).toFixed(1)}%</text><text x="${p.x}" y="175" text-anchor="middle">${escapeText(p.row.label)}</text>`).join(''),'Estimated target-touch probabilities by horizon');
}
function firstScenarioCrossing(data,hours) {
    const points=data.projection.filter(p=>p.hours<=hours), found=[];
    for(const key of ['low','median','high']){
        for(let i=1;i<points.length;i++){
            const a=points[i-1],b=points[i];
            if((a[key]-data.target_price)*(b[key]-data.target_price)<=0 && a[key]!==b[key]){
                const fraction=(data.target_price-a[key])/(b[key]-a[key]);
                found.push({key,hours:a.hours+fraction*(b.hours-a.hours),slope:(b[key]-a[key])/(b.hours-a.hours)});break;
            }
        }
    }
    return found.sort((a,b)=>a.hours-b.hours)[0] || null;
}
function timelineLabel(data,hours,total,compact=false) {
    const date=new Date(Date.parse(data.model.as_of_utc)+hours*3600000);
    return date.toLocaleString(undefined,compact?(total>24?{month:'short',day:'numeric'}:{hour:'2-digit',minute:'2-digit'}):(total<=5?{hour:'2-digit',minute:'2-digit'}:{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
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
    const ticks=Array.from({length:5},(_,i)=>{const h=hours*i/4;return `<line x1="${x(h)}" x2="${x(h)}" y1="190" y2="195" stroke="#94a1b3"/><text x="${x(h)}" y="215" text-anchor="${i===0?'start':i===4?'end':'middle'}">${escapeText(timelineLabel(data,h,hours,true))}</text>`;}).join('');
    const hit=firstScenarioCrossing(data,hours);
    let marker='';
    if(hit){
        const px=x(hit.hours),py=y(data.target_price),dx=16,dy=-hit.slope/(max-min)*155*hours/550*dx;
        marker=`<g data-crossing="${hit.key}"><title>Illustrative ${hit.key} scenario crossing at ${escapeText(timelineLabel(data,hit.hours,hours))}; not a forecast of first-touch time</title><line x1="${px}" x2="${px}" y1="190" y2="${py}" stroke="#e3c78e" stroke-dasharray="3 4"/><line x1="${px-dx}" x2="${px+dx}" y1="${py-dy}" y2="${py+dy}" stroke="#f0f4fa"/><circle cx="${px}" cy="${py}" r="5" fill="#e3c78e"/><text x="${Math.min(600,Math.max(65,px))}" y="238" text-anchor="${px>450?'end':'start'}">Scenario crossing ≈ ${escapeText(timelineLabel(data,hit.hours,hours))}</text></g>`;
    }else marker='<text x="58" y="238">No displayed scenario crosses the target in this window.</text>';
    return svg(`${grid}${['low','median','high'].map(key=>`<path d="${path(key)}" fill="none" stroke="${key==='median'?'#94a1b3':((key==='high')===(data.direction!=='short')?'#6fe0bf':'#ef7781')}" stroke-width="2" stroke-dasharray="5 5"/>`).join('')}${points.filter((p,i)=>i===0||i===points.length-1||[1,3,5,24,168,720].includes(p.hours)).map(p=>`<circle cx="${x(p.hours)}" cy="${y(p.median)}" r="3" fill="#94a1b3"/>`).join('')}<line x1="58" x2="608" y1="${y(data.target_price)}" y2="${y(data.target_price)}" stroke="#e3c78e" stroke-dasharray="5 5"/>${ticks}${marker}<text x="608" y="${Math.max(20,y(data.target_price)-6)}" text-anchor="end">Target $${fmt(data.target_price)}</text>`, `Projected median with 10 to 90 percentile range over ${hours} hours; not an actual future price path.`,260);
}
function render(data) {
    result = data; showQuote(data.quote);
    $('cards').innerHTML = data.horizons.map(row=>`<div class="card"><span>${escapeText(row.label)}</span><strong>${(row.probability*100).toFixed(1)}%</strong></div>`).join('');
    $('probability-chart').innerHTML = probabilityChart(data.horizons);
    $('projection-chart').innerHTML = projectionChart(data,Number($('chart-horizon').value));
    $('summary').textContent = `Move to target: ${data.price_change_percent.toFixed(2)}% · Implied ${data.leverage}× return: +${data.leveraged_return_percent.toFixed(2)}% before costs`;
    $('model-note').textContent = data.model.basis + '. ' + (data.model.delay_notice || 'A model range is not the actual future path.');
    if(data.outlook){
        const o=data.outlook;
        $('outlook').textContent=`Recommended action: ${o.action.toUpperCase()}${o.target_price?' · '+o.horizon+' · target $'+fmt(o.target_price):''} · ${o.leverage}× reference. ${o.reason}${o.conditional_setups?' Watch only: '+o.conditional_setups.map(s=>s.direction.toUpperCase()+' trigger $'+fmt(s.trigger)+' → target $'+fmt(s.target)).join('; ')+'. '+o.setup_notice:''}`;
        const e=data.evidence;
        $('evidence').textContent=`${e.observations} observed quotes · ${e.news.coverage}/2 news feeds available · ${e.news.headlines.length} recent relevant headlines. ${e.news.sources.map(s=>s.name+': '+(s.available?'reachable':'unavailable')).join(' · ')}. ${e.validation} ${e.unmodeled}${data.historical_context?' Historical context: '+Object.entries(data.historical_context.series).map(([k,v])=>k+' '+v.count+' daily observations ('+v.first+' to '+v.last+')').join('; ')+'. Daily validation MSE '+data.historical_context.validation.model_mse.toFixed(6)+' versus baseline '+data.historical_context.validation.zero_drift_mse.toFixed(6)+'. Historical directional model NOT promoted.':''}`;
        $('news').replaceChildren();
        for(const h of e.news.headlines){
            const a=document.createElement('a');
            if(!/^https?:\/\//i.test(h.url))continue;
            a.href=h.url;a.textContent=h.title;a.target='_blank';a.rel='noopener noreferrer';
            const p=document.createElement('p');p.append(a);$('news').append(p);
        }
    }
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
    try {
        const data=await api('/api/market');
        const previous = result?.quote || quote;
        const changed = !previous || data.quote.price !== previous.price || data.quote.timestamp_utc !== previous.timestamp_utc;
        showQuote(data.quote);
        if(!data.quote.usable) clearResults();
        else if(changed && result && !busy) calculate();
        if(data.source_warning) setError('Source refresh failed; showing the last verified snapshot.');
    }
    catch(error){ $('status').textContent='Source unavailable · retry shortly'; if(!quote) setError(error.message); }
    finally {setTimeout(refreshQuote,10000);}
}
for(const side of ['long','short']) $(''+side).addEventListener('click',()=>{direction=side;generation++;clearResults();$('long').setAttribute('aria-pressed',String(side==='long'));$('short').setAttribute('aria-pressed',String(side==='short'));});
$('target').addEventListener('input',()=>{generation++;clearResults();});
$('leverage').addEventListener('input',()=>{generation++;$('leverage-label').textContent=$('leverage').value+'×';clearResults();});
$('chart-horizon').addEventListener('change',()=>{if(result)$('projection-chart').innerHTML=projectionChart(result,Number($('chart-horizon').value));});
$('forecast-form').addEventListener('submit',calculate);
setInterval(()=>{
    if(result && Date.now()-Date.parse(result.model.as_of_utc)>180000) clearResults();
    if(quote && quote.market_open && Date.now()-Date.parse(quote.timestamp_utc)>900000){$('status').textContent='Quote too old · waiting for an update';clearResults();}
},1000);
refreshQuote();
