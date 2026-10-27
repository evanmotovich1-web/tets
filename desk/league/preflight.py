"""Read-only host capability probe. Never stops jobs or changes drivers/security."""
import importlib.metadata,json,platform,shutil,subprocess,sys

def inspect():
 versions={}
 for name in ['backtesting','numpy','pandas','rfc8785']:
  try:versions[name]=importlib.metadata.version(name)
  except importlib.metadata.PackageNotFoundError:versions[name]=None
 node=shutil.which('node');gpu=None
 if shutil.which('nvidia-smi'):
  try:
   p=subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.free','--format=csv,noheader'],capture_output=True,text=True,timeout=5)
   gpu=p.stdout.strip()if p.returncode==0 else 'NVML unavailable; GPU readiness not inferred'
  except(subprocess.TimeoutExpired,OSError):gpu='unverified'
 return dict(host=platform.node(),os=platform.system(),python=platform.python_version(),node=node,packages=versions,disk_free_bytes=shutil.disk_usage('.').free,gpu_read_only=gpu,league_candidate_ready=bool(node and all(versions.values())and platform.system()!='Windows'),native_windows_campaign_supported=False,main_model_checkpoint=None,main_model_training_ready=False,broker_enabled=False)
if __name__=='__main__':print(json.dumps(inspect(),indent=2))
