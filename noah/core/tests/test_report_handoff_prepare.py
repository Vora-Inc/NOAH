"""Preparation writes a draft only after both archived twins and graph agree."""
import copy
import io
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest
from noah.core import report_handoff_prepare as prepare, report_source_handoff as h
from noah.core.calculation_record import artifact, encoded, verify
from noah.core.report_publication import ReportPublicationRefused
from noah.core.tests.test_weekly_runtime import prepared
from noah.core.tests.test_report_source_handoff import handoff


def fake_releases(x,monkeypatch,change=None):
    monkeypatch.setattr(prepare,'release',lambda spec:(Path('/synthetic')/spec['release_id'],{'commit':spec['commit'],'files':{'source/'+k:{'sha256':v} for k,v in spec['candidate']['files'].items()}}))
    score=encoded(artifact(x.p.root,verify(x.p.root,x.p.raw),'agreed_score')).hex()
    def replay(root,fixture,manifest):
        side='old' if root.name.startswith('a') else 'new'
        value={'candidate':x.value['from_release' if side=='old' else 'to_release']['candidate'],
               'python':score,'typescript':score}
        if change==side:value['typescript']=b'changed'.hex()
        return value
    monkeypatch.setattr(prepare,'replay',replay)


def test_preparation_binds_actual_four_results_and_original_request(handoff,monkeypatch):
    x=handoff;fake_releases(x,monkeypatch)
    value=prepare.prepare(x.p.root,x.path,x.successor,x.value['from_release'],x.value['to_release'],
                          x.value['approval'],x.value['checkpoint_sha256'])
    assert value==x.value and x.path.read_bytes()==x.raw


@pytest.mark.parametrize('changed',['old','new'])
def test_any_independent_engine_disagreement_refuses(handoff,monkeypatch,changed):
    x=handoff;fake_releases(x,monkeypatch,changed)
    with pytest.raises(ReportPublicationRefused,match='engine changes'):
        prepare.prepare(x.p.root,x.path,x.successor,x.value['from_release'],x.value['to_release'],
                        x.value['approval'],x.value['checkpoint_sha256'])


def test_cli_writes_only_reviewable_draft_and_refuses_overwrite(handoff,monkeypatch,tmp_path):
    x=handoff;fake_releases(x,monkeypatch)
    files=[]
    for name,value in [('successor',x.successor),('old',x.value['from_release']),('new',x.value['to_release'])]:
        p=tmp_path/(name+'.json');p.write_bytes(encoded(value));files.append(p)
    output=tmp_path/'draft.json'
    args=['--scores',str(x.p.root),'--request',str(x.path),'--successor-reference',str(files[0]),
          '--old-release',str(files[1]),'--new-release',str(files[2]),'--output',str(output),
          '--approval',x.value['approval'],'--checkpoint-sha256',x.value['checkpoint_sha256']]
    assert prepare.main(args)==0 and h.document(output.read_bytes())==x.value
    with pytest.raises(ReportPublicationRefused,match='already exists'):prepare.main(args)
    assert x.path.read_bytes()==x.raw


def test_archive_hash_and_unprotected_release_refuse(tmp_path):
    spec={'root':str(tmp_path/'bad'),'archive':str(tmp_path/'archive'),
          'archive_sha256':'a'*64,'manifest_sha256':'b'*64}
    Path(spec['root']).mkdir();Path(spec['archive']).write_bytes(b'not archive')
    with pytest.raises(ReportPublicationRefused,match='protected'):
        prepare.release(spec)


def test_file_hash_rejects_links_and_limits(tmp_path):
    path=tmp_path/'file';path.write_bytes(b'abc')
    assert prepare.file_hash(path)==h.digest(b'abc')
    with pytest.raises(ReportPublicationRefused):prepare.file_hash(path,limit=2)
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):prepare.file_hash(link)


def test_real_isolated_python_and_typescript_replay(handoff,tmp_path):
    import shutil,sys
    x=handoff
    node=shutil.which('node')
    if node is None:pytest.skip('Node required for independent twin execution')
    root=tmp_path/'real-twin-release'
    source=Path(prepare.__file__).resolve().parents[2]
    (root/'payload').mkdir(parents=True)
    (root/'payload/source').symlink_to(source,target_is_directory=True)
    (root/'venv/bin').mkdir(parents=True)
    (root/'venv/bin/python').symlink_to(sys.executable)
    (root/'payload/node/bin').mkdir(parents=True)
    (root/'payload/node/bin/node').symlink_to(node)
    m=verify(x.p.root,x.p.raw)
    manifest={'files':{'source/noah/core/calculation_record.py':{}}}
    result=prepare.replay(root,artifact(x.p.root,m,'fixture'),manifest)
    expected=encoded(artifact(x.p.root,m,'agreed_score')).hex()
    assert result['python']==result['typescript']==expected
    from noah.core.calculation_record import source_identity
    assert result['candidate']==source_identity()


