import asyncio,sys,os,tempfile,pathlib
sys.path.insert(0,'tests')
from tests.test_real_target import *
s=asyncio.run(execute_run(tiny(),0,TargetConfig(base_url=URL,dsn=DSN),out_dir=pathlib.Path(tempfile.mkdtemp()),log=lambda m:None))
print(s["integrity"]); print(s["outcomes"]["entered_client"], s["warnings"])
