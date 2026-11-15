export type Parameter = { id: string; label: string; min?: number; max?: number; step?: number; default?: number; unit?: string; display_range?: {min:number;max:number;note?:string} };
export type Model = { id: string; label: string; parameters: Parameter[];cc?:number;wire_value?:number };
export type Block = { id: string; label: string; models: Model[]; enable?: object; toggle?: {cc:number;value?:number} };
export type Profile = { id: string; name: string; protocol: string; firmware?: string; channel?: number; blocks: Block[]; evidence?: string | {status:string;note?:string}; notes?: string[]; sources?: {title?:string;url?:string;note?:string}[]; device_actions?: {id:string;label:string;experimental?:boolean;warnings?:string[];presets?:string[]}[] };
export type Action = {type:string;device_id?:string;block_id?:string;model_id?:string;param_id?:string;channel?:number;value?:number|boolean;message_hex?:string;label?:string;[key:string]:unknown};
export type State = {label:string;actions:Action[];native_bank?:unknown;[key:string]:unknown};
export type Switch = {label:string;mode:'single'|'alternating';states:State[];[key:string]:unknown};
export type Page = {name:string;switches:Record<string,Switch>;[key:string]:unknown};
export type Project = {schema_version:number;name:string;controller:{id:string;max_sysex_bytes:number};pages:Page[];[key:string]:unknown};
export type CompiledState = {messages_hex:string[];sysex_hex:string;midi_rows:{channel:number;type:'CC'|'PC';data1:number;data2:number;hex:string}[];summaries:string[];summary:string;evidence_status:string;warnings:string[];byte_count:number;label:string};
export type CompiledProject = {pages:{name:string;switches:Record<string,{label:string;mode:string;states:CompiledState[]}>}[]};
export type Snapshot = {
  profiles:Profile[]; project:Project|null; origin:string; dirty:boolean; file_name:string|null; zoom_percent:number;
  controller:{devices:{name:string;input_name:string;output_name:string}[];connected:boolean;busy:boolean;identity:{name:string;firmware_version:string|number}|null;summary:string;preview:{summaries:string[];warnings:string[];changed_bytes:number}|null;status:string;can_write:boolean;pending_save?:{status:string;backup_name:string;restored:boolean;message:string}|null};
  compiled:CompiledProject|null;
};
export type Response = {ok:true;data:Snapshot}|{ok:false;error:{message:string}};
export type Request = (method:string,payload?:Record<string,unknown>)=>Promise<Response>;
export type Result<T> = {ok:true;data:T}|{ok:false;error:{message:string}};
/** The Android app's JavaScript interface; requests answer through window.__stompilotResolve. */
export type AndroidHost = {
  request(id:string,method:string,payload:string):void;
  /** Full screen without Android's bars, screen kept on, back button closes the live view. */
  setLiveScreen(on:boolean):void;
  startLive():string;
  stopLive():void;
};
export type TankBlock = {id:string;label:string;on:boolean;model:string};
export type TankState = {name:string;preset:string;volume:number;blocks:TankBlock[]};
export type ChocolatePage = {page:number;pages:number;device:string};
declare global {
  interface Window {
    pywebview?:{api:{request:Request}}; __STOMPILOT_REQUEST__?:Request;
    StompilotAndroid?:AndroidHost; __stompilotResolve?:(id:string,json:string)=>void;
  }
}