def test_authenticated_archive_and_installed_payload_agree(tmp_path,monkeypatch):
    import hashlib,tarfile,io,os,stat
    from types import SimpleNamespace
    body={'python/bin/python3.11':b'python','python/lib/libpython3.11.so.1.0':b'library',
          'source/noah/example.py':b'example'}
    manifest={'commit':'a'*40,'files':{name:{'sha256':h.digest(data),'size':len(data),'mode':0o500}
                                      for name,data in body.items()}}
    raw=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    root=tmp_path/('a'*12+'-'+h.digest(raw)[:16]);payload=root/'payload';payload.mkdir(parents=True)
    body['manifest.json']=raw
    for name,data in body.items():
        target=payload/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);target.chmod(0o644 if name=='manifest.json' else 0o500)
    for target,name in [('venv/bin/python','python/bin/python3.11'),('venv/lib/libpython3.11.so.1.0','python/lib/libpython3.11.so.1.0')]:
        path=root/target;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body[name]);path.chmod(0o500)
    archive=tmp_path/'release.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name,data in body.items():
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644 if name=='manifest.json' else 0o500;tar.addfile(info,io.BytesIO(data))
    spec={'root':str(root),'archive':str(archive),'archive_sha256':prepare.file_hash(archive),'manifest_sha256':h.digest(raw)}
    # Synthetic local payload models root ownership; real protected_tree is
    # tested separately and never relaxed by the preparation CLI.
    monkeypatch.setattr(prepare,'protected_tree',lambda _:None)
    original=Path.lstat
    def owned(path):
        info=original(path)
        return SimpleNamespace(**{name:(0 if name=='st_uid' else getattr(info,name))
                                  for name in dir(info) if name.startswith('st_')})
    monkeypatch.setattr(Path,'lstat',owned)
    assert prepare.release(spec)==(root,manifest)
    (payload/'source/noah/example.py').chmod(0o700)
    with pytest.raises(ReportPublicationRefused,match='installed artifact differs'):prepare.release(spec)
    (payload/'source/noah/example.py').chmod(0o500)
    (root/'venv/lib/libpython3.11.so.1.0').chmod(0o700)
    (root/'venv/lib/libpython3.11.so.1.0').write_bytes(b'different')
    (root/'venv/lib/libpython3.11.so.1.0').chmod(0o500)
    with pytest.raises(ReportPublicationRefused,match='interpreter differs'):prepare.release(spec)


def test_null_declared_commit_does_not_fabricate_source_label(handoff):
    value=copy.deepcopy(handoff.value)
    for release in ('from_release','to_release'):
        value[release]['candidate']['declared_commit']=None
    assert h.validate(value)==value
    value['to_release']['candidate']['declared_commit']='c'*40
    with pytest.raises(ReportPublicationRefused,match='release identity differs'):h.validate(value)


@pytest.mark.parametrize('names',[
 [],['source/noah/core/calculation_record.py','source/other/noah/core/calculation_record.py'],
 ['noah/core/calculation_record.py'],['outside/noah/core/calculation_record.py'],
 ['source/../noah/core/calculation_record.py'],['/source/noah/core/calculation_record.py'],
 ['source//noah/core/calculation_record.py'],['source/./noah/core/calculation_record.py'],
 ['source/hidden\\noah/core/calculation_record.py'],
])
def test_source_root_missing_ambiguous_or_malformed_refuses(names):
    with pytest.raises(ReportPublicationRefused):prepare.source_prefix({'files':dict.fromkeys(names,{})})


@pytest.mark.parametrize('prefix',['source/','source/nested/package/'])
def test_source_root_is_declared_by_authenticated_manifest(prefix):
    members={'noah/core/calculation_record.py':'a'*64,'noah/reports/example.py':'b'*64,
             'noah/starter/example.mjs':'c'*64,'neurons/validator.py':'d'*64,
             'verification/noah-ts/src/points.ts':'e'*64}
    files={prefix+key:{'sha256':value} for key,value in members.items()}
    files[prefix+'noah/core/tests/test_example.py']={'sha256':'f'*64}
    assert prepare.source_prefix({'files':files})==prefix
    assert prepare.source_files({'files':files})==members


# Real tar/manifest fixtures for legitimate zero-byte payload members.

