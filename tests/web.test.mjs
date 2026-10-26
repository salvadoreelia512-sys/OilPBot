import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {handler} from '../netlify/functions/api.mjs';
const script=readFileSync(new URL('../templates/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
const spot={price:100,symbol:'TVC:USOIL',timestamp_utc:new Date().toISOString(),status:'fresh',market_open:true,usable:true};
const response=(status,body,type='application/json')=>new Response(typeof body==='string'?body:JSON.stringify(body),{status,headers:{'content-type':type}});
const signalFixture=()=>({score:-.5,rating:'Sell',counts:{sell:6,neutral:2,buy:0},daily:{indicators:[]},weekly:{indicators:[]},as_of_bar:'2026-10-23',weekly_as_of:'2026-10-23',valid_until:new Date(Date.now()+86400000).toISOString(),generated_at_utc:new Date().toISOString(),quote:spot,atr_14:1,blocked_reasons:['No positive historical edge established; watch only'],setup:{direction:'short',entry:99,invalidation:100.5,week_target:97,month_target:96},historical_validation:{reliability_gate_passed:false,horizons:{}},notice:'Unvalidated scenario'});
const fixture=(reference=null)=>({quote:spot,reference_price:reference,outcomes:Object.fromEntries([1,3,5,24,168,720].map(hours=>{
 const units=hours===1?[340,430,230]:[500,30,470];
 return [String(hours),{hours,label:hours+' hours',ends_at_utc:new Date(Date.now()+hours*3600000).toISOString(),trading_hours:hours,
 reference_price:100,lower_boundary:99.75,upper_boundary:100.25,percentage_total:100,most_likely:[hours===1?'middle':'down'],near_tie:hours!==1,
 groups:['down','middle','up'].map((key,i)=>({key,label:['Down','Sideways','Up'][i],probability:units[i]/1000,percentage_units:units[i],sample_count:units[i]*6,endpoint:99+i,curve:Array.from({length:hours+1},(_,h)=>100+(i-1)*h/hours)}))}];})),
 model:{as_of_utc:new Date().toISOString(),last_daily_bar:'2026-10-23',simulation_paths:6000},validation:{current_archive_matches:true,horizons:{'1 week':{origins:392,multiclass_brier:.53,frequency_benchmark_brier:.52}}}});
function ui(onOutlook=async(path,options)=>response(200,fixture(JSON.parse(options.body).reference_price))){
 const elements={},timers=[],intervals=[],requests=[];
 const get=id=>elements[id]??=({value:id==='target'?'':id==='chart-horizon'?'1':'',textContent:'',innerHTML:'',hidden:false,dataset:{},disabled:false,events:{},children:[],append(...nodes){this.children.push(...nodes)},replaceChildren(){this.children=[];this.innerHTML=''},addEventListener(type,fn){this.events[type]=fn},setAttribute(key,value){this[key]=value}});
 const context=vm.createContext({document:{getElementById:get,createElement:()=>({textContent:'',children:[],append(...nodes){this.children.push(...nodes)}})},fetch:async(path,options)=>{
  requests.push({path,options});
  return path==='/api/market'?response(200,{quote:spot}):path==='/api/news'?response(200,{sources:[],headlines:[]}):path==='/api/signal'?response(200,signalFixture()):onOutlook(path,options);
 },AbortSignal,Date,console,setTimeout:(fn,ms)=>{timers.push({fn,ms});return 0},setInterval:(fn,ms)=>{intervals.push({fn,ms});return 0}});
 vm.runInContext(script,context);
 const api=vm.runInContext('({calculate,render,clearResults,projectionChart,renderSelectedOutcome,renderNews,refreshSignal,applyQuote,connectQuotes,renderSignal})',context);
 return {get,api,context,timers,intervals,requests};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const posts=ui=>ui.requests.filter(r=>r.path==='/api/outlook');

test('removed panels and obsolete direction/leverage controls are absent',()=>{
 for(const text of ['Chance of touching your target','Strategic evidence &amp; downside scenarios','id="cards"','id="probability-chart"','id="leverage"','id="long"','id="short"'])assert.ok(!html.includes(text),text);
 assert.match(html,/id="outcome-likelihood"/);assert.match(html,/optional/);
 const ids=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
 for(const match of script.matchAll(/\$\('([^']+)'\)/g))assert.ok(ids.has(match[1]),'Missing DOM node '+match[1]);
});

test('optional reference sends only outlook data and all three end labels render',async()=>{
 const u=ui();await flush();u.get('target').value='102';await u.api.calculate();
 assert.deepEqual(JSON.parse(posts(u)[0].options.body),{reference_price:102});
 assert.match(u.get('projection-chart').innerHTML,/Down 34.0%/);assert.match(u.get('projection-chart').innerHTML,/Sideways 43.0%/);assert.match(u.get('projection-chart').innerHTML,/Up 23.0%/);
 assert.match(u.get('outcome-likelihood').textContent,/Sideways 43.0%/);assert.match(u.get('outcome-likelihood').textContent,/100.0%/);
 assert.match(u.get('projection-chart').innerHTML,/Reference \$102.00/);assert.match(u.get('outcome-validation').textContent,/terminal-close tests/);
});

test('blank reference is allowed and chart-horizon changes use cached results',async()=>{
 const u=ui();await flush();await u.api.calculate();assert.deepEqual(JSON.parse(posts(u)[0].options.body),{reference_price:null});
 u.get('chart-horizon').value='168';u.get('chart-horizon').events.change();
 assert.match(u.get('outcome-likelihood').textContent,/Down 50.0%/);assert.match(u.get('outcome-likelihood').textContent,/Near tie/);assert.equal(posts(u).length,1);
 for(const hours of [1,3,5,24,168,720]){u.get('chart-horizon').value=String(hours);u.get('chart-horizon').events.change();assert.doesNotMatch(u.get('projection-chart').innerHTML,/NaN|undefined/);}
});

test('live prices, signal refreshes, clock and reference edits do not recalculate or erase',async()=>{
 const u=ui();await flush();await u.api.calculate();const before=u.get('projection-chart').innerHTML;
 u.api.applyQuote({quote:{...spot,price:101,usable:false}});u.intervals[0].fn();await u.api.refreshSignal();
 u.get('target').value='97.5';u.get('target').events.input();
 assert.equal(posts(u).length,1);assert.equal(u.get('projection-chart').innerHTML,before);assert.match(u.get('forecast-state').textContent,/Reference changed/);
 await u.api.calculate();assert.equal(posts(u).length,2);
});

test('failed replacement retains previous outlook; retry recovers',async()=>{
 let count=0;const u=ui(async()=>++count===2?response(503,{detail:'Source unavailable'}):response(200,fixture()));await flush();
 await u.api.calculate();const before=u.get('projection-chart').innerHTML;await u.api.calculate();
 assert.equal(u.get('projection-chart').innerHTML,before);assert.equal(u.get('error').textContent,'Source unavailable');
 await u.api.calculate();assert.equal(u.get('error').hidden,true);
});

test('HTML responses and invalid references are reported without a crash',async()=>{
 const u=ui(async()=>response(200,'<html>Unavailable</html>','text/html'));await flush();await u.api.calculate();assert.match(u.get('error').textContent,/host did not return an API/);
 const count=posts(u).length;u.get('target').value='-1';await u.api.calculate();assert.equal(posts(u).length,count);assert.match(u.get('error').textContent,/positive optional/);
});

test('input edits during a request discard the old response',async()=>{
 let resolve;const u=ui(()=>new Promise(r=>resolve=r));await flush();const pending=u.api.calculate();u.get('target').events.input();resolve(response(200,fixture()));await pending;
 assert.doesNotMatch(u.get('projection-chart').innerHTML,/<svg/);
});

test('closed/empty categories keep valid label layout without inventing paths',()=>{
 const u=ui(),data=fixture();const o=data.outcomes['1'];o.most_likely=['middle'];o.trading_hours=0;
 for(const g of o.groups){g.percentage_units=g.key==='middle'?1000:0;g.curve=g.key==='middle'?[100,100]:[];g.endpoint=g.key==='middle'?100:null;}
 u.api.render(data);assert.doesNotMatch(u.get('projection-chart').innerHTML,/NaN|undefined/);assert.match(u.get('projection-chart').innerHTML,/No sampled paths/);assert.match(u.get('outcome-likelihood').textContent,/Sideways 100.0%/);assert.match(u.get('outcome-ranges').textContent,/No scheduled trading/);
});

test('independent scenario remains visible with failed historical gate and changing reference',async()=>{
 const u=ui();await flush();assert.equal(u.get('outlook').textContent,'Model scenario: Short at $99.00/barrel');
 u.get('target').value='105';await u.api.calculate();assert.equal(u.get('outlook').textContent,'Model scenario: Short at $99.00/barrel');
 assert.match(u.get('signal-confidence').textContent,/not established/);
});

test('one-second market checks bypass caching; quote socket updates without an outlook post',async()=>{
 const u=ui();await flush();assert.ok(u.timers.some(t=>t.ms===1000));assert.equal(u.requests.find(r=>r.path==='/api/market').options.cache,'no-store');
 let socket;u.context.location={protocol:'https:',host:'oilpbot.onrender.com'};u.context.WebSocket=class{constructor(url){assert.equal(url,'wss://oilpbot.onrender.com/api/live');socket=this}};u.api.connectQuotes();socket.onmessage({data:JSON.stringify({quote:{...spot,price:101.25}})});
 assert.equal(u.get('price').textContent,'$101.25');assert.equal(posts(u).length,0);
});

test('news stays independent and excludes old/future/undated articles',async()=>{
 const u=ui();await flush();const item={title:'Oil supply story',url:'https://example.com/story',source:'News'};u.api.renderNews({sources:[{name:'News',available:true}],headlines:[{...item,published_utc:new Date(Date.now()-60000).toISOString()},{...item,published_utc:new Date(Date.now()-86400001).toISOString()},{...item,published_utc:'invalid'},{...item,published_utc:new Date(Date.now()+60000).toISOString()}]});
 assert.equal(u.get('news').children.length,1);await u.api.calculate();assert.equal(u.get('news').children.length,1);
});

test('proxy supports outlook and outcome validation with method and response guards',async()=>{
 const saved=process.env.OILPBOT_API_URL,native=globalThis.fetch;
 try{process.env.OILPBOT_API_URL='https://backend.example.com';globalThis.fetch=async()=>response(200,{ok:true});
 assert.equal((await handler({path:'/api/outlook',httpMethod:'POST',body:'{}'})).statusCode,200);
 assert.equal((await handler({path:'/api/outcome-validation',httpMethod:'GET'})).statusCode,200);
 assert.equal((await handler({path:'/api/outlook',httpMethod:'GET'})).statusCode,405);
 assert.equal((await handler({path:'/api/unknown',httpMethod:'GET'})).statusCode,404);
 globalThis.fetch=async()=>response(200,'<html>wrong host</html>','text/html');assert.equal((await handler({path:'/api/outlook',httpMethod:'POST',body:'{}'})).statusCode,502);
 }finally{globalThis.fetch=native;if(saved===undefined)delete process.env.OILPBOT_API_URL;else process.env.OILPBOT_API_URL=saved;}
});
