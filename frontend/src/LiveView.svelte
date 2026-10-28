<script lang="ts">
  import {createEventDispatcher, onDestroy, onMount} from 'svelte';
  import Icon from './Icon.svelte';
  import {hostRequest, isAndroid} from './bridge';
  import type {Action, ChocolatePage, Profile, Snapshot, TankState} from './types';

  /**
   * Full-screen live mode: the page name at the top, switches A-D below.
   * Opening it starts forwarding the Chocolate Plus to the TANK; closing it
   * (X or the phone's back button) stops forwarding.
   */
  export let snapshot: Snapshot | null;
  /** Runs an editor request and updates the shared snapshot (App's run). */
  export let run: (method: string) => Promise<unknown>;

  const dispatch = createEventDispatcher<{close: void}>();
  const SWITCHES = ['A', 'B', 'C', 'D'];
  const BURST_MS = 40;

  let running = false, starting = false;
  let problem = '';
  let route = '';
  let pageIndex = 0;
  let tank: TankState | null = null;
  let tankError = '';
  let pressed = '';
  let lastState: Record<string, number> = {};
  let unmatched = '';
  let burst: string[] = [];
  let burstTimer = 0, pollTimer = 0, refreshTimer = 0, pressTimer = 0;

  $: project = snapshot?.project ?? null;
  $: compiled = snapshot?.compiled ?? null;
  $: pages = project?.pages ?? [];
  $: if (pageIndex >= pages.length) pageIndex = Math.max(0, pages.length - 1);
  $: page = pages[pageIndex];
  $: compiledPage = compiled?.pages[pageIndex];

  function profileOf(action: Action): Profile | undefined {
    return snapshot?.profiles.find(p => p.id === action.device_id);
  }

  /** Compiled summaries start with the profile name, which the square does not need. */
  function short(summary: string): string {
    for (const profile of snapshot?.profiles ?? []) {
      for (const separator of [' · ', ': ']) {
        if (summary.startsWith(profile.name + separator)) return summary.slice(profile.name.length + separator.length);
      }
    }
    return summary;
  }

  type Chip = {key: string; text: string; on: boolean | null};
  // The TANK state is a parameter, so the template re-runs this when it changes.
  function chips(actions: Action[], tank: TankState | null): Chip[] {
    const result: Chip[] = [];
    for (const action of actions) {
      const profile = profileOf(action);
      if (action.block_id) {
        const block = tank?.blocks.find(b => b.id === action.block_id);
        const label = profile?.blocks.find(b => b.id === action.block_id)?.label ?? action.block_id;
        result.push({key: `block-${action.block_id}`, text: block ? `${label} · ${block.model} · ${block.on ? 'ON' : 'OFF'}` : `${label} · ?`, on: block ? block.on : null});
      } else if (action.type === 'select_preset') {
        const name = profile?.device_actions?.find(a => a.id === 'select_preset')?.presets?.[action.value as number] ?? String(action.value);
        result.push({key: `preset-${name}`, text: `Preset ${name}`, on: tank ? tank.preset === name : null});
      } else if (action.type === 'preset_volume') {
        result.push({key: 'volume', text: tank ? `Volume ${tank.volume} → ${action.value}` : `Volume → ${action.value}`, on: tank ? tank.volume === action.value : null});
      }
    }
    return result.filter((chip, index) => result.findIndex(other => other.key === chip.key) === index);
  }

  type Match = {page: number; switchId: string; state: number};
  /** Find the switch state whose compiled messages are exactly this burst; prefer the shown page. */
  function match(hexes: string[]): Match | null {
    if (!compiled) return null;
    const found: Match[] = [];
    compiled.pages.forEach((compiledPage, pageNumber) => {
      for (const [switchId, data] of Object.entries(compiledPage.switches)) {
        data.states.forEach((state, stateNumber) => {
          const sent = state.messages_hex;
          if (sent.length && sent.length === hexes.length && sent.every((message, index) => message === hexes[index])) {
            found.push({page: pageNumber, switchId, state: stateNumber});
          }
        });
      }
    });
    return found.find(candidate => candidate.page === pageIndex) ?? found[0] ?? null;
  }

  function received(event: Event) {
    const detail = (event as CustomEvent<{hex: string; forwarded: boolean}>).detail;
    if (!running) return;
    burst.push(detail.hex);
    if (!detail.forwarded) problem = 'The last message did not reach the TANK. Check its USB connection.';
    window.clearTimeout(burstTimer);
    burstTimer = window.setTimeout(endBurst, BURST_MS);
  }

  function endBurst() {
    const hexes = burst;
    burst = [];
    const found = match(hexes);
    if (found) {
      pageIndex = found.page;
      lastState = {...lastState, [`${found.page}:${found.switchId}`]: found.state};
      pressed = found.switchId;
      unmatched = '';
      window.clearTimeout(pressTimer);
      pressTimer = window.setTimeout(() => (pressed = ''), 700);
    } else {
      unmatched = hexes.join('  ');
    }
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(refreshTank, 350);
  }

  async function refreshTank() {
    const result = await hostRequest<TankState>('tank_state');
    if (result.ok) {
      tank = result.data;
      tankError = '';
    } else if (result.error.message !== 'The TANK is being read.') {
      tankError = result.error.message;
    }
  }

  async function start() {
    problem = '';
    if (!isAndroid()) {
      problem = 'Live forwarding runs in the Android app, with the Chocolate Plus (switch on U) and the TANK Mini on the phone\'s USB hub.';
      return;
    }
    starting = true;
    try {
      // The editor's connection to the Chocolate would pause forwarding.
      if (snapshot?.controller.connected) await run('controller_disconnect');
      const position = await hostRequest<ChocolatePage>('chocolate_page');
      if (position.ok) pageIndex = Math.min(position.data.page - 1, Math.max(0, pages.length - 1));
      const live = JSON.parse(window.StompilotAndroid!.startLive()) as {ok: boolean; message?: string; chocolate?: string; tank?: string};
      if (!live.ok) {
        problem = live.message ?? 'Forwarding could not start.';
        return;
      }
      route = `${live.chocolate} → ${live.tank}`;
      running = true;
      await refreshTank();
      window.clearInterval(pollTimer);
      pollTimer = window.setInterval(refreshTank, 3000);
    } finally {
      starting = false;
    }
  }

  function stop() {
    if (running) window.StompilotAndroid?.stopLive();
    running = false;
    window.clearInterval(pollTimer);
  }

  function close() {
    dispatch('close');
  }

  onMount(() => {
    window.addEventListener('stompilot-live-message', received);
    window.addEventListener('stompilot-back', close);
    window.StompilotAndroid?.setLiveScreen(true);
    start();
  });
  onDestroy(() => {
    window.removeEventListener('stompilot-live-message', received);
    window.removeEventListener('stompilot-back', close);
    window.clearTimeout(burstTimer);
    window.clearTimeout(refreshTimer);
    window.clearTimeout(pressTimer);
    stop();
    window.StompilotAndroid?.setLiveScreen(false);
  });
