import { Alert, Button, Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle, Input, Tabs, TabsList, TabsTab, Textarea } from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { apiUrl, postJson, useResource, type FlowDocument } from '../api';
import { useProjects } from '../parts';
import { projectScope } from '../router';
import { DataTable } from '../components/blocks/tables/data-table/DataTable';
import type { DataTableColumn } from '../components/blocks/tables/data-table/data-table-utils';

type Model = { provider: string; model: string };
type Step = { skipped?: boolean; id: string; task: string; selected?: number; reusable?: number; pages?: number; records?: number };
type Source = { title: string; repository: string; number: number | null; revision: string; state: string; body: string; epics: string[]; card?: { text: string; partial: boolean } };
type Dependency = { source: string; title: string; revision: string; passage: string; basis: string };
type Pair = { prerequisites?: Dependency[]; id: string; issue: Source; story: Source; reason: string; decision: string };
type Preview = { second_model?: DecisionModel | null; decision_model?: DecisionModel | null; generation?: string; flow: string; eligible: number; selected: number; reusable: number; external: number; total: number; page: number; steps: Step[]; items: Pair[] };
type Native = {contribution?: string; scope: string; provider: string; model: string; rubric: string; link_probability: number | null; strength?: number | null; review_reason: string; cached_from?: string; eur: string | null};
type DecisionModel = Model & {available: boolean; rates: {input:number; output:number; cached?:number; request?:number} | null; endpoint: string; rubric: string};
type Advisory = { human_scope?: string; status: 'advisory' | 'needs_review'; scope: string | null; reason: string; existing_link?: boolean };
type Run = { second_opinion?: Native | null; advisory?: Advisory | null; native?: Native | null; id: string; state: string; flow: string; version: number; model: string; cached_from?: string; spent: number; reserved: number; cap: number; detail: string; steps: Step[]; sample?: string[];
  failed_request?: {arm: string; reason: string; provider_ref?: string | null};
  arms: {arm: string; scope: string | null; evidence: string; tokens: number; input: number; output: number; eur: string | null; provider_ref?: string | null}[] };
type Status = { second_model?: DecisionModel | null; decision_model?: DecisionModel | null; generation?: string; paired: boolean; paid: boolean; models: Model[]; configured: Model | null; flow: string; run: Run | null };
const keyOf = (m: Model) => JSON.stringify([m.provider, m.model]);
const active = ['queued','calling','held','stopping','paused'];
const labels: Record<string,string> = {
  'evidence.relationships':'Read project relationships','evidence.candidates':'Find related records','evidence.reuse':'Reuse review decisions',
  'evidence.decisions':'Review issue links with Decisions', 'evidence.second_opinion':'Check a negative or uncertain decision', 'evidence.wiki':'Read wiki evidence','evidence.compare':'Compare original and wiki evidence',
};
const optionsClass = 'rounded-md border border-border bg-background px-2 py-1 text-sm';

function Passage({ row }: { row: Source }) {
  return <details className="min-w-40 max-w-lg whitespace-normal">
    <summary className="cursor-pointer text-primary">{row.title}</summary>
    <p className="my-1 text-xs text-muted-foreground">{row.repository}{row.number ? ' #' + row.number : ''} · {row.state} · {row.revision.slice(0,12)}</p>
    {!!row.epics.length && <p className="mt-1 text-xs">Epics · {row.epics.join(', ')}</p>}
    {row.card && <p className="my-2 whitespace-pre-wrap">{row.card.text}</p>}
    <details><summary className="cursor-pointer">Original source</summary><p className="my-2 whitespace-pre-wrap">{row.body}</p></details>
  </details>;
}
function DependencySources({items}: {items?: Dependency[]}) {
  if (!items?.length) return null;
  return <details className="mt-1 max-w-lg whitespace-normal">
    <summary className="cursor-pointer text-primary">Dependency evidence</summary>
    {items.map(item=><div key={item.source} className="my-2">
      <strong>{item.source} · {item.title}</strong>
      <p className="text-xs text-muted-foreground">{item.basis} · {item.revision}</p>
      <p className="whitespace-pre-wrap">{item.passage}</p>
    </div>)}
  </details>;
}

