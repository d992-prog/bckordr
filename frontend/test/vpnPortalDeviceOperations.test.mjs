import assert from "node:assert/strict";
import test from "node:test";

import { portalApi, PortalError } from "../src/vpn-portal/api.ts";
import { SessionGeneration } from "../src/vpn-portal/bootstrap.ts";
const module = await import("../src/vpn-portal/deviceOperations.ts").catch((error) => { if (error.code === "ERR_MODULE_NOT_FOUND") return {}; throw error; });
const requestId = "11111111-2222-4333-8444-555555555555";
const intent = {action:"replace", deviceId:21};
function operation(request_id, fields={}) { return {id:requestId,request_id,device_id:21,action:"replace",stage:"revoking",credential_generation:0,endpoint_id:7,public_error:null,created_at:"2026-10-26T00:00:00Z",updated_at:"2026-10-26T00:00:00Z",finished_at:null,...fields}; }

function memoryStorage() {
  const values = new Map();
  return {values,getItem:key=>values.get(key)??null,setItem:(key,value)=>{values.set(key,value);},removeItem:key=>{values.delete(key);}};
}

async function initializedRequests(session=new SessionGeneration(),changed=()=>{},unauthorized=()=>{},starting=()=>{}) {
  const storage=memoryStorage();
  const requests=new module.DeviceOperationRequests(session,changed,unauthorized,starting,()=>storage);
  await requests.initialize("csrf");return requests;
}

