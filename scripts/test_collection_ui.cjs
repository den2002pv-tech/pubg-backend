// CodeRabbit-generated regression checks; runs actual inline functions with browser boundaries stubbed.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('index.html', 'utf8');
const confirm = source.slice(source.indexOf('async function confirmCollection()'), source.indexOf('// User & Collection Loading'));
const fetchHelper = source.slice(source.indexOf('async function fetchWithTimeout('), source.indexOf('async function wakeBackend()'));
const element = () => ({style: {}, classList: {add(){}, remove(){}}, dataset:{}, setAttribute(){}});
async function exercise(mode) {
  let calls = 0, release;
  const gate = new Promise(resolve => release = resolve);
  const events = [];
  const ctx = {performance, crypto: require('node:crypto').webcrypto, setTimeout, clearTimeout,
    AbortController, TypeError, SyntaxError, Map, Set, API:'', BACKEND_WAKE_TIMEOUT_MS:10,
    collectionSaveInProgress:false, scanInProgress:false, currentScanId:'scan-original',
    pendingCards:[{id:'card_1', selected:true, editQuantity:4, quantity:2}],
    fileInput:element(), confirmBtn:element(), scanSaveStatusEl:element(), document:{getElementById:element},
    desiredIds:new Set(['card_1']), savedDesiredIds:new Set(['card_1']), navigator:{onLine:true},
    telegramHeaders:()=>({}), trackAnalytics(){}, triggerHaptic(){}, checkDesiredChanges(){},
    renderInventory(){}, showToast(){}, flashSavedCards(){}, updateConfirmState(){},
    fetch:async (url, options) => {
      if (url.endsWith('client-result')) { events.push(JSON.parse(options.body)); return {}; }
      calls++;
      assert.deepEqual(JSON.parse(options.body).cards, [{id:'card_1', quantity:4}]);
      await gate;
      if (mode === 'network') throw new TypeError('network');
      return {ok:mode !== 'database', status:mode === 'database' ? 500 : 200,
        json: mode === 'timeout' ? () => new Promise((_, reject) => {
          const fail=()=>reject(Object.assign(new Error('timeout'), {name:'AbortError'}));
          if(options.signal.aborted) fail(); else options.signal.addEventListener('abort',fail);
        }) : async()=>({status:'ok', saved:1, cards:[{id:'card_1',quantity:4}]})};
    }};
  vm.createContext(ctx); vm.runInContext(fetchHelper + confirm, ctx);
  const first = ctx.confirmCollection();
  await ctx.confirmCollection();
  assert.equal(calls,1,'double click must not send another save');
  release(); await first;
  assert.equal(ctx.collectionSaveInProgress,false);
  assert.equal(events[0].confirmed, mode === 'success');
  assert.notEqual(events[0].save_id,ctx.currentScanId);
  if(mode === 'success') {assert.equal(ctx.pendingCards.length,0); assert.equal(ctx.desiredIds.size,0);}
  else {assert.equal(ctx.pendingCards.length,1); assert.equal(ctx.desiredIds.size,1);
    await ctx.confirmCollection(); assert.equal(calls,2,'retry remains available');}
}
(async()=>{for(const mode of ['success','database','network','timeout']) await exercise(mode);
console.log('Collection UI: success, database error, network failure, body timeout, duplicate click and retry passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
