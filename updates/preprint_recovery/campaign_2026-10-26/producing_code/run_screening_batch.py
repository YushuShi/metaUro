"""Run the frozen, verified preprint jobs with bounded concurrency and reuse."""
import argparse,concurrent.futures,json,sys,threading
from pathlib import Path
from datetime import datetime,timezone
BASE=Path(__file__).resolve().parents[1];ROOT=BASE.parent
sys.path.insert(0,str(BASE/'implementation'));import preprint_screening as pipeline
sys.path.insert(0,str(ROOT/'metaGut'));from scripts import run_claude_gemini_parallel_review as gateway

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--manifest',default='screening_jobs_repair_manifest.json');parser.add_argument('--run-name',default='guarded_repair');parser.add_argument('--workers',type=int,default=2);args=parser.parse_args()
 if args.workers not in {1,2,3}:parser.error('workers must be bounded to1-3')
 manifest=json.loads((BASE/args.manifest).read_text());jobs=[]
 for relative in manifest['jobs']:
  job=json.loads((BASE/relative).read_text());pipeline.validate_source(job);jobs.append((relative,job))
 client=gateway._configure_client();results=[];lock=threading.Lock();out=BASE/'screening_run'
 def execute(item):
  relative,job=item
  try:
   result=pipeline.run_job(client,job,out)
   status={'job_file':relative,'repository':job['repository'],'pmid':job['source']['published_pmid'],'job_id':result['job_id'],'status':result['status'],'context_ids':[x['context_id'] for x in job['contexts']]}
   if result.get('adjudication'):status['decisions']=[{'context_id':r['context_id'],'decision':r['decision'],'confidence':r['confidence']} for r in result['adjudication']['results']]
   if result.get('problems'):status['problems']=result['problems']
  except Exception as exc:
   status={'job_file':relative,'repository':job['repository'],'pmid':job['source']['published_pmid'],'status':'operational_hold','error_type':type(exc).__name__,'safe_validation_error':str(exc) if isinstance(exc,ValueError) else None}
  with lock:
   results.append(status);pipeline.save(out/(args.run_name+'_progress.json'),{'updated_at':pipeline.now(),'completed_jobs':len(results),'total_jobs':len(jobs),'results':results})
   print(json.dumps(status,ensure_ascii=False),flush=True)
  return status
 with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:list(pool.map(execute,jobs))
 cost=pipeline.cost_report(out);pipeline.save(out/(args.run_name+'_complete.json'),{'completed_at':pipeline.now(),'job_count':len(jobs),'results':results,'cost':{k:v for k,v in cost.items() if k!='calls'}})
 print(json.dumps({k:v for k,v in cost.items() if k!='calls'}),flush=True)
if __name__=='__main__':main()
