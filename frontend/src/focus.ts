/** Keep keyboard focus inside a modal and restore the triggering control. */
export function trapFocus(node:HTMLElement) {
  const previous=document.activeElement as HTMLElement|null;
  const elements=()=>Array.from(node.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]'));
  queueMicrotask(()=>{(elements()[0]||node).focus();});
  const key=(event:KeyboardEvent)=>{
    if(event.key!=='Tab')return;
    const choices=elements();
    if(!choices.length){event.preventDefault();node.focus();return;}
    const first=choices[0],last=choices[choices.length-1];
    if(event.shiftKey&&(document.activeElement===first||document.activeElement===node)){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  };
  node.addEventListener('keydown',key);
  return {destroy(){node.removeEventListener('keydown',key);previous?.focus();}};
}
