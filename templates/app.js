/* No external JavaScript dependencies; charts are rendered as accessible SVG. */
const $ = id => document.getElementById(id);
let result = null, quote = null, busy = false, generation = 0, newsData = null, newsExpiresAt = Infinity, liveSignal = null;
const fmt = value => Number(value).toFixed(2);
const escapeText = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, {cache: 'no-store', ...options, signal: AbortSignal.timeout(70000)}); }
    catch { throw new Error('The oil service could not be reached. It may be waking up; retry shortly.'); }
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('The host did not return an API response. Check the backend deployment.');
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the optional reference price, then retry.');
    return data;
}
function setError(message) { $('error').textContent = message; $('error').hidden = !message; }
function clearResults() {
    result=null;
    $('projection-chart').innerHTML='<p class="muted">Press Calculate outlook to see modeled price outcomes.</p>';
    $('outcome-likelihood').textContent='';$('outcome-ranges').textContent='';
}
function showQuote(value) {
    if (!Number.isFinite(value.price) || !Number.isFinite(Date.parse(value.timestamp_utc))) throw new Error('The source returned an invalid quote.');
    if (quote && (Date.parse(value.checked_at_utc) < Date.parse(quote.checked_at_utc) || Date.parse(value.timestamp_utc) < Date.parse(quote.timestamp_utc))) return false;
    quote = value;
    $('price').textContent = '$' + fmt(value.price);
    const age = Math.max(0, Math.round((Date.now() - Date.parse(value.timestamp_utc))/1000));
    $('quote-time').textContent = 'Quote: ' + new Date(value.timestamp_utc).toLocaleString() + ` · ${age} sec old · live stream · checked every second`;
    const status = {fresh:'Recent quote', delayed:'Delayed quote · checked automatically', market_closed:'Market closed · last session quote', unavailable:'Quote too old · waiting for an update'};
    $('status').textContent = status[value.status] || 'Quote unavailable';
    $('status').dataset.status = value.status;
}
function svg(content, description, height = 190) {
    return `<svg viewBox="0 0 640 ${height}" role="img" aria-label="${escapeText(description)}">${content}</svg>`;
}
function timelineLabel(data,hours,total,compact=false) {
    const date=new Date(Date.parse(data.model.as_of_utc)+hours*3600000);
    return date.toLocaleString(undefined,compact?(total>24?{month:'short',day:'numeric'}:{hour:'2-digit',minute:'2-digit'}):{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
}
function projectionChart(data,hours) {
    const outcome=data.outcomes[String(hours)];
    if(!outcome)return '<p>No model result for this horizon.</p>';
    const width=760,height=300,left=58,right=535,top=25,bottom=215;
    const all=outcome.groups.flatMap(g=>g.curve),reference=data.reference_price;
    let min=Math.min(...all,...(Number.isFinite(reference)?[reference]:[])),max=Math.max(...all,...(Number.isFinite(reference)?[reference]:[]));
    const pad=Math.max(.1,(max-min)*.14);min-=pad;max+=pad;
    const x=h=>left+h/hours*(right-left),y=p=>bottom-(p-min)/(max-min)*(bottom-top);
    const colors={down:'#ef7781',middle:'#94a1b3',up:'#6fe0bf'};
    let content='';
    for(let i=0;i<4;i++){const p=min+(max-min)*i/3;content+=`<line x1="${left}" x2="${right}" y1="${y(p)}" y2="${y(p)}" stroke="#27303d"/><text x="${left-8}" y="${y(p)+4}" text-anchor="end">$${fmt(p)}</text>`;}
    for(let i=0;i<5;i++){const h=hours*i/4;content+=`<text x="${x(h)}" y="240" text-anchor="${i===0?'start':i===4?'end':'middle'}">${escapeText(timelineLabel(data,h,hours,true))}</text>`;}
    const endpoints=outcome.groups.map(g=>({g,anchor:y(g.endpoint ?? outcome.reference_price),label:y(g.endpoint ?? outcome.reference_price)})).sort((a,b)=>a.anchor-b.anchor);
    for(let i=0;i<endpoints.length;i++)endpoints[i].label=Math.max(top+10,i?endpoints[i-1].label+34:top+10,endpoints[i].label);
    if(endpoints.at(-1).label>bottom){const shift=endpoints.at(-1).label-bottom;for(const p of endpoints)p.label-=shift;}
    for(const item of [...endpoints].sort((a,b)=>Number(outcome.most_likely.includes(a.g.key))-Number(outcome.most_likely.includes(b.g.key)))){
        const g=item.g,color=colors[g.key],path=g.curve.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(2)},${y(p).toFixed(2)}`).join(' ');
        const highest=outcome.most_likely.includes(g.key);
        content+=`${g.curve.length?`<path d="${path}" fill="none" stroke="${color}" stroke-width="${highest?2.8:2}" stroke-dasharray="5 5"/><circle cx="${right}" cy="${item.anchor}" r="3" fill="${color}"/>`:""}<path d="M${right+3},${item.anchor} L555,${item.label-3} L565,${item.label-3}" fill="none" stroke="${color}"/><text data-outcome="${g.key}" x="572" y="${item.label}" style="fill:${color};font-weight:${highest?700:400}">${escapeText(g.label)} ${(g.percentage_units/10).toFixed(1)}%${highest?' · highest':''}</text><text x="572" y="${item.label+14}">${g.endpoint===null?"No sampled paths":"$"+fmt(g.endpoint)+" guide"}</text>`;
    }
    if(Number.isFinite(reference))content+=`<line x1="${left}" x2="${right}" y1="${y(reference)}" y2="${y(reference)}" stroke="#e3c78e" stroke-dasharray="5 5"/><text x="${right}" y="${Math.max(15,y(reference)-7)}" text-anchor="end">Reference $${fmt(reference)}</text>`;
    content+='<text x="58" y="275">Conditional median guides; percentages apply to ending-price ranges.</text>';
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Model terminal price outcomes over ${hours} hours; shares total 100 percent">${content}</svg>`;
}
function renderSelectedOutcome() {
    if(!result)return;
    const hours=Number($('chart-horizon').value),o=result.outcomes[String(hours)];
    $('projection-chart').innerHTML=projectionChart(result,hours);
    const leaders=o.groups.filter(g=>o.most_likely.includes(g.key));
    $('outcome-likelihood').textContent=`Most likely modeled outcome${leaders.length>1?'s':''}: ${leaders.map(g=>g.label+' '+(g.percentage_units/10).toFixed(1)+'%').join(' / ')}. Total: ${o.percentage_total.toFixed(1)}%.${o.near_tie?' Near tie — no clear directional separation.':''}`;
    $('outcome-ranges').textContent=`Compared with quote $${fmt(o.reference_price)}: Down below $${fmt(o.lower_boundary)}; Sideways $${fmt(o.lower_boundary)}–$${fmt(o.upper_boundary)}; Up above $${fmt(o.upper_boundary)}. Ends ${new Date(o.ends_at_utc).toLocaleString()}. ${o.trading_hours===0?'No scheduled trading movement in this window.':''}`;
}
function render(data) {
    result=data;showQuote(data.quote);
    $('forecast-state').textContent=`Fixed outlook calculated ${new Date(data.model.as_of_utc).toLocaleString()}. Live prices and news do not change this result; press Calculate outlook for a new snapshot.`;
    renderSelectedOutcome();
    $('model-note').textContent=`${data.model.simulation_paths.toLocaleString()} shared risk-conditioned simulations · daily history through ${data.model.last_daily_bar}. Current headlines and dated supply data affect temporary volatility/jump stress under unvalidated assumptions. These percentages are not calibrated confidence or trade instructions.`;
    const report=data.validation,lines=[];
    for(const [h,v] of Object.entries(report.horizons || {}))lines.push(`${h}: ${v.origins} non-overlapping daily origins; baseline terminal-category Brier ${v.multiclass_brier.toFixed(3)} versus frequency benchmark ${v.frequency_benchmark_brier.toFixed(3)} (lower is better, scale 0–2).`);
    $('outcome-validation').textContent=lines.join(' ')+' These are separate terminal-close tests, not the old target-touch or trading-rule results. They do not validate the live news overlay, hourly forecasts, arbitrary live origins, or exact dashed paths. Historical data may be revised.'+(report.current_archive_matches===false?' The current archive differs from the frozen report.':'');
}
async function calculate(event) {
    event?.preventDefault();if(busy)return;
    const id=++generation,text=$('target').value.trim(),reference=text===''?null:Number(text);
    setError('');
    if(reference!==null && (!Number.isFinite(reference)||reference<=0||reference>1000000)){setError('Enter a positive optional reference price up to 1,000,000.');return;}
    busy=true;$('submit').disabled=true;$('submit').textContent='Calculating…';
    try {
        const data=await api('/api/outlook',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reference_price:reference})});
        if(id===generation)render(data);
    }catch(error){if(id===generation)setError(error.message);}
    finally{busy=false;$('submit').disabled=false;$('submit').textContent='Calculate outlook';}
}
function applyQuote(data) {
    if(showQuote(data.quote)===false) return;
    updateSignalQuote(data.quote);
    if(data.source_warning) setError('Source connection interrupted; showing the last verified quote.');
}
async function refreshQuote() {
    try { applyQuote(await api('/api/market')); }
    catch(error){ $('status').textContent='Source unavailable · retry shortly'; if(!quote) setError(error.message); }
    finally {setTimeout(refreshQuote,1000);}
}
function connectQuotes() {
    if(typeof WebSocket === 'undefined') return;
    const socket = new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/api/live`);
    socket.onmessage = event => {
        try { applyQuote(JSON.parse(event.data)); }
        catch { $('status').textContent='Invalid quote update · retrying automatically'; }
    };
    socket.onclose = () => setTimeout(connectQuotes,15000);
    socket.onerror = () => socket.close();
}
function inputsChanged() {
    generation++;
    if(result)$('forecast-state').textContent=`Reference changed. Showing the outlook calculated ${new Date(result.model.as_of_utc).toLocaleString()}; press Calculate outlook to use the new reference.`;
}
$('target').addEventListener('input',inputsChanged);
$('chart-horizon').addEventListener('change',renderSelectedOutcome);
$('forecast-form').addEventListener('submit',calculate);
setInterval(()=>{
    if(newsData && Date.now()>=newsExpiresAt) renderNews(newsData,true);
    if(quote){showQuote(quote);updateSignalQuote(quote);}
    if(quote && quote.market_open && Date.now()-Date.parse(quote.timestamp_utc)>120000){$('status').textContent='Quote too old · waiting for an update';$('status').dataset.status='unavailable';}
},1000);
function renderNews(data, preserveStatus = false) {
    newsData = data;
    const headlines = data.headlines.filter(h=>{const age=Date.now()-Date.parse(h.published_utc);return Number.isFinite(age) && age>=0 && age<=86400000;});
    newsExpiresAt = headlines.length ? Math.min(...headlines.map(h=>Date.parse(h.published_utc)+86400001)) : Infinity;
    const available = data.sources.filter(s=>s.available).length;
    const checked = data.checked_at_utc ? new Date(data.checked_at_utc).toLocaleString() : 'unknown';
    if(!preserveStatus) $('news-status').textContent = `${available}/${data.sources.length} feeds reachable · Checked ${checked}. ` + data.sources.map(s=>s.name+': '+(s.available?'reachable':'unavailable')).join(' · ');
    $('news').replaceChildren();
    if(!headlines.length) {
        const p=document.createElement('p');
        p.textContent=available ? 'No matching oil headlines published in the last 24 hours.' : 'News sources unavailable. Retrying automatically.';
        $('news').append(p);
    }
    for(const h of headlines) {
        if(!/^https?:\/\//i.test(h.url)) continue;
        const p=document.createElement('p'), a=document.createElement('a'), meta=document.createElement('small');
        a.href=h.url;a.textContent=h.title;a.target='_blank';a.rel='noopener noreferrer';
        meta.textContent=` · ${h.source} · ${new Date(h.published_utc).toLocaleString()}${h.potential_relevance?' · '+h.potential_relevance:''}`;
        p.append(a,meta);$('news').append(p);
    }
}
function renderSignal(data) {
    liveSignal=data;
    const angle=data.score*Math.PI/2, x=110+75*Math.sin(angle),y=110-75*Math.cos(angle);
    $('signal-gauge').innerHTML=`<svg viewBox="0 0 220 150" role="img" aria-label="${escapeText(data.rating)} technical agreement"><path d="M20 110 A90 90 0 0 1 65 32" fill="none" stroke="#ef7781" stroke-width="12" stroke-linecap="round"/><path d="M65 32 A90 90 0 0 1 155 32" fill="none" stroke="#94a1b3" stroke-width="12"/><path d="M155 32 A90 90 0 0 1 200 110" fill="none" stroke="#6fe0bf" stroke-width="12" stroke-linecap="round"/><path d="M110 110 L${x} ${y}" stroke="#f0f4fa" stroke-width="6" stroke-linecap="round"/><text x="110" y="136" text-anchor="middle">${escapeText(data.rating)}</text></svg><p class="note">Sell ${data.counts.sell} · Neutral ${data.counts.neutral} · Buy ${data.counts.buy}. Votes are correlated indicators, not probabilities.</p>`;
    const setup=data.setup;
    $('setup-levels').hidden=!setup;
    if(setup){
        $('setup-direction').textContent=setup.direction.toUpperCase();
        $('setup-entry').textContent=(setup.direction==='long'?'Break above $':'Break below $')+fmt(setup.entry);
        $('setup-stop').textContent='$'+fmt(setup.invalidation);
        $('setup-week').textContent='$'+fmt(setup.week_target);
        $('setup-month').textContent='$'+fmt(setup.month_target);
    }
    $('setup-note').textContent=`Reference levels from daily session ${data.as_of_bar}, weekly session ${data.weekly_as_of}. Valid until ${new Date(data.valid_until).toLocaleString()}. ${data.scenario_basis || ''} ${data.notice} Week/month targets are alternative reference scenarios.`;
    const report=data.historical_validation;
    const negative=Object.values(report.horizons || {}).some(v=>(v.heldout.net_mean_by_roundtrip_bps?.['25'] ?? 0)<0);
    $('signal-confidence').textContent=data.rule_eligible===false?'Low technical agreement: this displayed model direction is not covered by the qualified-rule backtest.':report.reliability_gate_passed?'Historical technical rule passed its stated gate under assumed costs. The combined live/news action remains unvalidated.':negative?'Historical simulation includes negative average net returns. Watch levels only; no proven best action.':'Historical advantage is not established: conditional watch levels, not a proven best action.';
    $('signal-indicators').textContent=`Daily: ${data.daily.indicators.map(i=>i.name+': '+({1:'buy',0:'neutral','-1':'sell'}[i.vote])).join(' · ')}. Weekly: ${data.weekly.indicators.map(i=>i.name+': '+({1:'buy',0:'neutral','-1':'sell'}[i.vote])).join(' · ')}.`;
    const lines=[];
    for(const [h,v] of Object.entries(report.horizons || {})) {
        const r=v.heldout;
        lines.push(r.trades?`${h}: ${r.trades} held-out trades; mean net return after assumed 25 bps ${ (r.net_mean_by_roundtrip_bps['25']*100).toFixed(2)}%; block interval ${r.mean_net_return_95pct_block_interval_at_25bps.map(x=>(x*100).toFixed(2)+'%').join(' to ')}.`:`${h}: no triggered trades in the evaluated sample.`);
    }
    $('signal-validation').textContent=lines.join(' ')+' Fixed rule, daily OHLC stop-first assumptions, gaps and assumed costs. Actual broker fills/financing, news filter and live intraday timing are not validated. Report through '+(report.data_last_bar || 'unknown')+'. Historical results cover qualified technical agreement, not every low-agreement model scenario.';
    updateSignalQuote(quote || data.quote);
}
function updateSignalQuote(value) {
    if(!liveSignal) return;
    const data=liveSignal,setup=data.setup;
    const blocked=[...data.blocked_reasons].filter(r=>!r.includes('historical edge')&&!r.includes('agreement is weak'));
    if(!value.usable || value.market_open && Date.now()-Date.parse(value.timestamp_utc)>120000)blocked.push('The live quote is stale');
    if(!value.market_open)blocked.push('Market closed');
    if(Date.now()>=Date.parse(data.valid_until))blocked.push('Scenario expired; waiting for a newer completed session');
    $('signal-current').textContent=`Current $${fmt(value.price)} · quote ${new Date(value.timestamp_utc).toLocaleString()} · model check ${new Date(data.generated_at_utc).toLocaleString()}`;
    if(!setup){$('outlook').textContent='Model scenario unavailable';$('signal-status').textContent=data.scenario_basis || 'No directional scenario from the available data.';return;}
    const side=setup.direction==='long'?'Long':'Short';
    $('outlook').textContent=`Model scenario: ${side} at $${fmt(setup.entry)}/barrel`;
    const sign=setup.direction==='long'?1:-1,beyond=sign*(value.price-setup.entry);
    $('signal-current').textContent+=beyond>=0?' · Reference trigger crossed':` · $${fmt(Math.abs(value.price-setup.entry))} to entry trigger`;
    if(beyond>.25*data.atr_14){$('signal-status').textContent='Entry reference has passed — not a fresh entry; do not chase.';return;}
    if(blocked.length){$('signal-status').textContent=[...new Set(blocked)].join(' · ');return;}
    $('signal-status').textContent=beyond>=0?'Reference trigger reached. The displayed entry is not a guaranteed fill; no order has been placed.':`Entry condition: price must ${sign>0?'break above':'break below'} $${fmt(setup.entry)}. This is a model scenario, not a validated entry instruction.`;
}

async function refreshSignal() {
    try { renderSignal(await api('/api/signal')); }
    catch(error){liveSignal=null;$('setup-levels').hidden=true;$('signal-gauge').innerHTML='<p class="muted">Signal unavailable</p>';$('signal-current').textContent='';$('setup-note').textContent='';$('outlook').textContent='Model scenario unavailable';$('signal-status').textContent=error.message;$('signal-confidence').textContent='No current action is suggested.';}
    finally {setTimeout(refreshSignal,15000);}
}
async function refreshNews() {
    try { renderNews(await api('/api/news')); }
    catch(error) { $('news-status').textContent='News refresh failed · previous articles may be outdated. '+error.message; }
    finally { setTimeout(refreshNews,60000); }
}
refreshNews();
refreshSignal();
connectQuotes();
refreshQuote();
