<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import Icon from './Icon.svelte';
  import {trapFocus} from './focus';
  import type {Action, Profile} from './types';
  export let profiles:Profile[];
  export let original:Action|null = null;
  export let reloadAllowed = true;
  export let saving = false;
  export let saveError = '';
  const dispatch = createEventDispatcher<{save:Action;close:void}>();
  let profileId = original?.device_id || profiles[0]?.id || '';
  let blockId = original?.block_id || profiles.find(p=>p.id===profileId)?.blocks[0]?.id || '';
  let modelId = original?.model_id || profiles.find(p=>p.id===profileId)?.blocks.find(b=>b.id===blockId)?.models[0]?.id || '';
  let parameterId = original?.param_id || '';
  let kind = original?.type || 'enable';
  let channel = original?.channel || 1;
  let enabled = original?.value === false ? 'off' : 'on';
  let value = typeof original?.value === 'number' ? original.value : 0;
  let rawHex = original?.message_hex || '';
  const originalBytes = (rawHex.replace(/\s/g,'').match(/../g)||[]).map(v=>parseInt(v,16));
  let midiKind = originalBytes[0]===0xf0 ? 'sysex' : (originalBytes[0]&0xf0)===0xc0 ? 'pc' : 'cc';
  let midiLabel = original?.label || '';
  let source = original?.type==='midi'||!profiles.length ? 'midi' : 'profile';
  $: rawIsSysex = midiKind==='sysex';
  let rawChannel = (originalBytes[0] & 15) + 1;
  let rawCC = originalBytes[1] || 0;
  let rawValue = originalBytes[2] || 0;
  let localError = '';
  $: midiSummary = rawIsSysex ? 'SysEx · complete F0 … F7 message' : `Channel ${rawChannel} · ${midiKind==='pc'?`Program Change ${rawCC}`:`CC ${rawCC} = ${rawValue}`}`;
  $: profile = profiles.find(p=>p.id===profileId);
  $: block = profile?.blocks.find(b=>b.id===blockId);
  $: model = block?.models.find(m=>m.id===modelId);
  $: parameters = model?.parameters || [];
  $: parameter = parameters.find(p=>p.id===parameterId);
  $: low = parameter?.min ?? 0;
  $: high = parameter?.max ?? (profile?.protocol === 'midi_cc' ? 127 : 100);
  $: step = parameter?.step ?? 1;
  $: supportsReload = profile?.device_actions?.some(a=>a.id==='reload_current_preset');
  $: presetNames = profile?.device_actions?.find(a=>a.id==='select_preset')?.presets || [];
  $: supportsVolume = profile?.device_actions?.some(a=>a.id==='preset_volume');
  $: if(kind==='select_preset'&&!(Number.isInteger(value)&&value>=0&&value<presetNames.length))value=0;
  $: if(kind==='preset_volume'&&!(Number.isInteger(value)&&value>=0&&value<=100))value=100;
  $: canEnable = profile?.protocol === 'tank_v09' || !!block?.enable;
  $: canToggle = !!block?.toggle;
  $: hasToggles = !!profile?.blocks.some(b=>b.toggle);
  $: canModel = profile?.protocol === 'midi_cc' && model?.cc!==undefined;
  $: existingTankModel = original?.type==='model' && profile?.protocol==='tank_v09';
  $: summary = kind==='midi' ? `${midiLabel.trim()?midiLabel.trim()+' · ':''}${midiSummary}` : kind==='raw_midi' ? (rawIsSysex ? 'Imported SysEx · original bytes retained' : `Imported CC · channel ${rawChannel} · CC ${rawCC} = ${rawValue}`) : kind==='reload_current_preset' ? 'Reload the whole saved preset' : kind==='select_preset' ? `Select preset ${presetNames[value] ?? value} · Program Change ${value}` : kind==='preset_volume' ? `Preset volume = ${value}` : kind==='toggle' ? `${model?.label || block?.label || 'Effect'} · TOGGLE (CC ${block?.toggle?.cc ?? '?'})` : kind==='model'?`Select model · ${model?.label || 'Effect'}`:kind==='parameter' ? `${model?.label || 'Effect'} · ${parameter?.label || 'Parameter'} = ${value}` : `${model?.label || block?.label || 'Effect'} · ${enabled.toUpperCase()}`;

  // Device-wide actions have no block, model or address; their fields are removed on save.
  const deviceKinds=['reload_current_preset','select_preset','preset_volume'];
  const targetFields=['block_id','model_id','param_id','preset','preset_id','preset_index','preset_number','index','address','offset','memory_type','opcode','length','data'];
  function selectSource() { localError=''; kind=source==='midi'?'midi':'enable'; if(source==='profile')selectProfile(); }
  function selectProfile() { blockId=profiles.find(p=>p.id===profileId)?.blocks[0]?.id || ''; selectBlock(); }
  function selectBlock() {
    const selected=profiles.find(p=>p.id===profileId)?.blocks.find(b=>b.id===blockId);
    modelId=selected?.models[0]?.id || ''; selectModel();
  }
  function selectModel() {
    const selected=profiles.find(p=>p.id===profileId)?.blocks.find(b=>b.id===blockId)?.models.find(m=>m.id===modelId);
    parameterId=selected?.parameters[0]?.id || ''; selectParameter();
    const p=profiles.find(p=>p.id===profileId), b=p?.blocks.find(b=>b.id===blockId);
    if(kind==='parameter'&&!selected?.parameters.length)kind='enable';
    if(kind==='enable'&&p?.protocol!=='tank_v09'&&!b?.enable)kind='parameter';
    if(kind==='toggle'&&!b?.toggle)kind=p?.protocol==='tank_v09'||b?.enable?'enable':'parameter';
    if(kind==='model'&&p?.protocol==='midi_cc'&&selected?.cc===undefined)kind=b?.enable?'enable':'parameter';
    if(kind==='parameter'&&!selected?.parameters.length&&selected?.cc!==undefined)kind='model';
    if(deviceKinds.includes(kind)&&!p?.device_actions?.some(a=>a.id===kind))kind=b?.enable?'enable':selected?.parameters.length?'parameter':'model';
  }
  function selectParameter() {
    const selected=profiles.find(p=>p.id===profileId)?.blocks.find(b=>b.id===blockId)?.models.find(m=>m.id===modelId)?.parameters.find(p=>p.id===parameterId);
    const min=selected?.min??0,max=selected?.max??100;
    const increment=selected?.step??1;
    value=min+Math.min(Math.floor((max-min)/increment),Math.max(0,Math.round((Math.min(max,Math.max(min,selected?.default??min))-min)/increment)))*increment;
  }
  if(!parameterId) { parameterId=profiles.find(p=>p.id===profileId)?.blocks.find(b=>b.id===blockId)?.models.find(m=>m.id===modelId)?.parameters[0]?.id || ''; if(!original)selectParameter(); }
  if(!original){const p=profiles.find(p=>p.id===profileId),b=p?.blocks.find(b=>b.id===blockId),m=b?.models.find(m=>m.id===modelId);kind=p?.protocol==='tank_v09'||b?.enable?'enable':m?.parameters.length?'parameter':m?.cc!==undefined?'model':'enable';}
  if(!original&&!profiles.length)kind='midi';
  function accept() {
    localError='';
    try {
      let action:Action;
      if(kind==='raw_midi'||kind==='midi') {
        let hex=rawHex;
        if(!rawIsSysex) {
          if(!Number.isInteger(rawChannel)||rawChannel<1||rawChannel>16||!Number.isInteger(rawCC)||rawCC<0||rawCC>127||(midiKind==='cc'&&(!Number.isInteger(rawValue)||rawValue<0||rawValue>127)))throw Error('Use a channel from 1 to 16 and MIDI numbers/values from 0 to 127.');
          const bytes=midiKind==='pc'?[0xc0+rawChannel-1,rawCC]:[0xb0+rawChannel-1,rawCC,rawValue];
          hex=bytes.map(v=>v.toString(16).padStart(2,'0').toUpperCase()).join(' ');
        }
        const clean=hex.replace(/\s/g,'');
        if(!/^(?:[a-fA-F0-9]{2})+$/.test(clean))throw Error('Enter complete hexadecimal MIDI bytes.');
        const bytes=clean.match(/.{2}/g)!.map(v=>parseInt(v,16));
        if(rawIsSysex && !(bytes.length>=2&&bytes.length<=127&&bytes[0]===240&&bytes[bytes.length-1]===247&&bytes.slice(1,-1).every(v=>v<=127)))throw Error('Use a complete F0 … F7 message, with 7-bit data and at most 127 bytes.');
        action={...original,type:kind,message_hex:bytes.map(v=>v.toString(16).padStart(2,'0').toUpperCase()).join(' ')};
        if(kind==='midi'){if(midiLabel.trim())action.label=midiLabel.trim();else delete action.label;}
      } else if(kind==='reload_current_preset') {
        if(!reloadAllowed)throw Error('Reload must be the only action in this state.');
        action={...original,device_id:profileId,type:kind};
        for(const field of [...targetFields,'value'])delete action[field];
        if(original?.channel===undefined)delete action.channel;
      } else if(kind==='select_preset'||kind==='preset_volume') {
        const count=kind==='select_preset'?presetNames.length:101;
        if(!Number.isInteger(value)||value<0||value>=count)throw Error(kind==='select_preset'?'Choose a preset.':'Use a preset volume from 0 to 100.');
        if(kind==='select_preset'&&(!Number.isInteger(channel)||channel<1||channel>16))throw Error('Use a MIDI channel from 1 to 16.');
        action={...original,device_id:profileId,type:kind,value:Number(value)};
        for(const field of targetFields)delete action[field];
        if(kind==='select_preset')action.channel=Number(channel);else if(original?.channel===undefined)delete action.channel;
      } else {
        if(existingTankModel&&kind==='model'){dispatch('save',structuredClone(original!));return;}
        if(!profile||!block||(kind!=='enable'&&!model))throw Error('Choose a device, block and effect.');
        if(!Number.isInteger(channel)||channel<1||channel>16)throw Error('Use a MIDI channel from 1 to 16.');
        action={...original,device_id:profileId,block_id:blockId,model_id:modelId,channel:Number(channel),type:kind,value:kind==='enable'?enabled==='on':Number(value)};
        if(!model)delete action.model_id;
        delete action.param_id;
        if(kind==='toggle'){delete action.value;if(!canToggle)throw Error('This effect has no on/off toggle command in this profile.');}
        if(kind==='model'){delete action.value;if(!canModel)throw Error('This profile does not support MIDI model selection.');}
        if(kind==='parameter') {
          if(!parameter||!Number.isInteger(value)||value<low||value>high||(value-low)%step!==0)throw Error(`Use ${parameter?.label || 'a value'} from ${low} to ${high}, in steps of ${step}.`);
          action.param_id=parameterId;
        }
      }
      dispatch('save',action);
    } catch(error) {localError=(error as Error).message;}
  }
