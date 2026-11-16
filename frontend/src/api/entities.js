import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const channels={
  masters:()=>api('/channels'),
  master:id=>api('/channels/'+id),
  pending:()=>api('/channels/pending'),
  operations:()=>api('/channels/operations'),
  trace:name=>api('/channels/trace?name='+encodeURIComponent(name)),
  addAlias:data=>api('/channels/aliases',{method:'POST',body:JSON.stringify(data)}),
  claim:(entryId,data)=>api('/channels/entries/'+entryId+'/claim',{method:'POST',body:JSON.stringify(data)}),
  merge:data=>api('/channels/merge',{method:'POST',body:JSON.stringify(data)}),
  undo:data=>api('/channels/undo',{method:'POST',body:JSON.stringify(data)}),
};
