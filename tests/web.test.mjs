import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {handler} from '../netlify/functions/api.mjs';

const html = readFileSync(new URL('../templates/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function ui(fetch) {
    let state;
    const context = {fetch, AbortSignal, Date, console, setTimeout, clearTimeout, setInterval, clearInterval,
        Vue:{ref:value=>({value}), computed:fn=>({get value(){return fn()}}),onMounted:()=>{},onUnmounted:()=>{},
            createApp:({setup})=>({mount:()=>{state=setup()}})}};
    vm.runInNewContext(script,context);
    return state;
}
const spot = {price:75,symbol:'CL=F',source:'fixture',timestamp_utc:new Date().toISOString()};
const response = (status,body,type='application/json')=>new Response(typeof body==='string'?body:JSON.stringify(body),{status,headers:{'content-type':type}});

test('UI starts disconnected without a fake price',()=>{
    const state=ui();
    assert.equal(state.currentPrice.value,null);
    assert.equal(state.freshnessStatus.value,'Not connected');
});
test('HTML response gives a deployment error and retry recovers',async()=>{
    let n=0;
    const state=ui(async(path)=>{
        assert.equal(path,'/api/predict');
        return ++n===1?response(200,'<!DOCTYPE html>','text/html'):response(200,{spot,simulation:{prob_touch_target:.2}});
    });
    await state.fetchPrediction();
    assert.match(state.errorMessage.value,/web page instead of JSON/);
    assert.equal(state.result.value,null);
    await state.fetchPrediction();
    assert.equal(state.errorMessage.value,'');
    assert.equal(state.currentPrice.value,75);
    assert.equal(state.freshnessStatus.value,'Fresh');
});
test('stale response clears results and permits retry',async()=>{
    let n=0;
    const state=ui(async()=>++n===1?response(503,{detail:'REFUSED: stale'}):response(200,{spot,simulation:{}}));
    await state.fetchPrediction();
    assert.equal(state.errorTitle.value,'Fresh price required');
    assert.equal(state.isStale.value,true);
    await state.fetchPrediction();
    assert.equal(state.isStale.value,false);
});
test('validation failures are readable',async()=>{
    const state=ui(async()=>response(422,{detail:[{msg:'Invalid target'}]}));
    await state.fetchPrediction();
    assert.equal(state.errorMessage.value,'Invalid target');
});
test('Netlify proxy returns JSON for missing config, routing and upstream failures',async()=>{
    const saved=process.env.OILPBOT_API_URL;
    const nativeFetch=globalThis.fetch;
    try {
        delete process.env.OILPBOT_API_URL;
        const event={path:'/.netlify/functions/api/predict',httpMethod:'POST',body:'{"target_price":78}'};
        assert.equal((await handler(event)).statusCode,503);
        assert.equal((await handler({...event,path:'/api/unknown'})).statusCode,404);
        assert.equal((await handler({...event,httpMethod:'GET'})).statusCode,405);
        process.env.OILPBOT_API_URL='https://backend.example.com';
        globalThis.fetch=async(url,options)=>{
            assert.equal(url.href,'https://backend.example.com/api/predict');
            assert.equal(options.body,event.body);
            return response(503,{detail:'stale'});
        };
        assert.deepEqual(JSON.parse((await handler(event)).body),{detail:'stale'});
        globalThis.fetch=async()=>response(200,'<!DOCTYPE html>','text/html');
        assert.equal((await handler(event)).statusCode,502);
        globalThis.fetch=async()=>{throw new Error('offline')};
        assert.equal((await handler(event)).statusCode,502);
    } finally {
        if(saved===undefined) delete process.env.OILPBOT_API_URL; else process.env.OILPBOT_API_URL=saved;
        globalThis.fetch=nativeFetch;
    }
});

test('late prediction responses cannot show stale results as live',async()=>{
    const oldSpot={...spot,timestamp_utc:new Date(Date.now()-120000).toISOString()};
    const state=ui(async()=>response(200,{spot:oldSpot,simulation:{}}));
    await state.fetchPrediction();
    assert.equal(state.result.value,null);
    assert.equal(state.isStale.value,true);
    assert.equal(state.errorTitle.value,'Fresh price required');
});

test('HTML authentication page is reported as a site access problem',async()=>{
    const state=ui(async()=>response(401,'<!DOCTYPE html><title>Login Redirect</title>','text/html'));
    await state.fetchPrediction();
    assert.equal(state.errorTitle.value,'Site access required');
    assert.match(state.errorMessage.value,/Sign in/);
    assert.equal(state.result.value,null);
});

test('delayed mode labels old data and sends the selected mode',async()=>{
    const delayedSpot={...spot,timestamp_utc:new Date(Date.now()-600000).toISOString(),freshness:'delayed'};
    const state=ui(async(path,options)=>{
        assert.equal(JSON.parse(options.body).data_mode,'delayed');
        return response(200,{spot:delayedSpot,simulation:{}});
    });
    state.form.value.data_mode='delayed';
    await state.fetchPrediction();
    assert.equal(state.freshnessStatus.value,'Delayed data');
    assert.equal(state.isStale.value,false);
    assert.ok(state.result.value);
    state.form.value.data_mode='strict';
    state.resetMode();
    assert.equal(state.result.value,null);
    assert.equal(state.isStale.value,true);
});
test('quotes beyond fifteen minutes are refused even in delayed mode',async()=>{
    const old={...spot,timestamp_utc:new Date(Date.now()-901000).toISOString()};
    const state=ui(async()=>response(200,{spot:old,simulation:{}}));
    state.form.value.data_mode='delayed';
    await state.fetchPrediction();
    assert.equal(state.result.value,null);
    assert.equal(state.isStale.value,true);
});

test('Netlify forwards delayed quote mode to the backend',async()=>{
    const saved=process.env.OILPBOT_API_URL;
    const nativeFetch=globalThis.fetch;
    try {
        process.env.OILPBOT_API_URL='https://backend.example.com';
        globalThis.fetch=async(url)=>{
            assert.equal(url.href,'https://backend.example.com/api/quote?data_mode=delayed');
            return response(200,{spot});
        };
        const r=await handler({path:'/api/quote',httpMethod:'GET',queryStringParameters:{data_mode:'delayed'}});
        assert.equal(r.statusCode,200);
    } finally {
        if(saved===undefined) delete process.env.OILPBOT_API_URL; else process.env.OILPBOT_API_URL=saved;
        globalThis.fetch=nativeFetch;
    }
});
