import type { Request, Response, Result, Snapshot } from './types';

const empty: Snapshot = {
  profiles: [], project: null, origin: 'No project', dirty: false, file_name: null, zoom_percent: 100,
  controller: {devices: [], connected: false, busy: false, identity: null, summary: '', preview: null, status: 'Desktop bridge unavailable', can_write: false}, compiled: null,
};

export function isBrowserPreview(): boolean { return !window.pywebview?.api && !window.StompilotAndroid; }
export function isAndroid(): boolean { return !!window.StompilotAndroid; }

const pending = new Map<string, (json: string) => void>();
let sequence = 0;

/** Android answers asynchronously through window.__stompilotResolve(id, json). */
function androidRequest(method: string, payload: Record<string, unknown>): Promise<string> {
  const host = window.StompilotAndroid!;
  window.__stompilotResolve ??= (id, json) => { const resolve = pending.get(id); pending.delete(id); resolve?.(json); };
  return new Promise(resolve => {
    const id = String(++sequence);
    pending.set(id, resolve);
    host.request(id, method, JSON.stringify(payload));
  });
}

/** Requests that return something other than the editor snapshot (the Android live view). */
export async function hostRequest<T>(method: string, payload: Record<string, unknown> = {}): Promise<Result<T>> {
  if (window.__STOMPILOT_REQUEST__) return window.__STOMPILOT_REQUEST__(method, payload) as unknown as Promise<Result<T>>;
  if (!window.StompilotAndroid) return {ok: false, error: {message: 'The live view needs the Android app.'}};
  return JSON.parse(await androidRequest(method, payload)) as Result<T>;
}

export const request: Request = async (method, payload = {}) => {
  if (window.__STOMPILOT_REQUEST__) return window.__STOMPILOT_REQUEST__(method, payload);
  if (window.StompilotAndroid) return JSON.parse(await androidRequest(method, payload)) as Response;
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
