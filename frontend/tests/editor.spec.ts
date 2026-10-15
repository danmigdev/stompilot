import {test,expect,type Page} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
import type {Project,Snapshot} from '../src/types';

const repository=resolve(fileURLToPath(new URL('../..',import.meta.url)));
const tankProfile=JSON.parse(readFileSync(resolve(repository,'midi_configurator/profiles/tank_mini_v09.json'),'utf8'));
const example=JSON.parse(readFileSync(resolve(repository,'examples/tank_mini_boost_reload.json'),'utf8'));
const python=resolve(repository,'.venv/Scripts/python.exe');
function compile(project:Project,profiles=[tankProfile]) {
  return JSON.parse(execFileSync(python,['-c',"import sys,json; from midi_configurator.core import compile_config; data=json.load(sys.stdin); print(json.dumps(compile_config(data['project'],{p['id']:p for p in data['profiles']})))"],{cwd:repository,input:JSON.stringify({project,profiles}),encoding:'utf8'}));
}
function emptyProject():Project{return {schema_version:1,name:'New project',controller:{id:'chocolate_plus',max_sysex_bytes:127},pages:[{name:'Page 1',switches:Object.fromEntries('ABCD'.split('').map(id=>[id,{label:`Switch ${id}`,mode:'single',states:[{label:'Sequence',actions:[]}]}]))}]};}
async function fixture(page:Page,initial:Project|null=null,profiles=[tankProfile],slowReadFailure=false) {
  let snapshot:Snapshot={profiles,project:initial,compiled:initial?compile(initial,profiles):null,dirty:false,origin:initial?'Explicit browser test fixture':'No project',file_name:null,zoom_percent:100,controller:{devices:[],connected:false,busy:false,identity:null,summary:'',preview:null,status:'Explicit read-only test fixture',can_import:false,can_write:false}};
  const calls:string[]=[];
  await page.exposeFunction('stompilotTestRequest',async(method:string,payload:Record<string,unknown>={})=>{
    calls.push(method);
    try {
      if(method==='new_project'){snapshot.project=emptyProject();snapshot.dirty=false;snapshot.origin='Empty project';snapshot.file_name=null;}
      if(method==='open_project'){snapshot.project=structuredClone(example);snapshot.dirty=false;snapshot.origin='Saved test fixture';snapshot.file_name='fixture-project.json';}
      if(method==='set_project'){const p=payload.project as Project;const compiled=compile(p,profiles);snapshot.project=p;snapshot.compiled=compiled;snapshot.dirty=true;snapshot.controller.preview=null;}
      if(method==='save_project'){snapshot.dirty=false;snapshot.file_name='saved-test-fixture.json';}
      if(method==='set_zoom')snapshot.zoom_percent=Number(payload.percent);
      if(method==='controller_scan')snapshot.controller.devices=[{name:'Fixture Chocolate Plus (read only)',input_name:'Fixture input',output_name:'Fixture output'}];
      if(method==='controller_read'){if(slowReadFailure){snapshot.controller.busy=true;await new Promise(resolve=>setTimeout(resolve,850));snapshot.controller.busy=false;throw Error('Explicit fixture read failed.');}snapshot.controller.connected=true;snapshot.controller.identity={name:'Fixture Chocolate Plus',firmware_version:16};snapshot.controller.status='Read-only fixture loaded';snapshot.controller.summary='Explicit fixture · 1 page · A–D switches';snapshot.controller.can_import=true;}
      if(method==='controller_import'){snapshot.project=structuredClone(example);snapshot.dirty=false;snapshot.origin='Controller fixture import';}
      if(method==='controller_preview'){snapshot.controller.preview={summaries:['Explicit fixture preview: switch A'],warnings:[],changed_bytes:12};snapshot.controller.can_write=false;}
      if(method==='controller_write')throw Error('Hardware writing is disabled in browser tests.');
      if(method==='controller_disconnect'){snapshot.controller.connected=false;snapshot.controller.identity=null;snapshot.controller.can_import=false;}
      if(snapshot.project&&method!=='set_project')snapshot.compiled=compile(snapshot.project,profiles);
      return {ok:true,data:structuredClone(snapshot)};
    }catch(error){return {ok:false,error:{message:(error as Error).message}};}
  });
  await page.addInitScript(()=>{(window as unknown as {__STOMPILOT_REQUEST__:unknown}).__STOMPILOT_REQUEST__=(method:string,payload:unknown)=>(window as unknown as {stompilotTestRequest:(method:string,payload:unknown)=>unknown}).stompilotTestRequest(method,payload);});
  await page.goto('/');
  await expect(page.getByTestId('zoom-value')).toHaveText('100%');
  await expect(page.getByText('Starting Stompilot…')).toHaveCount(0);
  return {get:()=>snapshot,calls};
}
async function addEnable(page:Page,off=false){await page.getByTestId('add-action').click();if(off)await page.getByTestId('effect-off').click();await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);}
async function noOverflow(page:Page){const size=await page.evaluate(()=>({viewport:document.documentElement.clientWidth,body:document.body.scrollWidth,html:document.documentElement.scrollWidth,main:document.querySelector('.main-content')!.scrollWidth,mainClient:document.querySelector('.main-content')!.clientWidth}));expect(size.body).toBeLessThanOrEqual(size.viewport);expect(size.html).toBeLessThanOrEqual(size.viewport);expect(size.main).toBeLessThanOrEqual(size.mainClient+1);}

