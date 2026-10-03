<script lang="ts">
  export let name = 'plus';
  export let size = 18;
  const paths: Record<string,string> = {
    plus:'M12 5v14M5 12h14', minus:'M5 12h14', close:'m6 6 12 12M6 18 18 6',
    folder:'M3 7V5h6l2 2h10v12H3V7Z', save:'M5 3h12l4 4v14H3V3h2Zm2 0v6h10V3M7 21v-8h10v8',
    export:'M12 16V3m-4 4 4-4 4 4M5 13v7h14v-7', file:'M14 2H5v20h14V7l-5-5Zm0 0v6h5',
    usb:'M12 20V3m-3 3 3-3 3 3M12 13l-5-4V6m5 10 5-4V8M5 6h4M15 8h4v4h-4M10 20a2 2 0 1 0 4 0 2 2 0 1 0-4 0',
    grid:'M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z',
    chip:'M7 7h10v10H7V7ZM9 3v4m6-4v4M9 17v4m6-4v4M3 9h4m-4 6h4M17 9h4m-4 6h4',
    down:'m6 9 6 6 6-6', up:'m6 15 6-6 6 6', left:'m15 6-6 6 6 6', right:'m9 6 6 6-6 6',
    edit:'m16 3 5 5-12 12H4v-5L16 3Zm-3 3 5 5', trash:'M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7',
    copy:'M8 8h13v13H8V8ZM16 8V3H3v13h5', check:'m4 12 5 5L20 6',
    refresh:'M20 7a9 9 0 1 0 1 8M20 3v5h-5', info:'M12 11v6m0-10v1M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
    bolt:'m13 2-9 12h7l-1 8 10-12h-7l0-8Z',
    power:'M12 2v10M6 5a9 9 0 1 0 12 0', eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
    alert:'m12 3 10 18H2L12 3Zm0 5v6m0 3v1', home:'m3 11 9-8 9 8M5 9v12h14V9M9 21v-7h6v7',
  };
</script>

<svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d={paths[name] || paths.plus} /></svg>
