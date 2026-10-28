import {test,expect,type Page} from '@playwright/test';
import type {Snapshot} from '../src/types';

async function unfinishedSave(page:Page, status='restart_required') {
  const snapshot:Snapshot={profiles:[],project:null,origin:'',dirty:false,file_name:null,zoom_percent:100,compiled:null,
    controller:{devices:[],connected:false,busy:false,identity:null,summary:'',preview:null,status:'Ready',can_write:false,
      pending_save:{status,backup_name:'saved-configuration',restored:false,
        message:status==='restart_required'?'Keep the display off for 10 seconds, reconnect in U, then verify.':'Keep the pedal powered and connected in U. Restore backup before restarting.'}}};
  const calls:string[]=[];
  let restarted=false;
  await page.exposeFunction('savedControllerRequest',(method:string)=>{
    calls.push(method);
    if(method==='controller_verify'){
      if(!restarted)return {ok:false,error:{message:'A previous save still needs a physical restart.'}};
      snapshot.controller.pending_save=null;
      snapshot.controller.status='Project verified in permanent memory after restart. Normal saving is active.';
    }
    if(method==='controller_restore'){
      snapshot.controller.pending_save={status:'restart_required',backup_name:'saved-configuration',restored:true,message:'Original flash restored. Restart the pedal, then verify.'};
    }
    return {ok:true,data:structuredClone(snapshot)};
  });
  await page.addInitScript(()=>{
    (window as unknown as {__STOMPILOT_REQUEST__:unknown}).__STOMPILOT_REQUEST__=(method:string)=>(window as unknown as {savedControllerRequest:(method:string)=>unknown}).savedControllerRequest(method);
  });
  await page.goto('/');
  await expect(page.getByTestId('controller-pending-save')).toBeVisible();
  return {calls,restart:()=>{restarted=true;}};
}

test('unfinished save is visible after reopening and remains pending until restart verification succeeds',async({page})=>{
  const fixture=await unfinishedSave(page);
  await expect(page.getByTestId('controller-preview')).toBeDisabled();
  await page.getByTestId('controller-verify').click();
  await expect(page.getByTestId('controller-pending-save')).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('physical restart');
  fixture.restart();
  await page.getByTestId('controller-verify').click();
  await expect(page.getByTestId('controller-pending-save')).toHaveCount(0);
  await expect(page.getByTestId('controller-status')).toContainText('Normal saving is active');
  expect(fixture.calls.filter(x=>x==='controller_verify')).toHaveLength(2);
});

test('restoring an interrupted save has a concrete confirmation and still requires restart verification',async({page})=>{
  const fixture=await unfinishedSave(page,'recovery_required');
  await expect(page.getByTestId('controller-pending-save')).toContainText('Keep the pedal powered');
  await page.getByTestId('controller-restore').click();
  await expect(page.getByTestId('confirmation-dialog')).toContainText('complete configuration backed up before it');
  await page.getByTestId('confirm-cancel').click();
  expect(fixture.calls).not.toContain('controller_restore');
  await page.getByTestId('controller-restore').click();
  await page.getByTestId('confirm-accept').click();
  await expect(page.getByTestId('controller-pending-save')).toContainText('Original flash restored');
  expect(fixture.calls.filter(x=>x==='controller_restore')).toHaveLength(1);
  await expect(page.getByTestId('controller-verify')).toBeEnabled();
});