test('startup requires an explicit source and never loads a demo',async({page})=>{
  const state=await fixture(page);
  await expect(page.getByTestId('startup-open')).toBeVisible();await expect(page.getByTestId('startup-read')).toBeVisible();await expect(page.getByTestId('startup-new')).toBeVisible();
  expect(state.get().project).toBeNull();expect(state.calls).toEqual(['bootstrap']);
  await page.getByTestId('startup-new').click();await expect(page.getByTestId('project-name')).toHaveValue('New project');expect(state.get().dirty).toBe(false);await expect(page.getByTestId('action-0')).toHaveCount(0);
});
test('adds and edits on/off and parameter actions, then reorders exact compiled output',async({page})=>{
  const state=await fixture(page,emptyProject());await addEnable(page,true);
  expect(state.get().project!.pages[0].switches.A.states[0].actions[0].value).toBe(false);
  await page.getByTestId('add-action').click();await page.getByTestId('action-kind').selectOption('parameter');await page.getByTestId('parameter-value').fill('35');await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);
  await page.getByRole('button',{name:'Move action 2 up',exact:true}).click();await expect.poll(()=>state.get().project!.pages[0].switches.A.states[0].actions[0].type).toBe('parameter');
  await page.getByTestId('action-edit-1').click();await page.getByTestId('effect-on').click();await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);
  expect(state.get().project!.pages[0].switches.A.states[0].actions[1].value).toBe(true);await expect(page.getByTestId('hex-preview')).toContainText('F0');expect(state.get().dirty).toBe(true);
});
test('preserves imported CC/SysEx bytes and native-bank metadata until intentional edits',async({page})=>{
  const project=emptyProject();project.pages[0].switches.A.states[0]={label:'Imported CC',actions:[{type:'raw_midi',message_hex:'B3 11 40',custom_metadata:'retain'}],native_bank:{rows_hex:'00',sysex_hex:'00'}};
  project.pages[0].switches.B.states[0]={label:'Imported SysEx',actions:[{type:'raw_midi',message_hex:'F0 00 59 01 02 F7'}]};
  const state=await fixture(page,project);await page.getByTestId('action-edit-0').click();await page.getByRole('button',{name:'Cancel',exact:true}).click();expect(state.get().project!.pages[0].switches.A.states[0].actions[0].message_hex).toBe('B3 11 40');
  await page.getByTestId('action-edit-0').click();await page.getByTestId('raw-value').fill('65');await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);expect(state.get().project!.pages[0].switches.A.states[0].actions[0].message_hex).toBe('B3 11 41');expect(state.get().project!.pages[0].switches.A.states[0].native_bank).toEqual(project.pages[0].switches.A.states[0].native_bank);
  await page.getByTestId('switch-B').click();await page.getByTestId('action-edit-0').click();await expect(page.getByTestId('raw-hex')).toHaveValue('F0 00 59 01 02 F7');await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);expect(state.get().project!.pages[0].switches.B.states[0].actions[0].message_hex).toBe('F0 00 59 01 02 F7');
});
test('experimental reload stays the sole action in its state',async({page})=>{
  const state=await fixture(page,emptyProject());await page.getByTestId('add-action').click();await page.getByTestId('action-kind').selectOption('reload_current_preset');await expect(page.getByText('Experimental · stock TANK Mini V09')).toBeVisible();await page.getByTestId('action-save').click();await expect(page.getByTestId('add-action')).toBeDisabled();expect(state.get().project!.pages[0].switches.A.states[0].actions[0].type).toBe('reload_current_preset');
});
test('renames project/switch/state, alternates two states and limits pages to eight',async({page})=>{
  const state=await fixture(page,emptyProject());for(const [id,text] of [['project-name','My live rig'],['switch-name','Boost'],['state-name','Boost ON']]){await page.getByTestId(id).fill(text);await page.getByTestId(id).press('Tab');await expect.poll(()=>state.get().dirty).toBe(true);await expect(page.getByTestId('page-add')).toBeEnabled();}
  await page.getByTestId('switch-mode').selectOption('alternating');await expect(page.getByTestId('state-1')).toBeVisible();await page.getByTestId('state-1').click();await addEnable(page);await page.getByTestId('switch-mode').selectOption('single');await expect(page.getByTestId('confirmation-dialog')).toBeVisible();await page.getByTestId('confirm-cancel').click();expect(state.get().project!.pages[0].switches.A.mode).toBe('alternating');
  for(let i=1;i<8;i++){await page.getByTestId('page-add').click();await expect(page.getByTestId(`page-${i}`)).toBeVisible();}await expect(page.getByTestId('page-add')).toBeDisabled();expect(state.get().project!.pages).toHaveLength(8);
});
test('unsaved changes require save/discard/cancel before replacing the project',async({page})=>{
  const state=await fixture(page,emptyProject());await addEnable(page);await page.getByTestId('new-project').click();await expect(page.getByTestId('confirmation-dialog')).toBeVisible();await page.getByTestId('confirm-cancel').click();expect(state.get().project!.pages[0].switches.A.states[0].actions).toHaveLength(1);
  await page.getByTestId('open-project').click();await page.getByTestId('confirm-save').click();await expect(page.getByTestId('project-name')).toHaveValue(example.name);expect(state.calls.slice(-2)).toEqual(['save_project','open_project']);
});
test('zoom is persistent and every surface fits 1024px at 75–200%',async({page})=>{
  const state=await fixture(page,structuredClone(example));
  for(const zoom of [75,100,125,150,175,200]){
    await page.keyboard.press('Control+0');const delta=(zoom-100)/25;for(let i=0;i<Math.abs(delta);i++)await page.keyboard.press(delta<0?'Control+-':'Control+=');await expect(page.getByTestId('zoom-value')).toHaveText(`${zoom}%`);await noOverflow(page);
    await page.getByTestId('nav-profiles').click();await noOverflow(page);await page.getByTestId('nav-controller').click();await noOverflow(page);await page.getByTestId('nav-editor').click();await page.getByTestId('switch-C').click();await page.getByTestId('add-action').click();await noOverflow(page);const modal=page.getByTestId('action-editor');await expect(modal).toBeVisible();const sizes=await modal.evaluate(el=>({w:el.scrollWidth,c:el.clientWidth}));expect(sizes.w).toBeLessThanOrEqual(sizes.c+1);await page.keyboard.press('Escape');
  }
  await expect.poll(()=>state.get().zoom_percent).toBe(200);await page.evaluate(()=>window.dispatchEvent(new WheelEvent('wheel',{ctrlKey:true,deltaY:100,cancelable:true})));await expect(page.getByTestId('zoom-value')).toHaveText('175%');
});
test('controller startup performs an explicit scan/read/import without a write',async({page})=>{
  const state=await fixture(page);await page.getByTestId('startup-read').click();await expect(page.getByTestId('controller-device')).toHaveValue('Fixture Chocolate Plus (read only)');await page.getByTestId('controller-read').click();await expect(page.getByTestId('controller-summary')).toContainText('Explicit fixture');await page.getByTestId('controller-import').click();await expect(page.getByTestId('project-name')).toHaveValue(example.name);expect(state.calls).not.toContain('controller_write');
});
test('a slow failed controller read releases busy controls for retry',async({page})=>{
  await fixture(page,null,[tankProfile],true);await page.getByTestId('startup-read').click();await page.getByTestId('controller-read').click();await expect(page.getByTestId('controller-read')).toBeDisabled();await expect(page.getByRole('alert')).toContainText('Explicit fixture read failed.');await expect(page.getByTestId('controller-read')).toBeEnabled();await expect(page.getByTestId('controller-scan')).toBeEnabled();
});
test('custom CC profiles support absent model/parameter arrays and model selection without changing intent',async({page})=>{
  const profile={schema_version:1,id:'minimal_cc',name:'Minimal custom CC',protocol:'midi_cc',notes:'A user-defined fixture.',sources:['User-defined source'],blocks:[{id:'plain',label:'Plain switch',enable:{cc:20}},{id:'models',label:'Model selection',models:[{id:'one',label:'One',cc:21,wire_value:2}]}]};
  const state=await fixture(page,emptyProject(),[profile]);await addEnable(page);const action=state.get().project!.pages[0].switches.A.states[0].actions[0];expect(action.device_id).toBe('minimal_cc');expect(action).not.toHaveProperty('model_id');
  await page.getByTestId('switch-B').click();await page.getByTestId('add-action').click();await page.getByTestId('action-block').selectOption('models');await page.getByTestId('action-kind').selectOption('model');await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);expect(state.get().project!.pages[0].switches.B.states[0].actions[0].type).toBe('model');await page.getByTestId('action-edit-0').click();await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);expect(state.get().project!.pages[0].switches.B.states[0].actions[0].type).toBe('model');await page.getByTestId('nav-profiles').click();await expect(page.getByTestId('profile-detail')).toContainText('A user-defined fixture.');
});
test('editing reload keeps its nonsemantic metadata without adding fields',async({page})=>{
  const project=emptyProject();project.pages[0].switches.A.states[0].actions=[{type:'reload_current_preset',device_id:'tank_mini_v09',custom_metadata:{color:'turquoise'}}];const state=await fixture(page,project);await page.getByTestId('action-edit-0').click();await page.getByTestId('action-save').click();await expect(page.getByTestId('action-editor')).toHaveCount(0);expect(state.get().project!.pages[0].switches.A.states[0].actions[0]).toEqual(project.pages[0].switches.A.states[0].actions[0]);
});
test('narrow desktop keeps every project action reachable at 100% and 200% zoom',async({page})=>{
  await page.setViewportSize({width:620,height:800});
  const state=await fixture(page,emptyProject());
  for(const zoom of [100,200]){
    await page.keyboard.press('Control+0');
    if(zoom===200)for(let i=0;i<4;i++)await page.keyboard.press('Control+=');
    await expect(page.getByTestId('zoom-value')).toHaveText(`${zoom}%`);
    for(const id of ['open-project','new-project','save-project','save-as','export-messages']){
      const control=page.getByTestId(id);await expect(control).toBeVisible();await expect(control).toBeEnabled();
      await control.scrollIntoViewIfNeeded();const box=await control.boundingBox();expect(box).not.toBeNull();expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(621);expect(box!.y).toBeGreaterThanOrEqual(0);expect(box!.y+box!.height).toBeLessThanOrEqual(800);
    }
    await noOverflow(page);
    const font=await page.getByTestId('open-project').evaluate(el=>parseFloat(getComputedStyle(el).fontSize));expect(font).toBeCloseTo(14*.82*zoom/100,1);
    await page.getByTestId('export-messages').click();await expect.poll(()=>state.calls.at(-1)).toBe('export_messages');
    await page.getByTestId('save-as').click();await expect.poll(()=>state.calls.at(-1)).toBe('save_project');
    await page.getByTestId('new-project').click();await expect(page.getByTestId('project-name')).toHaveValue('New project');
    await page.getByTestId('open-project').click();await expect(page.getByTestId('project-name')).toHaveValue(example.name);
  }
});

test('global zoom scales every text surface without shrinking labels at layout changes',async({page})=>{
  await fixture(page,emptyProject());
  const selectors=['.brand','.brand small','.sidebar nav button','.project-tools button','.sidebar-label','.sidebar-footer','.page-heading h1','.statusbar'];
  for(const width of [620,1024,1440]){
    await page.setViewportSize({width,height:900});
    await page.keyboard.press('Control+0');
    const base=await page.evaluate(list=>list.map(selector=>parseFloat(getComputedStyle(document.querySelector(selector)!).fontSize)),selectors);
    for(const zoom of [125,150,175,200]){
      await page.keyboard.press('Control+=');
      await expect(page.getByTestId('zoom-value')).toHaveText(`${zoom}%`);
      const actual=await page.evaluate(list=>list.map(selector=>parseFloat(getComputedStyle(document.querySelector(selector)!).fontSize)),selectors);
      actual.forEach((size,index)=>expect(size,`${selectors[index]} at ${width}px / ${zoom}%`).toBeCloseTo(base[index]*zoom/100,1));
      await noOverflow(page);
    }
  }
});