</script>

<div class="live-screen" role="dialog" aria-modal="true" aria-labelledby="live-page-name" data-testid="live-view">
  <header class="live-top">
    <div class="live-title">
      <h1 id="live-page-name" data-testid="live-page-name">{page?.name ?? 'No project open'}</h1>
      <p class="live-line" role="status">
        {#if starting}<span>Connecting…</span>
        {:else if running}<span class="live-ok">Live · {route}</span>{/if}
        {#if pages.length > 1}<span>Page {pageIndex + 1} of {pages.length}</span>{/if}
        {#if tank}<span>{tank.name} · preset {tank.preset} · volume {tank.volume}</span>{/if}
        {#if problem}<span class="live-warning" data-testid="live-problem">{problem}</span>{#if !running && !starting && isAndroid()}<button class="live-retry" on:click={start} data-testid="live-retry">Try again</button>{/if}{/if}
        {#if tankError}<span class="live-warning">{tankError}</span>{/if}
        {#if unmatched}<span class="live-warning">Not in this project: <code>{unmatched}</code></span>{/if}
        {#if !project}<span class="live-warning">Open the project written to the Chocolate Plus to see what each switch sends.</span>{/if}
      </p>
    </div>
    <button class="live-close" aria-label="Close live view" on:click={close} data-testid="live-close"><Icon name="close" size={30} /></button>
  </header>

  <div class="live-grid">
    {#each SWITCHES as id}
      {@const data = page?.switches[id]}
      {@const compiledSwitch = compiledPage?.switches[id]}
      <article class="live-switch" class:pressed={pressed === id} data-testid={`live-switch-${id}`}>
        <div class="live-switch-head">
          <span class="live-letter">{id}</span>
          <strong>{data?.label ?? 'Not assigned'}</strong>
        </div>
        {#if data && compiledSwitch}
          <div class="live-states">
            {#each data.states as state, stateNumber}
              {@const sent = compiledSwitch.states[stateNumber]}
              <div class="live-state" class:active={lastState[`${pageIndex}:${id}`] === stateNumber}>
                {#if data.mode === 'alternating'}<span class="live-state-label">{stateNumber + 1}. {state.label}</span>{/if}
                {#each sent?.summaries ?? [] as summary}<span class="live-action">{short(summary)}</span>{:else}<span class="live-muted">No commands</span>{/each}
              </div>
            {/each}
          </div>
          <div class="live-chips">
            {#each chips(data.states.flatMap(state => state.actions), tank) as chip (chip.key)}
              <span class="live-chip" class:on={chip.on === true} class:off={chip.on === false}>{chip.text}</span>
            {/each}
          </div>
        {/if}
      </article>
    {/each}
  </div>
</div>

<style>
  .live-screen {
    position: fixed; inset: 0; z-index: 100; display: flex; flex-direction: column; gap: .8rem;
    background: #0c141b; color: #e5edf2;
    padding: max(12px, env(safe-area-inset-top, 0px)) max(12px, env(safe-area-inset-right, 0px))
             max(12px, env(safe-area-inset-bottom, 0px)) max(12px, env(safe-area-inset-left, 0px));
  }
  .live-top { display: flex; align-items: flex-start; gap: .75rem; }
  .live-title { flex: 1; min-width: 0; }
  .live-title h1 { margin: 0; font-size: 2.3rem; line-height: 1.1; overflow-wrap: break-word; text-wrap: balance; }
  .live-line { margin: .35rem 0 0; display: flex; flex-wrap: wrap; gap: .3rem .9rem; align-items: center; font-size: .82rem; color: #7f9eaf; }
  .live-ok { color: #7ae4c0; }
  .live-warning { color: #e4bd79; overflow-wrap: anywhere; }
  .live-warning code { font-size: .78rem; }
  .live-retry { font-size: .8rem; padding: .3rem .6rem; min-height: 0; }
  .live-close { flex: none; display: grid; place-items: center; width: 3rem; height: 3rem; padding: 0; border-radius: 50%;
    background: #182b36; border: 1px solid #304955; color: #e5edf2; }
  .live-grid { flex: 1; min-height: 0; display: grid; gap: .65rem;
    grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: repeat(2, minmax(0, 1fr)); }
  @media (orientation: landscape) { .live-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); grid-template-rows: minmax(0, 1fr); } }
  .live-switch { display: flex; flex-direction: column; gap: .5rem; min-width: 0; min-height: 0; overflow: hidden; padding: .8rem;
    background: #13222d; border: 2px solid #2a3f4b; border-radius: 12px; transition: border-color .2s, background .2s; }
  .live-switch.pressed { border-color: #55dcbc; background: #173a33; }
  .live-switch-head { display: flex; align-items: center; gap: .55rem; min-width: 0; }
  .live-switch-head strong { flex: 1; min-width: 0; overflow-wrap: break-word; font-size: 1.25rem; line-height: 1.2; }
  .live-letter { flex: none; display: grid; place-items: center; width: 2.4rem; height: 2.4rem; border-radius: 8px;
    background: #1e3440; color: #55dcbc; font-weight: 750; font-size: 1.35rem; }
  .live-states { display: grid; gap: .3rem; min-height: 0; overflow: auto; }
  .live-state { display: grid; gap: .15rem; padding: .4rem .5rem; border-radius: 8px; border: 1px solid transparent; }
  .live-state.active { border-color: #3d715d; background: #183732; }
  .live-state-label { font-size: .8rem; color: #a8c3d0; font-weight: 650; }
  .live-action { font-size: .95rem; overflow-wrap: break-word; }
  .live-muted { color: #7f9eaf; font-size: .9rem; }
  .live-chips { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: auto; }
  .live-chip { font-size: .8rem; padding: .25rem .45rem; border-radius: 5px; border: 1px solid #3b5361; background: #273b45; color: #a8c3d0; overflow-wrap: anywhere; }
  .live-chip.on { background: #21483c; color: #7ae4c0; border-color: #3d715d; }
  .live-chip.off { color: #7f9eaf; }
  @media (prefers-reduced-motion: reduce) { .live-switch { transition: none; } }
</style>