const columns: DataTableColumn<Pair>[] = [
  {id:'issue',header:'Technical issue',accessor:r=>r.issue.title,sortable:false,cell:r=><Passage row={r.issue}/>},
  {id:'story',header:'User Story',accessor:r=>r.story.title,sortable:false,cell:r=><Passage row={r.story}/>},
  {id:'reason',header:'Why selected',accessor:r=>r.reason,sortable:false,cell:r=><>{r.reason}<DependencySources items={r.prerequisites}/></>},
  {id:'decision',header:'Saved decision',accessor:r=>r.decision,sortable:false},
];

export function EvidencePanel({project, draft}: {project:string; draft?: FlowDocument}) {
  const path='projects/'+encodeURIComponent(project)+'/evidence';
  const [poll,setPoll]=useState(false);
  const status=useResource<Status>(apiUrl(path),poll ? 2500 : null);
  const data=status.data;
  const run=data?.run;
  useEffect(()=>setPoll(!!run && ['queued','calling','stopping'].includes(run.state)),[run?.state]);
  const [pick,setPick]=useState('');
  const fallback=data?.models.find(m=>data.configured && keyOf(m)===keyOf(data.configured)) ?? data?.models.find(m=>m.provider==='anthropic') ?? data?.models[0];
  const chosen=data?.models.find(m=>keyOf(m)===pick) ?? fallback;
  const [cap,setCap]=useState('12000');
  const [view,setView]=useState<Preview|null>(null);
  const [query,setQuery]=useState<{page:number;search:string;nonce:number}|null>(null);
  const decisionModel = draft ? view?.decision_model : data?.decision_model;
  const secondModel = draft ? view?.second_model : data?.second_model;
  const unavailable = decisionModel?.available === false || secondModel?.available === false;
  const generation = draft ? view?.generation : data?.generation;
  const [waiting,setWaiting]=useState(false);
  const [busy,setBusy]=useState(false);
  const [problem,setProblem]=useState('');
  useEffect(()=>{setView(null);setQuery(null);setProblem('');},[draft,project,pick]);
  useEffect(()=>{
    if(!query)return;
    let alive=true;
    setWaiting(true);
    postJson<Preview>(path+'/preview',{document:draft,provider:chosen?.provider,model:chosen?.model,page:query.page,search:query.search},{quiet:true})
      .then(value=>{if(alive){setView(value);setProblem('');}})
      .catch(e=>{if(alive)setProblem(e instanceof Error?e.message:'Preview failed.');})
      .finally(()=>{if(alive)setWaiting(false);});
    return ()=>{alive=false;};
  },[path,query,draft,chosen?.provider,chosen?.model]);
  const [attempt,setAttempt]=useState(()=>crypto.randomUUID());
  async function change(action?:string, candidate?:string){
    if(busy)return;
    setBusy(true);setProblem('');
    try {
      if(action) await postJson(path+'/control',{run_id:run?.id,action});
      else {
        await postJson(path+'/runs',{request_id:attempt,candidate,provider:chosen?.provider,model:chosen?.model,token_cap:Number(cap)});
        setAttempt(crypto.randomUUID());
      }
      status.reload();
    } catch(e){setProblem(e instanceof Error?e.message:'Evidence review failed.');}
    finally{setBusy(false);}
  }
  return <section aria-label="Review evidence" className="grid gap-3 rounded-xl border border-border bg-card p-5">
    {draft && <h2 className="text-lg font-semibold">Preview this flow</h2>}
    {data && <div className="flex flex-wrap items-end gap-3">
      {!generation && (!decisionModel || data.paired || draft) && !!data.models.length && <label className="grid gap-1 text-sm">Accounts model<select className={optionsClass} value={chosen?keyOf(chosen):''} onChange={e=>setPick(e.target.value)}>
        {data.models.map(m=><option key={keyOf(m)} value={keyOf(m)}>{m.provider} · {m.model}</option>)}</select></label>}
      {generation && <p className="text-sm">Generation · {generation}</p>}
      {[{label:'Decision model', model:decisionModel}, {label:'Conditional second opinion', model:secondModel}].map(({label, model}) => model && <div key={label} className="grid gap-1 text-sm"><strong>{label} · {model.provider} · {model.model}</strong>
        <span>{model.endpoint} · {model.rubric} · Administrator review</span>
        <span>{model.rates ? `USD per million tokens · ${model.rates.input} input · ${model.rates.output} output${model.rates.cached === undefined ? '' : ' · '+model.rates.cached+' cached input'}${model.rates.request ? ' · $'+model.rates.request+' per request' : ''}` : 'Price unavailable'}</span>
        {!model.available && <span>Choose an available model in the Flow Designer or check its account.</span>}</div>)}
      {secondModel && <p className="basis-full text-sm text-muted-foreground">One pair per run. A negative, incomplete or conflicting first judgment triggers a second opinion on the same original sources.</p>}
      <Button variant="secondary" size="sm" loading={waiting} disabled={waiting} onClick={()=>setQuery({page:1,search:'',nonce:Date.now()})}>Preview related records</Button>
      {!draft && !(run && active.includes(run.state)) && <>
        {data.paid && <label className="grid gap-1 text-sm">Pilot token cap<Input aria-label="Pilot token cap" type="number" min="1" max="2000000" value={cap} onChange={e=>setCap(e.target.value)} /></label>}
        <Button size="sm" disabled={busy || unavailable || (data.paid && ((data.paired && !chosen && !data.generation) || Number(cap)<=0 || Number(cap)>2000000))} loading={busy}
          onClick={()=>void change()}>{data.decision_model?'Run decision review':data.paid?'Run paired comparison':'Run evidence flow'}</Button>
      </>}
    </div>}
    {status.error && <Alert tone="warning" title={status.error}/>}
    {problem && <Alert tone="warning" title={problem}/>}
    {view && <>
      <p className="text-sm">{view.flow} · {view.selected.toLocaleString()} selected of {view.eligible.toLocaleString()} eligible pairs · {view.reusable} reusable · {view.external} across projects · 0 model calls</p>
      <p className="text-xs text-muted-foreground">{view.steps.map(s=>labels[s.task]).join(' → ')}</p>
      <DataTable caption="Related records" rows={view.items} columns={!draft && data?.decision_model ? [...columns, {id:'review',header:'Decision review',sortable:false,cell:r=><Button size="sm" variant="secondary" disabled={busy || !!(run && active.includes(run.state)) || unavailable || r.decision!=='unchecked' || Number(cap)<=0 || Number(cap)>2000000} onClick={()=>void change(undefined,r.id)}>Review this pair</Button>}] : columns} getRowId={r=>r.id}
        selectionMode="none" showQuickFilter showColumnMenu={false} quickFilter={query?.search??''} quickFilterDebounceMs={350}
        paginationMode="server" paginated={view.total>0} page={view.page} pageSize={10} pageSizeOptions={[10]} rowCount={view.total}
        loading={waiting} labels={{quickFilter:()=>'Search issues or Stories'}} emptyState="No related records." noResultsState="No matching records."
        onQueryChange={q=>setQuery(prev=>prev?.page===q.page && prev.search===(q.quickFilter??'')?prev:{page:q.page,search:(q.quickFilter??'').slice(0,200),nonce:Date.now()})}/>
    </>}
    {!draft && run && <div className="grid gap-2 border-t border-border pt-3 text-sm" aria-live="polite">
      <p><strong>{run.flow}</strong> · v{run.version} · {run.state} · {run.spent.toLocaleString()} tokens used / {run.cap.toLocaleString()} cap{run.reserved>0?' · '+run.reserved.toLocaleString()+' reserved':''}</p>
      {run.cached_from && <p>Reused both answers from a completed comparison. The arm totals below are from that run.</p>}
      <DecisionResults primary={run.native} second={run.second_opinion} advisory={run.advisory}/>
      {run.detail && <p>{run.detail}</p>}{run.sample && <p>{run.sample.join(' → ')}</p>}
      {run.steps.length>0 && <p>{run.steps.map(s=>labels[s.task]+(s.skipped ? ' (skipped)' : '')).join(' → ')}</p>}
      {run.failed_request && <p>{run.failed_request.arm === 'second_opinion' ? 'Second opinion' : run.failed_request.arm === 'wiki' ? 'Wiki excerpts' : 'Original passages'} · {run.failed_request.reason}{run.failed_request.provider_ref && ' · Provider reference '+run.failed_request.provider_ref}</p>}
      {run.arms.map(a=><p key={a.arm}><strong>{a.arm==='wiki'?'Wiki excerpts':'Original passages'}</strong> · {a.scope ?? 'No verdict'} · {a.input} input / {a.output} output tokens · {a.eur===null?'EUR estimate unavailable':'€'+a.eur}{a.evidence && ' · '+a.evidence}</p>)}
      <div className="flex gap-2">{['queued','calling'].includes(run.state) && <Button size="sm" variant="secondary" disabled={busy} onClick={()=>void change('stop')}>Stop</Button>}
        {['paused','held'].includes(run.state) && <><Button size="sm" disabled={busy} onClick={()=>void change('resume')}>Resume</Button><Button size="sm" variant="secondary" disabled={busy} onClick={()=>void change('end')}>End run</Button></>}</div>
    </div>}
    {!draft && <VerdictReviews path={path+'/reviews'} latest={run ? run.id+' '+run.state : ''}/>}
  </section>;
}

