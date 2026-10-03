import type {Profile} from './types';

/** Normalize display fields without changing the profile stored by Python. */
export function displayProfile(profile:Profile):Profile {
  const raw=profile as unknown as Record<string,unknown>;
  return {
    ...profile,
    blocks:Array.isArray(raw.blocks)?profile.blocks.map(block=>({...block,models:Array.isArray(block.models)?block.models.map(model=>({...model,parameters:Array.isArray(model.parameters)?model.parameters:[]})):[]})):[],
    notes:Array.isArray(raw.notes)?raw.notes.filter((note):note is string=>typeof note==='string'):typeof raw.notes==='string'?[raw.notes]:[],
    sources:Array.isArray(raw.sources)?raw.sources.map(source=>typeof source==='string'?{title:source}:source).filter((source):source is NonNullable<Profile['sources']>[number]=>!!source&&typeof source==='object'):[],
  };
}
