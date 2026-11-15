<script lang="ts">
  import { onMount, tick } from 'svelte';
  import Icon from './Icon.svelte';
  import ActionEditor from './ActionEditor.svelte';
  import {trapFocus} from './focus';
  import {displayProfile} from './profiles';
  import {request, isBrowserPreview, isAndroid} from './bridge';
  import LiveView from './LiveView.svelte';
  import type {Action, Page, Project, Snapshot} from './types';

  let snapshot:Snapshot|null=null;
  let loading=true, pending=false, browserPreview=false;
  let error='', localStatus='', activeTab:'editor'|'controller'|'profiles'='editor';
  // The live view exists only in the Android app, where the phone forwards the footswitches.
  const android=isAndroid();
  let liveOpen=false;
  let pageIndex=0, switchId='A', stateIndex=0, previewTab:'hex'|'cc'='hex';
  let zoom=100, deviceName='', profileId='';
  let actionEditor:{original:Action|null;index:number}|null=null;
  let actionError='';
  let confirmation:{title:string;body:string;kind:'unsaved'|'danger'|'write';execute:()=>Promise<void>}|null=null;
  let confirmBusy=false;
  let zoomTimer:number;
  $: project=snapshot?.project || null;
  $: currentPage=project?.pages[pageIndex];
  $: currentSwitch=currentPage?.switches[switchId];
  $: currentState=currentSwitch?.states[stateIndex];
  $: compiled=snapshot?.compiled?.pages[pageIndex]?.switches[switchId]?.states[stateIndex];
  $: controller=snapshot?.controller;
  $: locked=pending || !!controller?.busy;
  $: selectedProfile=snapshot?.profiles.find(p=>p.id===profileId)||snapshot?.profiles[0];
  $: allActions=project?.pages.reduce((sum,p)=>sum+Object.values(p.switches).reduce((n,s)=>n+s.states.reduce((a,state)=>a+state.actions.length,0),0),0)||0;
  $: if(snapshot && !profileId && snapshot.profiles.length) profileId=snapshot.profiles[0].id;
  $: document.documentElement.style.fontSize=`${14*zoom/100}px`;

  function absorb(data:Snapshot, adoptZoom=false) {
    if(!snapshot && data.controller.pending_save)activeTab='controller';
    snapshot={...data,profiles:data.profiles.map(displayProfile)};
    if(adoptZoom)zoom=Math.min(200,Math.max(75,data.zoom_percent || 100));
    if(data.project) {
      pageIndex=Math.min(pageIndex,data.project.pages.length-1);
      stateIndex=Math.min(stateIndex,data.project.pages[pageIndex]?.switches[switchId]?.states.length-1 || 0);
    }
    if(data.controller.devices.length && !data.controller.devices.some(d=>d.name===deviceName))deviceName=data.controller.devices[0].name;
  }
  async function run(method:string,payload:Record<string,unknown>={},options:{silent?:boolean;adoptZoom?:boolean}={}) {
    if(!options.silent){pending=true;error='';localStatus='';}
    let poll:number|undefined;
    let pollActive=true;
    if(method.startsWith('controller_') && method!=='controller_disconnect') {
      poll=window.setInterval(async()=>{
        const result=await request('bootstrap');
        if(pollActive&&result.ok)absorb(result.data);
      },650);
    }
    try {
      const result=await request(method,payload);
      pollActive=false;
      if(!result.ok)throw Error(result.error.message);
      absorb(result.data,options.adoptZoom);
      return result.data;
    } catch(e) {
      pollActive=false;
      error=(e as Error).message;
      if(method.startsWith('controller_')) {
        const recovered=await request('bootstrap').catch(()=>null);
        if(recovered?.ok)absorb(recovered.data);
      }
      return null;
    }
    finally {pollActive=false;if(poll)window.clearInterval(poll);if(!options.silent)pending=false;}
  }
  onMount(()=>{
    run('bootstrap',{}, {adoptZoom:true}).then(()=>{loading=false;browserPreview=isBrowserPreview();});
    const bridgeReady=()=>{browserPreview=false;run('bootstrap',{}, {adoptZoom:true}).then(()=>loading=false);};
    window.addEventListener('pywebviewready',bridgeReady);
    const key=(event:KeyboardEvent)=>{
      if(event.ctrlKey||event.metaKey) {
        if(['+','=','-','0'].includes(event.key)){event.preventDefault();setZoom(event.key==='0'?100:zoom+(event.key==='-'?-25:25));}
        if(event.key.toLowerCase()==='s'){event.preventDefault();(document.activeElement as HTMLElement)?.blur();if(project&&!locked)run('save_project');}
      }
      if(event.key==='Escape'&&!locked){actionEditor=null;confirmation=null;}
    };
    const wheel=(event:WheelEvent)=>{if(event.ctrlKey){event.preventDefault();setZoom(zoom+(event.deltaY<0?25:-25));}};
    window.addEventListener('keydown',key);window.addEventListener('wheel',wheel,{passive:false});
    const beforeUnload=(event:BeforeUnloadEvent)=>{if(!window.pywebview?.api&&snapshot?.dirty){event.preventDefault();event.returnValue='';}};
    window.addEventListener('beforeunload',beforeUnload);
    const closeRequested=()=>{
      if(locked){localStatus='Wait for the controller operation to finish before closing.';return;}
      if(snapshot?.dirty)confirmation={title:'Save before closing?',body:'This project has unsaved changes. Save it before closing, or discard the changes.',kind:'unsaved',execute:async()=>{await request('close_application',{discard:true});}};
      else request('close_application');
    };
    const notice=(event:Event)=>{localStatus=(event as CustomEvent<{message:string}>).detail.message;};
    window.addEventListener('stompilot-close-request',closeRequested);window.addEventListener('stompilot-notice',notice);
    return ()=>{window.removeEventListener('keydown',key);window.removeEventListener('wheel',wheel);window.removeEventListener('beforeunload',beforeUnload);window.removeEventListener('pywebviewready',bridgeReady);window.removeEventListener('stompilot-close-request',closeRequested);window.removeEventListener('stompilot-notice',notice);window.clearTimeout(zoomTimer);};
  });
  function setZoom(percent:number) {
    zoom=Math.min(200,Math.max(75,Math.round(percent)));
    window.clearTimeout(zoomTimer);
    zoomTimer=window.setTimeout(async()=>{
      const result=await request('set_zoom',{percent:zoom});
      if(result.ok && snapshot) snapshot={...snapshot,zoom_percent:result.data.zoom_percent};
      else if(!result.ok)error=result.error.message;
    },150);
  }
  async function mutate(change:(draft:Project)=>void) {
    if(!project||locked)return false;
    const draft=structuredClone(project);change(draft);
    return !!await run('set_project',{project:draft});
  }
  function textChange(event:Event,change:(draft:Project,text:string)=>void) {
    const input=event.currentTarget as HTMLInputElement;
    const text=input.value.trim();
    if(!text){error='Names cannot be empty.';input.value=input.defaultValue;return;}
    mutate(p=>change(p,text));
  }
  function emptyPage(number:number):Page {
    return {name:`Page ${number}`,switches:Object.fromEntries('ABCD'.split('').map(letter=>[letter,{label:`Switch ${letter}`,mode:'single',states:[{label:'Sequence',actions:[]}]}]))};
  }
  async function addPage(){if((project?.pages.length||0)>=8)return;const next=project!.pages.length; if(await mutate(p=>p.pages.push(emptyPage(next+1)))){pageIndex=next;stateIndex=0;}}
  function choosePage(index:number){pageIndex=index;stateIndex=0;}
  function guard(execute:()=>Promise<void>) {
    if(snapshot?.dirty)confirmation={title:'Save your changes?',body:'This project has unsaved changes. Save it before continuing, or discard the changes.',kind:'unsaved',execute};
    else execute();
  }
  async function confirmExecute(save=false) {
    if(!confirmation)return;
    confirmBusy=true;
    if(save) {const saved=await run('save_project');if(!saved||saved.dirty){confirmBusy=false;return;}}
    const execute=confirmation.execute;confirmation=null;await execute();confirmBusy=false;
  }
  function danger(title:string,body:string,execute:()=>Promise<void>){confirmation={title,body,kind:'danger',execute};}
  async function openProject(){guard(async()=>{const result=await run('open_project');if(result?.project){activeTab='editor';pageIndex=stateIndex=0;switchId='A';}});}
  async function newProject(){guard(async()=>{if(await run('new_project')){activeTab='editor';pageIndex=stateIndex=0;switchId='A';}});}
  async function readStartup(){activeTab='controller';await run('controller_scan');}
  // From the editor: connect and read when there is exactly one controller, then preview. Writing still needs its confirmation.
  async function prepareWrite(){
    activeTab='controller';
    if(!controller?.connected){
      const scanned=await run('controller_scan');
      if(scanned?.controller.devices.length!==1)return;
      if(!await run('controller_read',{device_name:scanned.controller.devices[0].name}))return;
    }
    if(await run('controller_preview')){await tick();document.querySelector('.write-panel')?.scrollIntoView({block:'nearest'});}
  }
  async function changeMode(mode:'single'|'alternating') {
    if(mode===currentSwitch?.mode)return;
    const execute=async()=>{if(await mutate(p=>{const s=p.pages[pageIndex].switches[switchId];s.mode=mode;if(mode==='single')s.states=s.states.slice(0,1);else s.states.push({label:'State 2',actions:[]});}))stateIndex=0;};
    if(mode==='single'&&currentSwitch?.states[1]?.actions.length)danger('Use one sequence?','The second state and its actions will be removed from this switch.',execute);else await execute();
  }
  async function moveAction(index:number,delta:number){await mutate(p=>{const actions=p.pages[pageIndex].switches[switchId].states[stateIndex].actions;[actions[index],actions[index+delta]]=[actions[index+delta],actions[index]];});}
  async function saveAction(action:Action){
    if(!actionEditor)return;
    const index=actionEditor.index;
    const succeeded=await mutate(p=>{const actions=p.pages[pageIndex].switches[switchId].states[stateIndex].actions;if(index<0)actions.push(action);else actions[index]=action;});
    if(succeeded)actionEditor=null;else actionError=error;
  }
  function actionInfo(action:Action) {
    if(action.type==='raw_midi'){
      const bytes=(action.message_hex||'').trim().split(/\s+/).map(v=>parseInt(v,16));
      return {title:bytes[0]===240?'Imported SysEx':`CC ${bytes[1]} = ${bytes[2]}`,sub:bytes[0]===240?`Original MIDI · ${bytes.length} bytes`:`Imported MIDI · channel ${(bytes[0]&15)+1}`,value:'RAW',icon:'file'};
    }
    const p=snapshot?.profiles.find(p=>p.id===action.device_id),b=p?.blocks.find(b=>b.id===action.block_id),m=b?.models.find(m=>m.id===action.model_id);
    if(action.type==='reload_current_preset')return {title:'Reload current preset',sub:p?.name||'TANK Mini',value:'EXPERIMENTAL',icon:'refresh'};
    if(action.type==='select_preset'){const names=p?.device_actions?.find(a=>a.id==='select_preset')?.presets;return {title:'Select preset',sub:`${p?.name||action.device_id} · ch ${action.channel||1}`,value:names?.[action.value as number]??String(action.value),icon:'grid'};}
    if(action.type==='preset_volume')return {title:'Preset volume',sub:p?.name||action.device_id||'',value:String(action.value),icon:'volume'};
    const param=m?.parameters.find(p=>p.id===action.param_id);
    return {title:action.type==='parameter'?`${m?.label||b?.label} · ${param?.label||action.param_id}`:m?.label||b?.label||'Effect',sub:`${b?.label||''} · ${p?.name||action.device_id} · ch ${action.channel||1}`,value:action.type==='parameter'?String(action.value):action.type==='model'?'MODEL':action.type==='toggle'?'TOGGLE':action.value?'ON':'OFF',icon:action.type==='parameter'?'chip':'power'};
  }
  async function copyHex(){
    const text=compiled?.messages_hex.join('\n')||'';
    try{await navigator.clipboard.writeText(text);localStatus='MIDI bytes copied to clipboard.';}catch{error='Clipboard access is unavailable. Select the MIDI bytes and copy them manually.';}
  }
  async function writeController(){if(!controller?.preview)return;confirmation={title:'Write to Chocolate Plus?',body:`Apply this exact preview to the connected controller (${controller.preview.changed_bytes} changed bytes). Stompilot rereads the controller, creates a backup and verifies the result.${controller.identity?.name==='FootCtrlPlus_013'?' After writing, turn the pedal fully off and reconnect it for the final check.':''}`,kind:'write',execute:async()=>{await run('controller_write');}};}
  function restoreController(){danger('Restore the original configuration?','Replace the unfinished save with the complete configuration backed up before it. Your project stays available in Stompilot. Keep the pedal powered and connected in U until restoration finishes.',async()=>{await run('controller_restore');});}
  function evidenceText(evidence:unknown){return typeof evidence==='string'?evidence.replace(/_/g,' '):typeof evidence==='object'&&evidence?(evidence as {note?:string;status?:string}).note||(evidence as {status?:string}).status?.replace(/_/g,' ')||'':'';}