function NativeResult({value}: {value: Native}) {
  return <div className="grid gap-1 text-sm"><strong>{value.provider} · {value.model}</strong><span>Link probability · {value.link_probability == null ? "No answer" : (100 * value.link_probability).toFixed(1)+"%"} · Scope · {scopeText(value.scope)}{value.contribution ? ' · Contribution · '+value.contribution : value.rubric === 'issue-story-v1' ? ' · Direct coverage check' : ''}{value.strength != null && ` · Evidence strength ${value.strength.toFixed(2)}/2`}</span><span>{value.review_reason} · {value.cached_from ? "Reused · no new charge" : value.eur == null ? "EUR estimate unavailable" : "€"+value.eur}</span></div>;
}

function DecisionResults({primary,second,advisory}: {primary?:Native|null; second?:Native|null; advisory?:Advisory|null}) {
  return <>
    {advisory && <div><strong>{advisory.status === 'needs_review' ? 'Needs administrator review' : 'Advisory result · '+scopeText(advisory.scope)}</strong><p>{advisory.reason}</p>{advisory.human_scope && <p>Previous administrator judgment · {scopeText(advisory.human_scope)}</p>}
      {advisory.existing_link !== undefined && <p className="text-muted-foreground">Saved issue–Story link at review time · {advisory.existing_link ? 'Linked' : 'Unlinked'}</p>}</div>}
    {primary && <NativeResult value={primary}/>}
    {second && <div><strong>Second opinion</strong><NativeResult value={second}/></div>}
  </>;
}

