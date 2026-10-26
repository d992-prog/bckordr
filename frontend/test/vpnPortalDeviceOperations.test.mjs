import assert from "node:assert/strict";
import test from "node:test";

import { portalApi, PortalError } from "../src/vpn-portal/api.ts";
import { SessionGeneration } from "../src/vpn-portal/bootstrap.ts";
const module = await import("../src/vpn-portal/deviceOperations.ts").catch((error) => { if (error.code === "ERR_MODULE_NOT_FOUND") return {}; throw error; });
const requestId = "11111111-2222-4333-8444-555555555555";
const intent = {action:"replace", deviceId:21};
function operation(request_id, fields={}) { return {id:requestId,request_id,device_id:21,action:"replace",stage:"revoking",credential_generation:0,endpoint_id:7,public_error:null,created_at:"2026-10-26T00:00:00Z",updated_at:"2026-10-26T00:00:00Z",finished_at:null,...fields}; }

test("duplicate taps have one request and a lost response reuses its UUID", async () => {
  assert.equal(typeof module.DeviceOperationRequests, "function");
  const original = portalApi.replaceDevice;
  let reject;
  const calls=[];
  portalApi.replaceDevice = async (id, uuid) => { calls.push(uuid); if(calls.length===1) return new Promise((_, fail)=>{reject=fail;}); return operation(uuid); };
  try {
    const requests = new module.DeviceOperationRequests(new SessionGeneration(), ()=>{}, ()=>{}, ()=>{});
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

test("unfinished failed and uncertain operations retain their slot and identity", () => {
  assert.equal(typeof module.operationFinished,"function");
  assert.equal(module.operationFinished(operation(requestId,{stage:"failed"})),false);
  assert.equal(module.operationFinished(operation(requestId,{stage:"uncertain"})),false);
  assert.equal(module.operationFinished(operation(requestId,{stage:"completed",finished_at:"2026-10-26T00:01:00Z"})),true);
  const requests=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{});
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
    const requests=new module.DeviceOperationRequests(session,()=>{notifications++;},()=>{},()=>{});
    const pending=requests.submit(intent,"csrf");
    const uuid=requests.entries["replace:21"].requestId;
    session.invalidate(); requests.clear(); const before=notifications;
    finish(operation(uuid,{stage:"completed"})); await pending;
    assert.deepEqual(requests.entries,{}); assert.equal(notifications,before);
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
    const requests=new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{});
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
      const requests = new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{});
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
    const requests = new module.DeviceOperationRequests(new SessionGeneration(),()=>{},()=>{},()=>{});
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
