import type { Request, Response, Snapshot } from './types';

const empty: Snapshot = {
  profiles: [], project: null, origin: 'No project', dirty: false, file_name: null, zoom_percent: 100,
  controller: {devices: [], connected: false, busy: false, identity: null, summary: '', preview: null, status: 'Desktop bridge unavailable', can_import: false, can_write: false}, compiled: null,
};

export function isBrowserPreview(): boolean { return !window.pywebview?.api; }

export const request: Request = async (method, payload = {}) => {
  if (window.__STOMPILOT_REQUEST__) return window.__STOMPILOT_REQUEST__(method, payload);
  if (!window.pywebview?.api) {
    // Allow the desktop host to finish injecting the bridge before falling back.
    await new Promise<void>(resolve => {
      const timer = window.setTimeout(resolve, 650);
      window.addEventListener('pywebviewready', () => { window.clearTimeout(timer); resolve(); }, {once:true});
    });
  }
  if (window.pywebview?.api) return window.pywebview.api.request(method, payload);
  if (method === 'bootstrap') return {ok: true, data: structuredClone(empty)};
  if (method === 'set_zoom') {
    empty.zoom_percent = Number(payload.percent);
    return {ok: true, data: structuredClone(empty)};
  }
  return {ok: false, error: {message:'This browser preview has no desktop bridge. Launch Stompilot to open files or connect a controller.'}} satisfies Response;
};
