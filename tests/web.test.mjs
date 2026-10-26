import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {handler} from '../netlify/functions/api.mjs';
const script=readFileSync(new URL('../templates/app.js',import.meta.url),'utf8');
const spot={price:75,symbol:'TVC:USOIL',timestamp_utc:new Date().toISOString(),status:'fresh',market_open:true,usable:true};
const response=(status,body,type='application/json')=>new Response(typeof body==='string'?body:JSON.stringify(body),{status,headers:{'content-type':type}});
function ui(fetchForecast){
 const timers=[], marketRequests=[];
 const elements={};
 const get=id=>elements[id]??=( {value:id==='leverage'?'5':id==='target'?'78':id==='chart-horizon'?'168':'',textContent:'',innerHTML:'',hidden:false,dataset:{},disabled:false,events:{},children:[],append(...nodes){this.children.push(...nodes)},replaceChildren(){this.innerHTML='';this.children=[]},addEventListener(type,fn){this.events[type]=fn},setAttribute(key,value){this[key]=value}} );
 const context=vm.createContext({document:{getElementById:get,createElement:()=>({textContent:'',children:[],append(...nodes){this.children.push(...nodes)}})},fetch:async(path,options)=>path==='/api/market'?(marketRequests.push(options),response(200,{quote:spot})):path==='/api/news'?response(200,{sources:[],headlines:[]}):fetchForecast(path,options),AbortSignal,Date,console,setTimeout:(fn,ms)=>{timers.push({fn,ms});return 0},setInterval:()=>0});
 vm.runInContext(script,context);
 const api=vm.runInContext('({calculate,render,clearResults,projectionChart,renderNews,refreshNews})',context);
 return {get,api,context,timers,marketRequests};
}
const fixture=()=>({quote:spot,target_price:78,price_change_percent:4,leverage:5,leveraged_return_percent:20,horizons:[1,3,5,24,168].map((hours,i)=>({hours,label:['1 hour','3 hours','5 hours','1 day','1 week'][i],probability:.1*i})),projection:Array.from({length:169},(_,hours)=>({hours,median:75,low:74-hours/100,high:76+hours/100})),model:{basis:'Range model',as_of_utc:new Date().toISOString()}});
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('only target, direction and leverage are sent; all charts render',async()=>{
 const {get,api}=ui(async(path,options)=>{assert.equal(path,'/api/forecast');assert.deepEqual(JSON.parse(options.body),{target_price:78,direction:'long',leverage:5});return response(200,fixture())});
 await api.calculate();
 assert.match(get('cards').innerHTML,/1 week/);
 assert.match(get('projection-chart').innerHTML,/<svg/);
 assert.match(get('probability-chart').innerHTML,/40.0%/);
 assert.match(get('summary').textContent,/20.00%/);
 assert.equal(get('submit').disabled,false);
});
test('failed requests show errors and retry recovers',async()=>{
 let n=0;const {get,api}=ui(async()=>++n===1?response(503,{detail:'Source unavailable'}):response(200,fixture()));
 await api.calculate();assert.equal(get('error').textContent,'Source unavailable');
 await api.calculate();assert.equal(get('error').hidden,true);assert.match(get('cards').innerHTML,/40.0%/);
});
test('HTML responses are handled without a JSON parsing crash',async()=>{
 const {get,api}=ui(async()=>response(200,'<!doctype html>','text/html'));
 await api.calculate();assert.match(get('error').textContent,/host did not return an API/);
});
test('changing direction clears prior results',async()=>{
 const {get,api}=ui(async()=>response(200,fixture()));await api.calculate();
 get('short').events.click();assert.doesNotMatch(get('cards').innerHTML,/40.0%/);assert.equal(get('short')['aria-pressed'],'true');
});
test('market-closed quotes are labelled and not called live',async()=>{
 const data=fixture();data.quote={...spot,status:'market_closed',market_open:false};
 const {get,api}=ui(async()=>response(200,data));await flush();await api.calculate();
 assert.match(get('status').textContent,/Market closed/);assert.equal(get('closed-note').hidden,false);
});
test('input changes during a request discard its response',async()=>{
 let resolve;const {get,api}=ui(()=>new Promise(r=>resolve=r));
 const pending=api.calculate();get('target').events.input();resolve(response(200,fixture()));await pending;
 assert.doesNotMatch(get('cards').innerHTML,/40.0%/);
});
test('proxy supports the new market and forecast endpoints',async()=>{
 const saved=process.env.OILPBOT_API_URL,nativeFetch=globalThis.fetch;
 try{
  process.env.OILPBOT_API_URL='https://backend.example.com';
  globalThis.fetch=async(url)=>{assert.match(url.pathname,/\/api\/(market|forecast|news)/);return response(200,{ok:true})};
  assert.equal((await handler({path:'/api/market',httpMethod:'GET'})).statusCode,200);
  assert.equal((await handler({path:'/api/news',httpMethod:'GET'})).statusCode,200);
  assert.equal((await handler({path:'/api/forecast',httpMethod:'POST',body:'{}'})).statusCode,200);
  assert.equal((await handler({path:'/api/unknown',httpMethod:'GET'})).statusCode,404);
  globalThis.fetch=async()=>response(200,'<!doctype html>','text/html');
  assert.equal((await handler({path:'/api/market',httpMethod:'GET'})).statusCode,502);
 }finally{globalThis.fetch=nativeFetch;if(saved===undefined)delete process.env.OILPBOT_API_URL;else process.env.OILPBOT_API_URL=saved}
});