type Judgment = {scope: string; note: string; revision: string};
type ReviewRow = {second_opinion:string|null; id:string; issue:string; story:string; original:string|null; wiki:string|null; native:string|null; decision:string|null};
type ReviewDetail = {prerequisites?:Dependency[]; second_opinion:Native|null; advisory:Advisory|null; id:string; issue:Source; story:Source; arms:Run['arms']; native:Native|null; decision:Judgment|null};
const scopeLabels: Record<string,string> = {full:'All', partial:'Some', unrelated:'None', unclear:'Unclear'};
const scopeText = (value: string | null) => value ? scopeLabels[value] ?? value : 'No verdict';

function ReviewDecision({path, review, saved}: {path:string; review:ReviewDetail; saved:()=>void}) {
  const [scope,setScope] = useState(review.decision?.scope ?? '');
  const [note,setNote] = useState(review.decision?.note ?? '');
  const [busy,setBusy] = useState(false);
  const [problem,setProblem] = useState('');
  async function save() {
    if (!scope || busy) return;
    setBusy(true); setProblem('');
    try {
      await postJson(path, {scope, note, expected_revision:review.decision?.revision ?? null});
      saved();
    } catch(e) {setProblem(e instanceof Error ? e.message : 'Could not save this judgment.');}
    finally {setBusy(false);}
  }
  return <div className="grid gap-3">
    <div><strong>Technical issue</strong><Passage row={review.issue}/></div>
    <div><strong>User Story</strong><Passage row={review.story}/></div>
    <DependencySources items={review.prerequisites}/>
    <DecisionResults primary={review.native} second={review.second_opinion} advisory={review.advisory}/>
    {review.arms.map(a=><p key={a.arm}><strong>{a.arm==='wiki'?'Wiki excerpts':'Original passages'} · {scopeText(a.scope)}</strong><br/>{a.evidence}</p>)}
    <label className="grid gap-1">Your judgment<select className={optionsClass} value={scope} onChange={e=>setScope(e.target.value)}>
      <option value="" disabled>Select a scope</option>
      {['full','partial','unrelated','unclear'].map(v=><option key={v} value={v}>{scopeText(v)}</option>)}
    </select></label>
    <label className="grid gap-1">Evidence or criterion (optional)<Textarea value={note} onValueChange={setNote} maxLength={2000} rows={2}/></label>
    {problem && <Alert tone="warning" title={problem}/>}
    <div><Button size="sm" loading={busy} disabled={busy || !scope} onClick={()=>void save()}>Save judgment</Button></div>
  </div>;
}

