"""Explicit ephemeral CLI configuration for the qualified three-tool broker."""
import copy,json,os
from pathlib import Path
import tomllib
PROJECT=Path('@PROJECT@')
V2=PROJECT/'sandbox/controller/v2'
CLI=Path('@RUNTIME@/cli/codex')
PYTHON=Path('@RUNTIME@/venv/bin/python')
WORK=PROJECT/'sandbox/candidate/work'

def toml(value):
    if isinstance(value,bool):return str(value).lower()
    if isinstance(value,str):return json.dumps(value)
    if isinstance(value,dict):return '{'+','.join(json.dumps(k)+'='+toml(v) for k,v in value.items())+'}'
    if isinstance(value,list):return '['+','.join(toml(v) for v in value)+']'
    return str(value)

def client(destination,broker,gateway,prompt,images=()):
    destination=Path(destination)
    (destination/'home').mkdir();(destination/'codex-home').mkdir()
    config=tomllib.loads((PROJECT/'sandbox/profile.toml').read_text())
    features={key:False for key in 'apps plugins remote_plugin hooks browser_use browser_use_external computer_use memories multi_agent multi_agent_v2 image_generation skill_search skill_mcp_dependency_install tool_suggest shell_snapshot api_key_model_discovery enable_request_compression standalone_web_search goals workspace_dependencies request_permissions_tool unbounded_connection_retries daemon_auto_start system_proxy_fallback respect_system_proxy'.split()}
    features.update(code_mode_host=True,skip_host_skill_discovery=True)
    config['features'].update(features)
    config.update(model='gpt-6.1-sol',model_reasoning_effort='xhigh',model_context_window=500000,model_auto_compact_token_limit=490000,model_provider='deng_qualified_gate',cli_auth_credentials_store='ephemeral',analytics={'enabled':False},feedback={'enabled':False},check_for_update_on_startup=False,history={'persistence':'none'},otel={'exporter':'none'})
    names=['run_python','view_image','check_geometry']
    config['mcp_servers']={'deng_cad':{'command':str(PYTHON),'args':[str(V2/'mcp_relay.py')],'env':broker.environment(),'enabled_tools':names,'default_tools_approval_mode':'prompt','tools':{name:{'approval_mode':'approve'} for name in names},'required':True,'startup_timeout_sec':15,'tool_timeout_sec':150}}
    config['model_providers']={'deng_qualified_gate':{'name':'Qualified local CAD gate','base_url':gateway.base_url,'wire_api':'responses','requires_openai_auth':False,'supports_websockets':False,'request_max_retries':0,'stream_max_retries':0,'stream_idle_timeout_ms':240000,'capabilities':{'remote_compaction':'unsupported'}}}
    command=[str(CLI),'--no-daemon','-a','never','exec','--ignore-user-config','--ignore-rules','--strict-config','--skip-git-repo-check','--ephemeral','--json','-C',str(WORK)]
    for key,value in config.items():command.extend(['-c',key+'='+toml(value)])
    command.extend(['--output-last-message',str(destination/'last-message.txt')])
    for image in images:command.extend(['--image',str(image)])
    command.extend(['--',prompt])
    env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':str(destination/'home'),'CODEX_HOME':str(destination/'codex-home'),'LANG':'en_US.UTF-8','NO_PROXY':'127.0.0.1,localhost','TMPDIR':str(PROJECT/'sandbox/candidate/tmp')}
    public=copy.deepcopy(config);public['mcp_servers']['deng_cad']['env']={key:'[private broker capability]' for key in broker.environment()}
    (destination/'configuration.json').write_text(json.dumps(public,indent=2)+'\n')
    (destination/'prompt.txt').write_text(prompt+'\n')
    return command,env