test('projection uses local date/time ticks and interpolated crossing marker',()=>{
 const {api}=ui(async()=>response(200,fixture()));const data=fixture();
 data.projection=[{hours:0,low:75,median:75,high:75},{hours:1,low:74,median:76,high:79}];
 const markup=api.projectionChart(data,1);
 assert.match(markup,/data-crossing="high"/);
 assert.match(markup,/Scenario crossing/);
 assert.doesNotMatch(markup,/\+1 hours/);
 data.target_price=100;
 assert.match(api.projectionChart(data,1),/No displayed scenario crosses/);
});


test('quotes poll every ten seconds without browser caching or redundant forecasts',async()=>{
 let forecasts=0;
 const {api,timers,marketRequests}=ui(async()=>{forecasts++;return response(200,fixture())});
 await flush();
 assert.equal(timers.findLast(t=>t.ms===10000).ms,10000);
 assert.equal(marketRequests[0].cache,'no-store');
 await api.calculate();
 await timers.findLast(t=>t.ms===10000).fn();
 assert.equal(forecasts,1);
 assert.equal(timers.findLast(t=>t.ms===10000).ms,10000);
});

test('a changed quote recalculates the forecast and an unusable quote clears it',async()=>{
 let forecasts=0;
 const {api,get,timers}=ui(async()=>{forecasts++;return response(200,fixture())});
 await flush();await api.calculate();
 try {
  spot.price=75.5;
  await timers.findLast(t=>t.ms===10000).fn();await flush();
  assert.equal(forecasts,2);
  spot.usable=false;
  await timers.findLast(t=>t.ms===10000).fn();
  assert.doesNotMatch(get('cards').innerHTML,/40.0%/);
  assert.equal(forecasts,2);
 } finally {spot.price=75;spot.usable=true;}
});


test('independent news remains visible when forecasts are cleared',async()=>{
 const {get,api,timers}=ui(async()=>response(503,{detail:'Stale quote'}));
 await flush();
 api.renderNews({sources:[{name:'EIA',available:true}],checked_at_utc:new Date().toISOString(),headlines:[{title:'Crude oil inventories',url:'https://www.eia.gov/story',source:'EIA',published_utc:new Date().toISOString(),used_for_risk:false}]});
 assert.match(get('news-status').textContent,/1\/1 feeds reachable/);
 assert.equal(get('news').children.length,1);
 assert.match(get('news').children[0].children[1].textContent,/Background context/);
 await api.calculate();
 assert.equal(get('news').children.length,1);
 assert.ok(timers.some(t=>t.ms===60000));
});

test('official quote widget requests the exact TVC instrument and labels the backend reference',()=>{
 const html=readFileSync(new URL('../templates/index.html',import.meta.url),'utf8');
 assert.match(html,/https:\/\/s3.tradingview.com\/external-embedding\/embed-widget-single-quote.js/);
 assert.match(html,/"symbol":"TVC:USOIL"/);
 assert.match(html,/Forecast reference quote/);
});