</script>

<svelte:head><title>{project ? `${project.name} · Stompilot` : 'Stompilot'}</title></svelte:head>

<div class="app-shell" class:large-text={zoom>=150} style={`--text-size:${14*zoom/100}px`}>
  <header class="topbar">
    <button class="brand" on:click={()=>activeTab='editor'} aria-label="Stompilot project editor"><span class="brand-symbol"><Icon name="bolt" size={22} /></span><span>stomp<span class="accent">ilot</span></span></button>
    <div class="topbar-project">{#if project}<span class:unsaved={snapshot?.dirty} class="dot"></span><span class="truncate">{snapshot?.file_name||project.name}</span><span class="muted small">{snapshot?.dirty?'Unsaved changes':'Saved project'}</span>{/if}</div>
    <div class="zoom-toolbar" aria-label="Text zoom"><span class="zoom-label">Aa</span><button class="icon-button compact" aria-label="Decrease text zoom" on:click={()=>setZoom(zoom-25)} disabled={zoom<=75} data-testid="zoom-out"><Icon name="minus" size={15} /></button><button class="zoom-value" on:click={()=>setZoom(100)} title="Reset text zoom (Ctrl+0)" data-testid="zoom-value">{zoom}%</button><button class="icon-button compact" aria-label="Increase text zoom" on:click={()=>setZoom(zoom+25)} disabled={zoom>=200} data-testid="zoom-in"><Icon name="plus" size={15} /></button></div>
  </header>

  {#if browserPreview}<div class="browser-banner" data-testid="browser-preview"><Icon name="eye" size={15} />Browser preview · {window.__STOMPILOT_REQUEST__?'explicit test fixture; no hardware connection':'desktop bridge unavailable; launch Stompilot to use files and hardware'}</div>{/if}
  <div class="workspace">
    <aside class="sidebar">
      <span class="eyebrow sidebar-label">WORKSPACE</span>
      <nav aria-label="Main navigation">{#if android}<button on:click={()=>liveOpen=true} disabled={locked} data-testid="nav-live"><Icon name="bolt" /><span>Live</span></button>{/if}<button class:active={activeTab==='editor'} on:click={()=>activeTab='editor'} data-testid="nav-editor"><Icon name="grid" /><span>Project editor</span></button><button class:active={activeTab==='controller'} on:click={()=>activeTab='controller'} data-testid="nav-controller"><Icon name="usb" /><span>Chocolate Plus</span>{#if controller?.connected}<span class="connection-dot"></span>{/if}</button><button class:active={activeTab==='profiles'} on:click={()=>activeTab='profiles'} data-testid="nav-profiles"><Icon name="chip" /><span>Device profiles</span></button></nav>
      <div class="sidebar-divider"></div>
      <span class="eyebrow sidebar-label">PROJECT</span>
      <div class="project-tools"><button on:click={openProject} disabled={locked} data-testid="open-project"><Icon name="folder" />Open project</button><button on:click={newProject} disabled={locked} data-testid="new-project"><Icon name="plus" />New project</button><button on:click={()=>run('save_project')} disabled={!project||locked} data-testid="save-project"><Icon name="save" />Save</button><button on:click={()=>run('save_project',{save_as:true})} disabled={!project||locked} data-testid="save-as"><Icon name="file" />Save as…</button><button on:click={()=>run('export_messages')} disabled={!project||locked} data-testid="export-messages"><Icon name="export" />Export MIDI</button></div>
    </aside>

    <main class="main-content">
      {#if error}<div class="notice error global-error" role="alert"><Icon name="alert" /><span>{error}</span><button class="icon-button" on:click={()=>error=''} aria-label="Dismiss error"><Icon name="close" size={15} /></button></div>{/if}
      {#if android&&liveOpen}<LiveView {snapshot} run={method=>run(method)} on:close={()=>liveOpen=false} />{/if}
      {#if loading}<div class="loading-state"><span class="spinner"></span><p>Starting Stompilot…</p></div>
      {:else if activeTab==='editor'&&!project}
        <section class="startup">
          <p class="startup-intro">Build and organize MIDI actions for your Chocolate Plus.<br class="desktop-break" /> Choose where you want to begin.</p>
          <div class="startup-options"><button class="start-card" on:click={openProject} disabled={locked} data-testid="startup-open"><span class="option-icon"><Icon name="folder" size={24}/></span><strong>Open saved project</strong><span>Pick up where you left off.</span><Icon name="right" /></button><button class="start-card" on:click={readStartup} disabled={locked} data-testid="startup-read"><span class="option-icon"><Icon name="usb" size={24}/></span><strong>Read Chocolate Plus</strong><span>See what your controller sends now.</span><Icon name="right" /></button><button class="start-card featured" on:click={newProject} disabled={locked} data-testid="startup-new"><span class="option-icon"><Icon name="plus" size={24}/></span><strong>Create empty project</strong><span>A fresh canvas for your live rig.</span><Icon name="right" /></button></div>
          <div class="startup-note"><Icon name="info" size={16}/><span>Projects stay on your computer. Connect your controller when you’re ready.</span></div>
        </section>
      {:else if activeTab==='editor'&&project&&currentPage&&currentSwitch&&currentState}
        <div class="page-heading"><div><span class="eyebrow">PROJECT EDITOR</span></div><div class="button-row heading-actions"><button on:click={prepareWrite} disabled={locked} data-testid="editor-write"><Icon name="export" />Write to Chocolate Plus</button><button class="primary" on:click={()=>run('save_project')} disabled={locked}><Icon name="save" />Save project</button></div></div>
        <section class="project-strip"><label class="project-name-label">Project name<input value={project.name} on:change={e=>textChange(e,(p,text)=>p.name=text)} disabled={locked} data-testid="project-name" /></label><div class="project-meta"><span class="pill"><Icon name="usb" size={14}/>Chocolate Plus</span><span>{project.pages.length} {project.pages.length===1?'page':'pages'} · {allActions} {allActions===1?'action':'actions'}</span><span class="origin">{snapshot?.origin || 'Empty project'}</span></div></section>
        <section class="editor-section">
          <div class="section-top"><div><span class="eyebrow">PAGES</span><span class="muted small"> {project.pages.length} / 8</span></div><button class="quiet small-button" on:click={addPage} disabled={locked||project.pages.length>=8} data-testid="page-add"><Icon name="plus" size={15}/>Add page</button></div>
          <div class="page-tabs" aria-label="Project pages">{#each project.pages as page,index}<button class:active={pageIndex===index} on:click={()=>choosePage(index)} data-testid={`page-${index}`}><span class="page-number">{String(index+1).padStart(2,'0')}</span>{page.name}</button>{/each}</div>
          <div class="page-edit-row"><label>Page name<input value={currentPage.name} on:change={e=>textChange(e,(p,text)=>p.pages[pageIndex].name=text)} disabled={locked} data-testid="page-name" /></label><div class="button-row"><button class="icon-button" aria-label="Move page earlier" disabled={locked||pageIndex===0} on:click={async()=>{const index=pageIndex;if(await mutate(p=>[p.pages[index-1],p.pages[index]]=[p.pages[index],p.pages[index-1]]))pageIndex--;}}><Icon name="left"/></button><button class="icon-button" aria-label="Move page later" disabled={locked||pageIndex===project.pages.length-1} on:click={async()=>{const index=pageIndex;if(await mutate(p=>[p.pages[index+1],p.pages[index]]=[p.pages[index],p.pages[index+1]]))pageIndex++;}}><Icon name="right"/></button><button class="icon-button danger-button" aria-label="Remove page" disabled={locked||project.pages.length===1} on:click={()=>danger('Remove this page?',`Remove “${currentPage!.name}” and all four switch assignments?`,async()=>{await mutate(p=>p.pages.splice(pageIndex,1));})}><Icon name="trash"/></button></div></div>
          <div class="switch-grid" aria-label="Footswitches">{#each ['A','B','C','D'] as letter}{@const footswitch=currentPage.switches[letter]}<button class="switch-card" class:active={switchId===letter} on:click={()=>{switchId=letter;stateIndex=0;}} data-testid={`switch-${letter}`}><span class="switch-letter">{letter}</span><span class="switch-description"><strong>{footswitch.label}</strong><span>{footswitch.mode==='alternating'?'Two alternating states':'One sequence'} · {footswitch.states.reduce((sum,s)=>sum+s.actions.length,0)} actions</span></span><span class="switch-indicator"></span></button>{/each}</div>
        </section>
        <div class="editor-columns">
          <section class="panel sequence-panel">
            <div class="panel-header"><div><span class="eyebrow">SWITCH {switchId}</span><h2>Action sequence</h2></div><span class="letter-badge">{switchId}</span></div>
            <div class="switch-form"><label>Switch name<input value={currentSwitch.label} on:change={e=>textChange(e,(p,text)=>p.pages[pageIndex].switches[switchId].label=text)} disabled={locked} data-testid="switch-name" /></label><label>Switch behavior<select value={currentSwitch.mode} on:change={e=>changeMode((e.currentTarget as HTMLSelectElement).value as 'single'|'alternating')} disabled={locked} data-testid="switch-mode"><option value="single">One sequence</option><option value="alternating">Alternate two states</option></select></label></div>
            <div class="state-tabs" aria-label="Switch states">{#each currentSwitch.states as state,index}<button class:active={stateIndex===index} on:click={()=>stateIndex=index} data-testid={`state-${index}`}><span class="dot"></span>{currentSwitch.mode==='single'?'Sequence':`State ${index+1}`}<span class="state-count">{state.actions.length}</span></button>{/each}</div>
            <div class="state-content"><label>State name<input value={currentState.label} on:change={e=>textChange(e,(p,text)=>p.pages[pageIndex].switches[switchId].states[stateIndex].label=text)} disabled={locked} data-testid="state-name" /></label>
              <div class="action-list" data-testid="action-list">{#each currentState.actions as action,index}{@const info=actionInfo(action)}<article class="action-row" data-testid={`action-${index}`}><span class="action-index">{String(index+1).padStart(2,'0')}</span><span class="action-icon"><Icon name={info.icon}/></span><div class="action-description"><strong>{info.title}</strong><span>{info.sub}</span><span class="value-badge" class:on={info.value==='ON'} class:experimental={info.value==='EXPERIMENTAL'}>{info.value}</span></div><div class="action-controls"><button class="icon-button" aria-label={`Move action ${index+1} up`} disabled={locked||index===0} on:click={()=>moveAction(index,-1)}><Icon name="up" size={15}/></button><button class="icon-button" aria-label={`Move action ${index+1} down`} disabled={locked||index===currentState!.actions.length-1} on:click={()=>moveAction(index,1)}><Icon name="down" size={15}/></button><button class="icon-button" aria-label={`Edit action ${index+1}`} disabled={locked} on:click={()=>{actionError='';actionEditor={original:action,index};}} data-testid={`action-edit-${index}`}><Icon name="edit" size={15}/></button><button class="icon-button danger-button" aria-label={`Remove action ${index+1}`} disabled={locked} on:click={()=>danger('Remove this action?',`Remove “${info.title}” from this state?`,async()=>{await mutate(p=>p.pages[pageIndex].switches[switchId].states[stateIndex].actions.splice(index,1));})} data-testid={`action-remove-${index}`}><Icon name="trash" size={15}/></button></div></article>{:else}<div class="empty-sequence"><span class="empty-icon"><Icon name="bolt" size={27}/></span><strong>Your sequence starts here.</strong><p>Add an action to control an effect<br />or set a parameter.</p></div>{/each}</div>
              <button class="add-action-button" on:click={()=>{actionError='';actionEditor={original:null,index:-1};}} disabled={locked||!snapshot?.profiles.length||currentState.actions.some(a=>a.type==='reload_current_preset')||currentState.actions.length>=128} data-testid="add-action"><Icon name="plus"/>Add action</button>
              {#if currentState.actions.some(a=>a.type==='reload_current_preset')}<p class="field-hint amber-text">Reload current preset must be the only action in this state.</p>{/if}
            </div>
          </section>
          <section class="panel preview-panel"><div class="panel-header"><div><span class="eyebrow">COMPILED OUTPUT</span><h2>MIDI preview</h2></div><span class="pill neutral">{compiled?.byte_count||0} bytes</span></div><p class="muted preview-intro">Exact messages for this state, in sending order.</p><div class="preview-tabs"><button class:active={previewTab==='hex'} on:click={()=>previewTab='hex'}>Hex bytes</button><button class:active={previewTab==='cc'} on:click={()=>previewTab='cc'}>MIDI rows <span>{compiled?.midi_rows.length||0}</span></button><button class="icon-button copy-button" aria-label="Copy MIDI bytes" disabled={!compiled?.messages_hex.length} on:click={copyHex} data-testid="copy-midi"><Icon name="copy" size={16}/></button></div>{#if previewTab==='hex'}<div class="hex-preview" data-testid="hex-preview">{#if compiled?.messages_hex.length}{#each compiled.messages_hex as message,index}<div class="hex-message"><span>{String(index+1).padStart(2,'0')}</span><code>{message}</code></div>{/each}{:else}<p>Messages appear here<br />when you add an action.</p>{/if}</div>{:else}<div class="table-scroll"><table><thead><tr><th>Channel</th><th>Type</th><th>Number</th><th>Value</th></tr></thead><tbody>{#each compiled?.midi_rows||[] as row}<tr><td>{row.channel}</td><td>{row.type}</td><td>{row.data1}</td><td>{row.type==='PC'?'—':row.data2}</td></tr>{:else}<tr><td colspan="4" class="muted">No CC or Program Change messages in this state.</td></tr>{/each}</tbody></table></div>{/if}<div class="preview-summary">{#each compiled?.summaries||[] as summary}<p><Icon name="check" size={14}/>{summary}</p>{/each}</div>{#if compiled?.warnings.length}<div class="preview-warnings"><span class="eyebrow amber-text">BEFORE YOU PLAY</span>{#each compiled.warnings as warning}<p>{warning}</p>{/each}</div>{:else}<div class="preview-help"><Icon name="info" size={16}/><p>Choose Write to Chocolate Plus to review the changes and write them to the controller.</p></div>{/if}</section>
        </div>
      {:else if activeTab==='controller'}
        <div class="page-heading"><div><span class="eyebrow">CONTROLLER</span></div><span class="connection-pill" class:connected={controller?.connected}><span class="dot"></span>{controller?.connected?'Connected':'Not connected'}</span></div>
        <section class="panel controller-connection"><div class="panel-header"><div><span class="eyebrow">USB CONNECTION</span><h2>Connect your controller</h2></div><Icon name="usb" size={25}/></div><p class="muted">Connect Chocolate Plus by USB, close other MIDI applications, then scan for devices.</p><div class="connection-form"><label>Available MIDI device<select bind:value={deviceName} disabled={locked} data-testid="controller-device">{#if !controller?.devices.length}<option value="">No Chocolate Plus found</option>{/if}{#each controller?.devices||[] as device}<option value={device.name}>{device.name}</option>{/each}</select></label><button on:click={()=>run('controller_scan')} disabled={locked} data-testid="controller-scan"><Icon name="refresh"/>Scan devices</button><button class="primary" on:click={()=>run('controller_read',{device_name:deviceName})} disabled={locked||!deviceName} data-testid="controller-read"><Icon name="usb"/>Read controller</button>{#if controller?.connected}<button on:click={()=>run('controller_disconnect')} disabled={locked} data-testid="controller-disconnect">Disconnect</button>{/if}</div><div class="controller-status" role="status" data-testid="controller-status">{#if locked}<span class="spinner small-spinner"></span>{:else}<span class="dot" class:connected={controller?.connected}></span>{/if}<span>{controller?.status||'Ready to scan'}</span></div>{#if controller?.identity}<div class="identity-row"><span><strong>{controller.identity.name}</strong></span><span>Firmware {controller.identity.firmware_version}</span></div>{/if}</section>
        {#if controller?.pending_save}
          <section class="panel" aria-label="Finish configuration save" data-testid="controller-pending-save">
            <div class="panel-header"><div><span class="eyebrow">CONFIGURATION SAVE</span><h2>{controller.pending_save.status==='restart_required'?'Verify after restart':'Restore interrupted save'}</h2></div><Icon name="refresh"/></div>
            <p>{controller.pending_save.message}</p>
            {#if controller.pending_save.backup_name}<p class="field-hint">Backup: {controller.pending_save.backup_name}</p>{/if}
            <div class="panel-actions">
              <button class="primary" on:click={()=>run('controller_verify')} disabled={locked||controller.pending_save.status==='invalid'} data-testid="controller-verify">Verify after restart</button>
              <button on:click={restoreController} disabled={locked||controller.pending_save.status==='invalid'} data-testid="controller-restore">Restore backup</button>
            </div>
          </section>
        {/if}
        <div class="controller-columns"><section class="panel"><div class="panel-header"><div><span class="eyebrow">ON YOUR CONTROLLER</span><h2>Current configuration</h2></div><Icon name="grid"/></div>{#if controller?.summary}<pre class="read-summary" data-testid="controller-summary">{controller.summary}</pre>{:else}<div class="empty-sequence compact-empty"><Icon name="usb" size={32}/><strong>A clear picture of your setup.</strong><p>Read your Chocolate Plus to inspect its pages and assignments.</p></div>{/if}<p class="field-hint panel-hint">Read only. To change the controller, write a project.</p></section><section class="panel write-panel"><div class="panel-header"><div><span class="eyebrow">FROM YOUR PROJECT</span><h2>Write preview</h2></div>{#if controller?.preview}<span class="pill neutral">{controller.preview.changed_bytes} changed bytes</span>{/if}</div><p class="muted preview-intro">Review exactly what will change before writing. A fresh backup is saved before every write.</p><div class="panel-actions"><button on:click={()=>run('controller_preview')} disabled={locked||!controller?.connected||!project||!!controller?.pending_save} data-testid="controller-preview"><Icon name="eye"/>Preview changes</button></div>{#if controller?.preview}<div class="write-preview" data-testid="write-preview">{#each controller.preview.summaries as summary}<p>{summary}</p>{/each}</div>{#if controller.preview.warnings.length}<div class="preview-warnings">{#each controller.preview.warnings as warning}<p>{warning}</p>{/each}</div>{/if}<div class="panel-actions"><button class="primary" on:click={writeController} disabled={locked||!controller.can_write} data-testid="controller-write"><Icon name="export"/>Write to controller</button></div>{:else}<div class="empty-sequence compact-empty"><Icon name="eye" size={30}/><strong>See every change first.</strong><p>{project?'Read the controller, then generate a write preview.':'Open or create a project to prepare a write preview.'}</p></div>{/if}</section></div>
      {:else if activeTab==='profiles'}
        <div class="page-heading"><div><span class="eyebrow">DEVICE LIBRARY</span></div><button class="primary" on:click={()=>run('import_profile')} disabled={locked} data-testid="profile-import"><Icon name="plus"/>Import JSON profile</button></div>
        <div class="profiles-layout"><section class="profile-list panel" aria-label="Device profiles">{#each snapshot?.profiles||[] as profile}<button class:active={selectedProfile?.id===profile.id} on:click={()=>profileId=profile.id} data-testid={`profile-${profile.id}`}><span class="profile-avatar"><Icon name="chip" size={25}/></span><span><strong>{profile.name}</strong><small>{profile.protocol==='tank_v09'?'TANK Mini SysEx':'MIDI CC'} · {profile.blocks.length} blocks</small></span><Icon name="right" size={16}/></button>{:else}<p class="muted">Launch the desktop app to load device profiles.</p>{/each}<p class="field-hint">Import a JSON profile to extend your device library.</p></section>{#if selectedProfile}<section class="panel profile-detail" data-testid="profile-detail"><div class="panel-header"><div><span class="eyebrow">DEVICE PROFILE</span><h2>{selectedProfile.name}</h2></div><button on:click={()=>run('export_profile',{profile_id:selectedProfile.id})} disabled={locked} data-testid="profile-export"><Icon name="export"/>Export profile</button></div><div class="profile-meta"><span class="pill neutral">{selectedProfile.protocol}</span>{#if selectedProfile.firmware}<span class="pill neutral">{selectedProfile.firmware}</span>{/if}<span class="pill">{selectedProfile.blocks.length} effect blocks</span></div><p class="profile-evidence">{evidenceText(selectedProfile.evidence)}</p><div class="profile-blocks">{#each selectedProfile.blocks as block}<details><summary><span><strong>{block.label}</strong><small>{block.models.length} models</small></span><Icon name="down" size={16}/></summary><div class="model-catalog">{#each block.models as model}<div><strong>{model.label}</strong><span>{model.parameters.map(p=>`${p.label} ${p.min??0}–${p.max??100}`).join(' · ')||'No parameters'}</span></div>{/each}</div></details>{/each}</div>{#if selectedProfile.notes?.length}<div class="profile-notes"><span class="eyebrow">PROFILE INFORMATION</span>{#each selectedProfile.notes as note}<p>{note}</p>{/each}</div>{/if}{#if selectedProfile.sources?.length}<div class="profile-sources"><span class="eyebrow">SOURCES</span>{#each selectedProfile.sources as source}<p><strong>{source.title}</strong>{#if source.note}<span>{source.note}</span>{/if}{#if source.url&&/^https?:\/\//.test(source.url)}<a href={source.url} target="_blank" rel="noreferrer">View source ↗</a>{/if}</p>{/each}</div>{/if}</section>{/if}</div>
      {/if}
    </main>
  </div>
  <footer class="statusbar"><span class="status-message" role="status">{#if locked}<span class="spinner small-spinner"></span>{:else}<span class="dot"></span>{/if}{localStatus || (locked ? controller?.busy?controller.status:'Working…' : 'Ready.')}</span><span class="status-shortcuts">Ctrl + / − to zoom · Ctrl+0 to reset</span></footer>
  {#if actionEditor&&snapshot}<ActionEditor profiles={snapshot.profiles} original={actionEditor.original} reloadAllowed={(currentState?.actions.length||0)===(actionEditor.index<0?0:1)} saving={locked} saveError={actionError} on:save={e=>saveAction(e.detail)} on:close={()=>actionEditor=null}/>{/if}
  {#if confirmation}<div class="modal-backdrop"><div class="modal confirmation-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title" data-testid="confirmation-dialog" tabindex="-1" use:trapFocus><header class="modal-header"><div><span class="eyebrow">{confirmation.kind==='unsaved'?'UNSAVED PROJECT':confirmation.kind==='write'?'READY TO WRITE':'CONFIRM CHANGE'}</span><h2 id="confirm-title">{confirmation.title}</h2></div></header><div class="modal-body"><p>{confirmation.body}</p>{#if error}<p class="error-text" role="alert">{error}</p>{/if}</div><footer class="modal-footer"><button on:click={()=>confirmation=null} disabled={confirmBusy} data-testid="confirm-cancel">Cancel</button>{#if confirmation.kind==='unsaved'}<button on:click={()=>confirmExecute(false)} disabled={confirmBusy} data-testid="confirm-discard">Discard changes</button><button class="primary" on:click={()=>confirmExecute(true)} disabled={confirmBusy} data-testid="confirm-save">Save and continue</button>{:else}<button class:primary={confirmation.kind==='write'} class:danger={confirmation.kind==='danger'} on:click={()=>confirmExecute(false)} disabled={confirmBusy} data-testid="confirm-accept">{confirmation.kind==='write'?'Write exact preview':'Confirm'}</button>{/if}</footer></div></div>{/if}
</div>