</script>

<div class="modal-backdrop">
  <div class="modal action-modal" role="dialog" aria-modal="true" aria-labelledby="action-title" data-testid="action-editor" tabindex="-1" use:trapFocus>
    <header class="modal-header"><div><span class="eyebrow">{original ? 'EDIT ACTION' : 'NEW ACTION'}</span><h2 id="action-title">{kind==='midi' ? 'MIDI command' : kind==='raw_midi' ? 'Imported MIDI command' : original ? 'Edit action' : 'Build your action'}</h2></div><button class="icon-button" aria-label="Close action editor" on:click={()=>dispatch('close')} disabled={saving}><Icon name="close" /></button></header>
    <div class="modal-body">
      {#if !original}<label>Command source<select bind:value={source} on:change={selectSource} data-testid="action-source"><option value="profile" disabled={!profiles.length}>Device profile</option><option value="midi">MIDI command · no profile needed</option></select></label>{/if}
      {#if kind==='raw_midi'||kind==='midi'}
        {#if kind==='midi'}
          <label>Name (optional)<input bind:value={midiLabel} placeholder="e.g. Delay on" data-testid="midi-label" /></label>
          <label>Message type<select bind:value={midiKind} data-testid="midi-kind"><option value="cc">Control Change (CC)</option><option value="pc">Program Change</option><option value="sysex">System Exclusive (SysEx)</option></select></label>
          <p class="field-hint">Use the commands documented by your device. A state can contain CC and Program Change, or up to 127 SysEx bytes.</p>
        {:else}<p class="muted">Chocolate Plus stores MIDI commands. Target devices and effect models cannot be inferred from these bytes.</p>{/if}
        {#if rawIsSysex}
          <label>SysEx message (hex)<textarea class="hex-input" rows="6" bind:value={rawHex} spellcheck="false" data-testid="raw-hex"></textarea></label>
          <p class="field-hint">One complete F0 … F7 message · hexadecimal bytes · 7-bit data · up to 127 bytes.</p>
        {:else}
          <div class="form-grid"><label>MIDI channel<input type="number" min="1" max="16" bind:value={rawChannel} data-testid="midi-channel" /></label><label>{midiKind==='pc'?'Program number':'CC number'}<input type="number" min="0" max="127" bind:value={rawCC} data-testid="raw-cc" /></label>{#if midiKind==='cc'}<label>Value<input type="number" min="0" max="127" bind:value={rawValue} data-testid="raw-value" /></label>{/if}</div>
          {#if midiKind==='pc'}<p class="field-hint">Program numbers are 0–127. If your device labels presets 1–128, subtract 1.</p>{/if}
        {/if}
      {:else}
        <label>Device profile<select bind:value={profileId} on:change={selectProfile} disabled={existingTankModel}>{#each profiles as p}<option value={p.id}>{p.name}</option>{/each}</select></label>
        <label>Action<select bind:value={kind} data-testid="action-kind" disabled={existingTankModel}><option value="enable" disabled={!canEnable}>Turn on / off</option>{#if hasToggles}<option value="toggle" disabled={!canToggle}>Toggle on / off (CC)</option>{/if}<option value="parameter" disabled={!parameters.length}>Set parameter</option>{#if canModel||original?.type==='model'}<option value="model" disabled={!canModel&& !existingTankModel}>{existingTankModel?'Existing TANK model command':'Select model (CC)'}</option>{/if}{#if presetNames.length}<option value="select_preset">Select preset</option>{/if}{#if supportsVolume}<option value="preset_volume">Set preset volume</option>{/if}{#if supportsReload}<option value="reload_current_preset" disabled={!reloadAllowed}>Reload current preset · experimental</option>{/if}</select></label>
        {#if kind==='reload_current_preset'}
          <div class="notice amber"><Icon name="alert" /><div><strong>Experimental · stock TANK Mini V09</strong><p>Restores the entire saved preset, including every effect’s on/off state, and discards temporary edits. Use this action alone in its state, in normal playing mode. Tested on a TANK over USB; not yet through the Chocolate Plus HOST port.</p></div></div>
        {:else if kind==='select_preset'}
          <div class="form-grid"><label>Preset<select bind:value={value} data-testid="action-preset">{#each presetNames as name,index}<option value={index}>{name}</option>{/each}</select></label><label>MIDI channel<input type="number" min="1" max="16" bind:value={channel} /></label></div>
          <p class="field-hint">Sends Program Change {value}. The device loads the saved preset, so unsaved changes on it are discarded.</p>
        {:else if kind==='preset_volume'}
          <label>Volume <span class="muted">0–100</span><div class="range-row"><input type="range" min="0" max="100" step="1" bind:value={value} aria-label="Preset volume slider" /><input type="number" min="0" max="100" step="1" bind:value={value} data-testid="volume-value" /></div></label>
          <p class="field-hint">Sets the volume of the preset playing on the TANK. Selecting or reloading a preset brings back its saved volume; nothing is saved.</p>
        {:else if existingTankModel}
          <div class="notice amber"><Icon name="info" /><p>This existing TANK model command is preserved exactly. Set effect models on the device before adding on/off or parameter actions. To replace this command, remove it and add a new action.</p></div>
        {:else}
          <div class="form-grid"><label>Effect block<select bind:value={blockId} on:change={selectBlock} data-testid="action-block">{#each profile?.blocks || [] as b}<option value={b.id}>{b.label}</option>{/each}</select></label><label>MIDI channel<input type="number" min="1" max="16" bind:value={channel} /></label></div>
          {#if block?.models.length}<label>{kind==='model'?'Model to select':'Effect already selected in the preset'}<select bind:value={modelId} on:change={selectModel} data-testid="action-model">{#each block?.models || [] as m}<option value={m.id}>{m.label}</option>{/each}</select></label><p class="field-hint">{kind==='model'?'Sends the model selection CC declared by this device profile.':'Choose the effect that is already on your device. This action controls it without changing the model.'}</p>{/if}
          {#if kind==='parameter'}
            <label>Parameter<select bind:value={parameterId} on:change={selectParameter} data-testid="action-parameter">{#each parameters as p}<option value={p.id}>{p.label}</option>{/each}</select></label>
            <label>Value <span class="muted">{low}–{high} · step {step}</span><div class="range-row"><input type="range" min={low} max={high} {step} bind:value={value} aria-label="Parameter value slider" /><input type="number" min={low} max={high} {step} bind:value={value} data-testid="parameter-value" /></div></label>
            <p class="field-hint">Default {Math.min(high,Math.max(low,parameter?.default??low))} · {profile?.protocol==='tank_v09'?'raw control value':'MIDI value'}{#if parameter?.display_range} · catalog display range {parameter.display_range.min}–{parameter.display_range.max}; no unit conversion applied{/if}</p>
          {:else if kind==='toggle'}
            <p class="field-hint" data-testid="toggle-hint">Sends CC {block?.toggle?.cc}. Each press switches {block?.label} off if it is on and on if it is off, so the result depends on the effect's state on the device.</p>
          {:else if kind==='enable'}
            <div class="segmented" aria-label="Effect state"><button class:active={enabled==='on'} on:click={()=>enabled='on'} data-testid="effect-on"><Icon name="power" />ON</button><button class:active={enabled==='off'} on:click={()=>enabled='off'} data-testid="effect-off"><Icon name="power" />OFF</button></div>
          {/if}
        {/if}
      {/if}
      <div class="action-summary"><span class="eyebrow">ACTION PREVIEW</span><strong>{summary}</strong></div>
      {#if localError||saveError}<div class="notice error" role="alert">{localError||saveError}</div>{/if}
    </div>
    <footer class="modal-footer"><button on:click={()=>dispatch('close')} disabled={saving}>Cancel</button><button class="primary" on:click={accept} disabled={saving} data-testid="action-save"><Icon name="check" />{saving?'Saving…':'Save action'}</button></footer>
  </div>
</div>
