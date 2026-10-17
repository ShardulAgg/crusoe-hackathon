import json
import sys

import pytest
from fastapi.testclient import TestClient

from resource_manager.main import app
from resource_manager.models import JobSubmission, NodeRegistration
from resource_manager.queue import ComputeQueue
from resource_manager.resource_catalog import ResourceCatalog, ResourceSpec
from resource_manager.runtime import DockerRuntime


@pytest.fixture
def resource():
    return ResourceSpec(
        id="cpu",
        node_id="node-a",
        enabled=True,
        image="trusted-runtime:local",
        cpus=2,
        memory_mb=8192,
        max_concurrent_jobs=2,
    )


@pytest.fixture
def catalog(resource):
    return ResourceCatalog([resource])


@pytest.fixture
def queue(tmp_path):
    result = ComputeQueue(tmp_path / "compute.sqlite3")
    result.register_node(NodeRegistration(id="node-a", cpus=2, memory_mb=8192))
    return result


@pytest.fixture
def body():
    return JobSubmission(
        resource_id="cpu",
        node_id="node-a",
        cpus=1,
        memory_mb=512,
        scratch_mb=32,
        timeout_seconds=10,
    )


@pytest.fixture
def client(tmp_path, monkeypatch, resource):
    inventory = tmp_path / "Resources"
    inventory.mkdir()
    (inventory / "cpu.json").write_text(resource.model_dump_json())
    monkeypatch.setenv("RESOURCE_MANAGER_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("RESOURCE_MANAGER_RESOURCES_DIR", str(inventory))
    monkeypatch.delenv("RESOURCE_MANAGER_API_KEY", raising=False)
    with TestClient(app) as result:
        result.post("/nodes/register", json={"id": "node-a", "cpus": 2, "memory_mb": 8192})
        yield result


@pytest.fixture
def fake_docker(tmp_path):
    path = tmp_path / "fake_docker.py"
    path.write_text("""import json,sys,time,os
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[1:]
if args[:1]==['--context']: args=args[2:]
with (root/'docker.log').open('a') as stream: stream.write(json.dumps(args)+'\\n')
mode=(root/'mode').read_text() if (root/'mode').exists() else 'echo'
if args[0]=='info':
    if mode=='daemon-fail': sys.exit(1)
    print(json.dumps('27.0'))
elif args[:2]==['image','inspect']:
    if mode=='missing-image': sys.exit(1)
    volumes={'/data':{}} if mode=='volumes' else None
    print(json.dumps([{'Id':'sha256:'+'a'*64,'Config':{'Volumes':volumes}}]))
elif args[0]=='create':
    (root/'created').write_text('yes')
    if mode=='slow-create': time.sleep(30)
    print('b'*64)
elif args[0]=='start':
    if mode=='silent': time.sleep(30)
    elif mode=='stdout-limit': sys.stdout.write('x'*(2*1024*1024+1)+'\\n')
    elif mode=='stderr-limit': sys.stderr.write('x'*(16*1024*1024+1))
    elif mode=='bad-utf8': sys.stdout.buffer.write(b'\\xff\\n')
    elif mode=='trailing': sys.stdout.write('opaque trailing output')
    elif mode=='control': sys.stdout.write('\\x00'*(2*1024*1024)+'\\n')
    elif mode=='short-lines': sys.stdout.write('x\\n'*100000)
    else:
        for line in sys.stdin: sys.stdout.write(line); sys.stdout.flush()
    (root/'exit').write_text('7' if mode=='nonzero' else '0')
elif args[0]=='inspect':
    print(json.dumps({'Running':False,'ExitCode':int((root/'exit').read_text())}))
elif args[0]=='rm':
    if mode=='cleanup-fail':
        sys.stderr.write('sensitive daemon address');sys.exit(1)
    (root/'created').unlink(missing_ok=True)
else: sys.exit(9)
""")

    def factory(resource, mode="echo"):
        (tmp_path / "mode").write_text(mode)
        return DockerRuntime(resource, docker_command=(sys.executable, str(path)))

    factory.calls = lambda: [
        json.loads(line) for line in (tmp_path / "docker.log").read_text().splitlines()
    ]
    factory.root = tmp_path
    return factory