function ReviewDialog({path, close, saved}: {path:string; close:()=>void; saved:()=>void}) {
  const item=useResource<ReviewDetail>(apiUrl(path));
  return <Dialog open onOpenChange={open=>{if(!open)close();}}>
    <DialogContent size="lg">
      <DialogHeader><DialogTitle>Review comparison</DialogTitle>
        <DialogDescription>Your judgment applies to these saved passages. Project links have their own approval process.</DialogDescription></DialogHeader>
      <DialogBody>{item.error ? <Alert tone="warning" title={item.error}/> : item.data ?
        <ReviewDecision path={path} review={item.data} saved={saved}/> : <p>Loading evidence…</p>}</DialogBody>
    </DialogContent>
  </Dialog>;
}

function VerdictReviews({path,latest}: {path:string; latest:string}) {
  const [state,setState]=useState('pending');
  const [page,setPage]=useState(1);
  const [opened,setOpened]=useState('');
  const list=useResource<{total:number;page:number;items:ReviewRow[]}>(apiUrl(path,{state,page}));
  const reload=list.reload;
  const previous=useRef(latest);
  useEffect(()=>{if(previous.current!==latest){previous.current=latest;reload();}},[latest,reload]);
  const columns:DataTableColumn<ReviewRow>[]=[
    {id:'issue',header:'Technical issue',accessor:r=>r.issue,sortable:false},
    {id:'story',header:'User Story',accessor:r=>r.story,sortable:false},
    {id:'original',header:'Original',accessor:r=>scopeText(r.original),sortable:false},
    {id:'wiki',header:'Wiki',accessor:r=>scopeText(r.wiki),sortable:false},
    {id:'native',header:'Decisions',accessor:r=>scopeText(r.native),sortable:false},
    {id:'second',header:'Second opinion',accessor:r=>r.second_opinion ? scopeText(r.second_opinion) : '—',sortable:false},
    ...(state==='reviewed'?[{id:'decision',header:'Your judgment',accessor:(r:ReviewRow)=>scopeText(r.decision),sortable:false}]:[]),
    {id:'review',header:'Action',sortable:false,cell:r=><Button size="sm" variant="secondary" onClick={()=>setOpened(r.id)}>Review</Button>},
  ];
  return <div className="grid gap-2 border-t border-border pt-3">
    <h3 className="font-semibold">Review model verdicts</h3>
    <p className="text-sm text-muted-foreground">Open a verdict to compare the original issue and Story. Saving your judgment records the review. Project links have their own approval process.</p>
    <Tabs value={state} onValueChange={v=>{setState(String(v));setPage(1);}}>
      <TabsList aria-label="Verdict review status"><TabsTab value="pending">Pending</TabsTab><TabsTab value="advisory">Advisory results</TabsTab><TabsTab value="reviewed">Reviewed</TabsTab></TabsList>
    </Tabs>
    {list.error && <Alert tone="warning" title={list.error}/>}
    {list.data && <DataTable caption="Model verdicts" rows={list.data.items} columns={columns} getRowId={r=>r.id}
      selectionMode="none" showColumnMenu={false} paginationMode="server" paginated={list.data.total>10}
      page={list.data.page} pageSize={10} pageSizeOptions={[10]} rowCount={list.data.total} loading={list.loading}
      emptyState={state==='pending'?'No comparisons awaiting review.':state==='advisory'?'No advisory results.':'No reviewed comparisons.'} onQueryChange={q=>setPage(q.page)}/>}
    {opened && <ReviewDialog key={opened} path={path+'/'+opened} close={()=>setOpened('')} saved={()=>{setOpened('');reload();}}/>}
  </div>;
}

export function DesignerEvidence({draft}: {draft:FlowDocument}) {
  const list=useProjects();
  const [project,setProject]=useState('');
  const owner=projectScope();
  const target=owner || project || list.data?.default_project || list.data?.items[0]?.project;
  return <div className="mt-4 grid gap-2">{!owner && list.data && <label className="flex items-center gap-2 text-sm">Preview project
    <select className={optionsClass} value={target??''} onChange={e=>setProject(e.target.value)}>
      {list.data.items.map(p=><option key={p.project} value={p.project}>{p.display_name}</option>)}</select></label>}
    {target && <EvidencePanel key={target} project={target} draft={draft}/>}</div>;
}