test("an unresolved creation survives reload before any device metadata exists", async () => {
  const storage=memoryStorage(),original=portalApi.createDevice,calls=[];
  const create={action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"};
  portalApi.createDevice=async(payload)=>{calls.push(payload);if(calls.length===1)throw new PortalError(0,"lost response");return operation(payload.request_id,{action:"create",stage:"completed",finished_at:"2026-10-26T00:01:00Z"});};
  try {
    const first=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
    await first.initialize("stable-private-csrf");
    await first.submit(create,"stable-private-csrf");
    const persisted=storage.getItem("veltrix.device-requests.v1");
    assert.ok(persisted);assert.equal(persisted.includes("stable-private-csrf"),false);
    const second=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
    await second.initialize("stable-private-csrf");second.restore([]);
    assert.equal(second.entries.create.requestId,calls[0].request_id);
    await second.submit(create,"stable-private-csrf");
    assert.deepEqual(calls[0],calls[1]);
    assert.equal(storage.getItem("veltrix.device-requests.v1"),null,"settled requests leave no journal record");
  } finally {portalApi.createDevice=original;}
});

test("journal identities cannot cross authenticated sessions or explicit logout", async () => {
  const storage=memoryStorage(),original=portalApi.createDevice;
  portalApi.createDevice=async()=>{throw new PortalError(0,"lost");};
  try {
    const first=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
    await first.initialize("session-one");await first.submit({action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"},"session-one");
    const second=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
    await second.initialize("session-two");assert.deepEqual(second.entries,{});assert.equal(storage.values.size,0);
    await second.submit({action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"},"session-two");
    assert.equal(storage.values.size,1);second.clear();assert.equal(storage.values.size,0);
  } finally {portalApi.createDevice=original;}
});

test("storage write failure prevents dispatch without exposing its exception", async () => {
  const storage=memoryStorage(),original=portalApi.createDevice;let calls=0;
  storage.setItem=()=>{throw new Error("private-storage-secret");};
  portalApi.createDevice=async()=>{calls++;};
  try {
    const requests=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
    await requests.initialize("private-csrf");
    await requests.submit({action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"},"private-csrf");
    assert.equal(calls,0);assert.ok(requests.entries.create.error);
    assert.equal(JSON.stringify(requests.entries).includes("private-storage-secret"),false);
    assert.equal(storage.values.size,0);
  } finally {portalApi.createDevice=original;}
});

test("untrusted journal fields, duplicate identities and oversized data fail mutations closed", async () => {
  const storage=memoryStorage(),original=portalApi.createDevice;let calls=0;
  const csrf="private-csrf",scope=Buffer.from(await crypto.subtle.digest("SHA-256",new TextEncoder().encode("veltrix-device-requests-v1:"+csrf))).toString("hex");
  const create={action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"};
  const record={key:"create",requestId,intent:create};
  const envelope={version:1,scope,requests:[record]};
  const invalid=["not-json","x".repeat(32769),JSON.stringify({...envelope,extra:"private-secret"}),
    ...[
      {...record,key:"__proto__"},{...record,key:"create:0"},{...record,requestId:"00000000-0000-0000-0000-000000000000"},
      {...record,requestId:"not-a-uuid"},{...record,operation:{subscription_url:"https://private-secret"}},
      {...record,intent:{...create,subscription_id:0}},{...record,intent:{...create,endpoint_id:2147483648}},
      {...record,intent:{...create,display_name:"x".repeat(65)}},{...record,intent:{...create,display_name:"\nprivate-secret"}},
      {...record,intent:{...create,csrf_token:"private-secret"}},{...record,intent:{action:"revoke",deviceId:21}},
    ].map(bad=>JSON.stringify({...envelope,requests:[bad]})),
    JSON.stringify({...envelope,requests:[record,record]}),
    JSON.stringify({...envelope,requests:[record,{...record,key:"create:31"}]}),
    JSON.stringify({...envelope,requests:Array(33).fill(record)})];
  portalApi.createDevice=async()=>{calls++;};
  try {
    for(const raw of invalid){
      storage.setItem("veltrix.device-requests.v1",raw);
      const requests=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
      await requests.initialize(csrf);assert.deepEqual(requests.entries,{});
      await requests.submit(create,csrf);
      assert.equal(calls,0);assert.ok(requests.entries.create.error);
      assert.equal(JSON.stringify(requests.entries).includes("private-secret"),false);
    }
  } finally {portalApi.createDevice=original;}
});

test("unavailable storage and initialization invalidation never dispatch or restore private state", async () => {
  const original=portalApi.createDevice;let calls=0;
  portalApi.createDevice=async()=>{calls++;};
  try {
    const requests=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>{throw new Error("private-storage-secret");});
    await requests.initialize("csrf");
    await requests.submit({action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"},"csrf");
    assert.equal(calls,0);assert.ok(requests.entries.create.error);assert.equal(JSON.stringify(requests.entries).includes("private-storage-secret"),false);
    const storage=memoryStorage(),session=new SessionGeneration();
    const next=new module.DeviceOperationRequests(session,()=>{},()=>{},()=>{},()=>storage);
    const pending=next.initialize("csrf");session.invalidate();next.clear();await pending;
    assert.deepEqual(next.entries,{});assert.equal(storage.values.size,0);
    await next.submit(intent,"csrf");assert.equal(calls,0);
  } finally {portalApi.createDevice=original;}
});

test("authoritative device metadata settles a recovered journal without replaying the POST", async () => {
  const storage=memoryStorage(),original=portalApi.replaceDevice;
  portalApi.replaceDevice=async()=>{throw new PortalError(0,"lost response");};
  try {
    const first=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
    await first.initialize("csrf");await first.submit(intent,"csrf");
    const second=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{},()=>storage);
    await second.initialize("csrf");
    second.restore([{id:21,operation:operation(first.entries["replace:21"].requestId,{stage:"completed",finished_at:"2026-10-26T00:01:00Z"})}]);
    assert.equal(second.entries["replace:21"].definitive,true);assert.equal(storage.values.size,0);
  } finally {portalApi.replaceDevice=original;}
});

test("duplicate taps have one request and a lost response reuses its UUID", async () => {
  assert.equal(typeof module.DeviceOperationRequests, "function");
  const original = portalApi.replaceDevice;
  let reject;
  const calls=[];
  portalApi.replaceDevice = async (id, uuid) => { calls.push(uuid); if(calls.length===1) return new Promise((_, fail)=>{reject=fail;}); return operation(uuid); };
  try {
    const requests = await initializedRequests();
    const first=requests.submit(intent,"csrf");
    await requests.submit(intent,"csrf");
    assert.equal(calls.length,1);
    reject(new PortalError(0,"network"));
    await first;
    assert.equal(requests.pendingFor(21),true);
    await requests.submit(intent,"csrf");
    assert.equal(calls.length,2);
    assert.equal(calls[0],calls[1]);
  } finally { portalApi.replaceDevice=original; }
});

test("unfinished failed and uncertain operations retain their slot and identity", async () => {
  assert.equal(typeof module.operationFinished,"function");
  assert.equal(module.operationFinished(operation(requestId,{stage:"failed"})),false);
  assert.equal(module.operationFinished(operation(requestId,{stage:"uncertain"})),false);
  assert.equal(module.operationFinished(operation(requestId,{stage:"completed",finished_at:"2026-10-26T00:01:00Z"})),true);
  const requests=await initializedRequests();
  requests.restore([{id:21,subscription_id:11,display_name:"Ноутбук",operation:operation(requestId,{stage:"uncertain"})}]);
  assert.equal(requests.pendingFor(21),true);
  assert.equal(requests.entries["replace:21"].requestId,requestId);
});

test("late operation completion cannot cross logout or a different request binding", async () => {
  assert.equal(typeof module.DeviceOperationRequests,"function");
  const original=portalApi.replaceDevice;
  let finish;
  portalApi.replaceDevice=()=>new Promise((resolve)=>{finish=resolve;});
  try {
    const session=new SessionGeneration();
    let notifications=0;
    const requests=await initializedRequests(session,()=>{notifications++;});
    const pending=requests.submit(intent,"csrf");
    const uuid=requests.entries["replace:21"].requestId;
    session.invalidate(); requests.clear(); const before=notifications;
    finish(operation(uuid,{stage:"completed"})); await pending;
    assert.deepEqual(requests.entries,{}); assert.equal(notifications,before);
    await requests.initialize("csrf");
    portalApi.replaceDevice=async()=>operation(requestId,{device_id:999});
    await requests.submit(intent,"csrf");
    assert.equal(requests.entries["replace:21"].operation,null);
    assert.equal(requests.pendingFor(21),true);
  } finally { portalApi.replaceDevice=original; }
});

test("creation retries preserve the exact payload and reject edits after an unknown response", async () => {
  assert.equal(typeof module.DeviceOperationRequests,"function");
  const original=portalApi.createDevice;
  const calls=[];
  portalApi.createDevice=async(payload)=>{calls.push(payload);throw new PortalError(0,"network");};
  try {
    const requests=await initializedRequests();
    const create={action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"};
    await requests.submit(create,"csrf");
    await requests.submit({...create,display_name:"Другое"},"csrf");
    assert.equal(calls.length,1);
    await requests.submit(create,"csrf");
    assert.deepEqual(calls[0],calls[1]);
  } finally {portalApi.createDevice=original;}
});

for (const action of ["create", "revoke", "replace"]) {
  test(`${action} retains an unresolved UUID after a timeout and rejected replay`, async () => {
    const method = action === "create" ? "createDevice" : action === "revoke" ? "revokeDevice" : "replaceDevice";
    const original = portalApi[method];
    const uuids = [];
    const submitted = action === "create"
      ? {action,subscription_id:11,endpoint_id:7,display_name:"Телефон"}
      : {action,deviceId:21};
    portalApi[method] = async (payload, uuid) => {
      const request = action === "create" ? payload.request_id : uuid;
      uuids.push(request);
      if (uuids.length === 1) throw new PortalError(0,"network timeout");
      if (uuids.length === 2) throw new PortalError(429,"rate limited");
      return operation(request,{action,stage:"completed",finished_at:"2026-10-26T00:01:00Z"});
    };
    try {
      const requests = await initializedRequests();
      const key = module.operationKey(submitted);
      await requests.submit(submitted,"csrf");
      await requests.submit(submitted,"csrf");
      assert.equal(requests.entries[key].definitive,false,"rejected replay cannot settle the first unknown submission");
      if (action === "create") {
        await requests.submit({...submitted,display_name:"Другое имя"},"csrf");
        assert.equal(uuids.length,2,"the unresolved payload cannot be replaced after a rejected replay");
      }
      await requests.submit(submitted,"csrf");
      assert.equal(uuids.length,3);
      assert.deepEqual(uuids,[uuids[0],uuids[0],uuids[0]]);
      assert.equal(requests.entries[key].definitive,true,"an authoritative completed operation settles the submission");
    } finally { portalApi[method] = original; }
  });
}

test("an initial definitive validation rejection allows an edited payload with a fresh UUID", async () => {
  const original = portalApi.createDevice;
  const payloads = [];
  portalApi.createDevice = async (payload) => {
    payloads.push(payload);
    if (payloads.length === 1) throw new PortalError(422,"invalid country");
    return operation(payload.request_id,{action:"create",stage:"completed",finished_at:"2026-10-26T00:01:00Z"});
  };
  try {
    const requests = await initializedRequests();
    const create = {action:"create",subscription_id:11,endpoint_id:7,display_name:"Телефон"};
    await requests.submit(create,"csrf");
    assert.equal(requests.entries.create.definitive,true);
    await requests.submit({...create,endpoint_id:8},"csrf");
    assert.equal(payloads.length,2);
    assert.notEqual(payloads[0].request_id,payloads[1].request_id);
    assert.equal(payloads[1].endpoint_id,8);
  } finally { portalApi.createDevice = original; }
});

test("UUID fallback uses cryptographic random bytes and produces a version-four identifier", () => {
  assert.equal(typeof module.operationRequestId,"function");
  let called=0;
  const uuid=module.operationRequestId({getRandomValues(bytes){called++;bytes.fill(0);return bytes;}});
  assert.equal(called,1);
  assert.match(uuid,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(uuid,"00000000-0000-0000-0000-000000000000");
});

test("a completed mutation cannot prepare credentials from a stale device snapshot", () => {
  assert.equal(typeof module.deviceConnectionBlocked, "function");
  const device = {id:21,credential_generation:1,state:"active",operation:null};
  const completed = operation(requestId,{stage:"completed",credential_generation:2,finished_at:"2026-10-26T00:01:00Z"});
  const requests = {"replace:21":{intent,operation:completed,definitive:true,busy:false}};
  assert.equal(module.deviceConnectionBlocked(device,requests),true);
  assert.equal(module.deviceConnectionBlocked({...device,credential_generation:2},requests),false);
  assert.equal(module.deviceConnectionBlocked({...device,id:22},requests),false);
  const revoke = {"revoke:21":{intent:{action:"revoke",deviceId:21},operation:{...completed,action:"revoke"},definitive:true,busy:false}};
  assert.equal(module.deviceConnectionBlocked({...device,credential_generation:2},revoke),true);
  assert.equal(module.deviceConnectionBlocked({...device,state:"revoked",credential_generation:2},revoke),false);
  assert.equal(module.deviceConnectionBlocked({...device,operation:{...completed,stage:"uncertain",finished_at:null}},{}),true);
});