@pytest.fixture
def empty_release_archive(tmp_path,monkeypatch):
 body={'python/bin/python3.11':b'python','python/lib/libpython3.11.so.1.0':b'library','source/noah/__init__.py':b''}
 def make(change=None, *, archive_manifest_mode=0o644, installed_manifest_mode=0o644):
  manifest={'commit':'a'*40,'files':{n:{'size':len(b),'mode':0o500,'sha256':h.digest(b)}for n,b in body.items()}}
  if change=='wrong-sha':manifest['files']['source/noah/__init__.py']['sha256']='1'*64
  if change=='wrong-size':manifest['files']['source/noah/__init__.py']['size']=1
  if change=='boolean-size':manifest['files']['source/noah/__init__.py']['size']=False
  if change=='undeclared':del manifest['files']['source/noah/__init__.py']
  raw=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode();root=tmp_path/('a'*12+'-'+h.digest(raw)[:16]);payload=root/'payload';payload.mkdir(parents=True)
  members={**body,'manifest.json':raw}
  for name,data in members.items():
   target=payload/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);target.chmod(installed_manifest_mode if name=='manifest.json' else 0o500)
  for rel,name in(('venv/bin/python','python/bin/python3.11'),('venv/lib/libpython3.11.so.1.0','python/lib/libpython3.11.so.1.0')):
   target=root/rel;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body[name]);target.chmod(0o500)
  tarpath=tmp_path/'release.tar.gz'
  with tarfile.open(tarpath,'w:gz')as bundle:
   for name,data in members.items():
    info=tarfile.TarInfo(name);info.size=len(data);info.mode=archive_manifest_mode if name=='manifest.json' else 0o500;bundle.addfile(info,io.BytesIO(data))
   if change=='duplicate':
    info=tarfile.TarInfo('source/noah/__init__.py');info.size=0;info.mode=0o500;bundle.addfile(info,io.BytesIO(b''))
  return {'root':str(root),'archive':str(tarpath),'archive_sha256':h.digest(tarpath.read_bytes()),'manifest_sha256':h.digest(raw)},root
 # Simulate only root ownership/protected ancestors for a local real-file fixture.
 monkeypatch.setattr(prepare,'protected_tree',lambda _:None)
 old=Path.lstat
 def owned(path):
  s=old(path);return SimpleNamespace(**{n:(0 if n=='st_uid'else getattr(s,n))for n in dir(s)if n.startswith('st_')})
 monkeypatch.setattr(Path,'lstat',owned)
 return make

def test_legitimate_empty_member_declared_in_archive_and_manifest(empty_release_archive):
 spec,root=empty_release_archive();assert prepare.release(spec)[0]==root

@pytest.mark.parametrize('kind',['wrong-sha','wrong-size','boolean-size','undeclared','duplicate'])
def test_empty_must_be_declared_identically_in_both_authenticated_inputs(empty_release_archive,kind):
 spec,_=empty_release_archive(kind)
 with pytest.raises(ReportPublicationRefused):prepare.release(spec)

@pytest.mark.parametrize('kind',['hardlink','symlink','content','mode','empty-python','empty-library'])
def test_installed_empty_and_nonempty_custody_guards_remain(empty_release_archive,kind):
 spec,root=empty_release_archive();target=root/'payload/source/noah/__init__.py'
 if kind=='hardlink':os.link(target,root/'alias')
 elif kind=='symlink':target.unlink();target.symlink_to(root/'absent')
 elif kind=='content':target.chmod(0o700);target.write_bytes(b'changed');target.chmod(0o500)
 elif kind=='mode':target.chmod(0o700)
 else:
  target=root/('venv/bin/python'if kind=='empty-python'else'venv/lib/libpython3.11.so.1.0');target.chmod(0o700);target.write_bytes(b'');target.chmod(0o500)
 with pytest.raises((ReportPublicationRefused,OSError)):prepare.release(spec)

def test_default_empty_archive_executable_or_input_hash_stays_refused(tmp_path):
 empty=tmp_path/'empty';empty.touch()
 with pytest.raises(ReportPublicationRefused):prepare.file_hash(empty)
 assert prepare.file_hash(empty,allow_empty=True)==h.digest(b'')
 fifo=tmp_path/'fifo';os.mkfifo(fifo)
 with pytest.raises(ReportPublicationRefused):prepare.file_hash(fifo)


@pytest.mark.parametrize('archive_mode',[0o600,0o644])
def test_maintained_manifest_mode_normalization(empty_release_archive,archive_mode):
 spec,root=empty_release_archive(archive_manifest_mode=archive_mode)
 assert prepare.release(spec)[0]==root

@pytest.mark.parametrize('archive_mode,installed_mode',[(0o640,0o644),(0o666,0o644),(0o755,0o644),(0o4600,0o644),(0o600,0o600),(0o644,0o600),(0o600,0o664),(0o644,0o755)])
def test_manifest_mode_exception_remains_narrow(empty_release_archive,archive_mode,installed_mode):
 spec,_=empty_release_archive(archive_manifest_mode=archive_mode,installed_manifest_mode=installed_mode)
 with pytest.raises(ReportPublicationRefused):prepare.release(spec)

def test_manifest_normalization_does_not_apply_to_payload(empty_release_archive):
 spec,root=empty_release_archive(archive_manifest_mode=0o600)
 (root/'payload/source/noah/__init__.py').chmod(0o644)
 with pytest.raises(ReportPublicationRefused):prepare.release(spec)
