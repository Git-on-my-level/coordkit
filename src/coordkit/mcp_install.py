"""Explicit local MCP service installation; never exposes a public endpoint."""
import argparse
import os
from pathlib import Path
import secrets
from .manifest import ManifestError
from .paths import python_env
from .services import manager
from .state import read_json, write_json
from .selfupdate import register


def spec(manifest, secret, port):
    return dict(Label=manifest.launchd.label_prefix+'.mcp',
                ProgramArguments=[manifest.launchd.python,'-m','coordkit.cli','--manifest',str(manifest.file),
                                  'mcp-serve','--secret-file',str(secret),'--port',str(port)],
                WorkingDirectory=str(manifest.file.parent),
                EnvironmentVariables={'PATH':':'.join(manifest.launchd.path),**python_env()},
                RunAtLoad=True, KeepAlive=True,
                StandardOutPath=str(manifest.paths.root/'mcp/stdout.log'),
                StandardErrorPath=str(manifest.paths.root/'mcp/stderr.log'))


def main(argv, manifest):
    from .mcp_server import SECRET_FILE, load_secret
    from .ops import parser_for
    parser=parser_for('mcp')
    opts=parser.parse_args(argv)
    manifest.writable()
    if not 1 <= opts.port <= 65535:
        raise ManifestError('invalid_mcp_port')
    receipt=manifest.paths.root/'mcp-service.json'
    service=manager(manifest)
    label=manifest.launchd.label_prefix+'.mcp'
    if opts.action=='uninstall':
        if receipt.exists():
            service.uninstall(label)
            receipt.unlink()
        print('MCP_UNINSTALLED')
        return 0
    secret=Path(opts.secret_file or SECRET_FILE).expanduser().absolute()
    secret.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(secret,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd,'w') as stream:
            stream.write(secrets.token_urlsafe(48)+'\n')
    load_secret(secret)
    job=spec(manifest,secret,opts.port)
    Path(job['StandardOutPath']).parent.mkdir(parents=True,exist_ok=True)
    previous=read_json(receipt)
    if previous and previous==job and service.path(label).exists():
        print('MCP_INSTALLED 127.0.0.1:'+str(opts.port)+'/mcp')
        return 0
    write_json(receipt,job)  # recovery ownership precedes manager mutation
    service.install(job,previous=bool(previous))
    register(manifest)
    print('MCP_INSTALLED 127.0.0.1:'+str(opts.port)+'/mcp')
    print('Use a bearer token from '+str(secret)+'. Configure TLS transport separately; writes remain disabled.')
    return 0
