"""Mutation tests for exact-app scope, scanner errors, and changed risk."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('vex', Path(__file__).with_name('check_application_vex.py'))
vex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vex)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.v = {'id': 'PYSEC-2026-1325', 'aliases': ['CVE-2024-23342', 'GHSA-wj6h-64fc-37mp'],
                  'fix_versions': [], 'description': 'Reviewed affected private-key operation.'}
        self.raw = {'dependencies': [{'name': 'ecdsa', 'version': '0.19.2', 'vulns': [self.v]}], 'fixes': []}
        self.digest = vex.advisory_digest(self.v)

    def assess(self, raw=None, code=1):
        return vex.assess(raw or self.raw, code, {'ecdsa': '0.19.2'}, True, self.digest)

    def test_raw_affected_finding_remains_visible(self):
        before = copy.deepcopy(self.raw)
        r = self.assess()
        self.assertEqual(r['application_status'], 'not_affected')
        self.assertTrue(r['component_still_affected'])
        self.assertEqual(r['raw_exit'], 1)
        self.assertEqual(before, self.raw)

    def test_changed_advisory_requires_review(self):
        for field, value in [('description', 'Expanded to verification'), ('fix_versions', ['0.20']),
                             ('id', 'NEW-1'), ('aliases', ['CVE-OTHER'])]:
            with self.subTest(field=field):
                raw = copy.deepcopy(self.raw); raw['dependencies'][0]['vulns'][0][field] = value
                with self.assertRaises(vex.Refused): self.assess(raw)

    def test_scanner_failures_and_package_drift(self):
        for code in (0, 2, -9, None, True):
            with self.subTest(code=code), self.assertRaises(vex.Refused): self.assess(code=code)
        for mutate in (lambda x: x['dependencies'][0].update(version='0.20'),
                       lambda x: x['dependencies'].clear(),
                       lambda x: x['dependencies'].append(copy.deepcopy(x['dependencies'][0])),
                       lambda x: x['dependencies'][0].update(skip_reason='network')):
            raw = copy.deepcopy(self.raw); mutate(raw)
            with self.assertRaises(vex.Refused): self.assess(raw)
        with self.assertRaises(vex.Refused):
            vex.assess(self.raw, 1, {'ecdsa': '0.19.2'}, False, self.digest)

    def tree(self, root):
        names = {vex.POLICY: '100644'}; files = {}
        (root / vex.POLICY).parent.mkdir(parents=True)
        (root / vex.POLICY).write_text('{}')
        for name in (*vex.LOCKS, 'arbitrary/new_caller.py'):
            p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('reviewed')
            names[name] = '100644'; files[name] = {'mode': '100644', 'sha256': vex.sha(p)}
        return names, {'files': files, 'locks': {n: vex.sha(root/n) for n in vex.LOCKS}}

    def test_complete_inventory_rejects_add_missing_content_mode_and_symlink(self):
        for mutation in ('added', 'missing', 'bytes', 'mode', 'symlink', 'self_excluded_extra'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve(); names, p = self.tree(root); target = root/'arbitrary/new_caller.py'
                vex.verify_tree(root, p, names)
                if mutation == 'added': names['somewhere_else/caller.py'] = '100644'
                if mutation == 'missing': del names['arbitrary/new_caller.py']
                if mutation == 'bytes': target.write_text('import ecdsa; new_secret_operation()')
                if mutation == 'mode': target.chmod(0o755)
                if mutation == 'symlink': target.unlink(); target.symlink_to(root/'constraints.txt')
                if mutation == 'self_excluded_extra': del p['files']['arbitrary/new_caller.py']
                with self.assertRaises(vex.Refused): vex.verify_tree(root, p, names)

    def test_duplicate_json_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x'; p.write_text('{"status":1,"status":2}')
            with self.assertRaises(vex.Refused): vex.load(p)

    def test_platform_and_wheel_drift_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); wheel=root/'wheels'; wheel.mkdir()
            p={'wheels': {n: hashlib.sha256(b'wheel').hexdigest() for n in (
                'bittensor_wallet-4.1.1-cp311-cp311-manylinux_2_28_x86_64.whl',
                'ecdsa-0.19.2-py2.py3-none-any.whl', 'substrate_interface-1.7.11-py3-none-any.whl')}}
            for n in p['wheels']: (wheel/n).write_bytes(b'wheel')
            for n in vex.LOCKS[1:]: (root/n).write_text('--hash=sha256:'+hashlib.sha256(b'wheel').hexdigest())
            with patch.object(vex.platform,'system',return_value='Linux'), patch.object(vex.platform,'machine',return_value='x86_64'), patch.object(vex.sys,'version_info',(3,11)):
                vex.verify_wheels(root,p,wheel)
                (wheel/next(iter(p['wheels']))).write_bytes(b'changed')
                with self.assertRaises(vex.Refused): vex.verify_wheels(root,p,wheel)
            with patch.object(vex.platform,'system',return_value='Darwin'):
                with self.assertRaises(vex.Refused): vex.verify_wheels(root,p,wheel)

    def test_review_and_lock_binding_are_required(self):
        p = vex.load(Path(__file__).with_name('ecdsa-application-vex.json'))
        p['control_review'] = dict(p['review'])  # Synthetic approval for admission tests only.
        vex.validate_policy(p)
        for change in ('pending', 'missing', 'platform', 'package', 'extra_field'):
            q = copy.deepcopy(p)
            if change == 'pending': q['control_review']['status'] = 'pending'
            if change == 'missing': q.pop('review')
            if change == 'platform': q['subject']['platform'] = 'any'
            if change == 'package': q['packages']['reports']['ecdsa'] = '0.20'
            if change == 'extra_field': q['ignore_everything'] = True
            with self.subTest(change=change), self.assertRaises(vex.Refused): vex.validate_policy(q)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); names, policy=self.tree(root)
            policy['locks']['constraints.txt']='0'*64
            with self.assertRaises(vex.Refused): vex.verify_tree(root,policy,names)

    def test_git_inventory_catches_new_caller_outside_known_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); _, policy=self.tree(root)
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            subprocess.run(['git','add','.'],cwd=root,check=True)
            vex.verify_tree(root,policy)
            (root/'brand_new_entrypoint.py').write_text('import ecdsa')
            subprocess.run(['git','add','brand_new_entrypoint.py'],cwd=root,check=True)
            with self.assertRaises(vex.Refused): vex.verify_tree(root,policy)

    def test_both_raw_scans_retained_even_on_scanner_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); output=root/'evidence'; output.mkdir(); calls=[]
            def failed_scan(argv, cwd, out, name):
                calls.append(name)
                (out/(name+'.stdout')).write_text('{}')
                (out/(name+'.stderr')).write_text('scanner unavailable')
                return {'exit_code': 2, 'execution_error': None}
            with patch.object(vex,'capture',side_effect=failed_scan), patch.object(vex.importlib.metadata,'version',return_value='2.10.1'):
                self.assertEqual(vex.run_python(root,output),1)
            self.assertEqual(calls,['public','reports'])
            for n in calls: self.assertEqual((output/(n+'.stderr')).read_text(),'scanner unavailable')
            self.assertEqual(vex.load(output/'application-policy.json')['status'],'refused')

    def test_node_complete_clean_report_and_malformed_refusals(self):
        clean = {'auditReportVersion': 2, 'vulnerabilities': {}, 'metadata': {
            'vulnerabilities': dict(info=0, low=0, moderate=0, high=0, critical=0, total=0),
            'dependencies': dict(prod=1, dev=23, optional=20, peer=0, peerOptional=0, total=23)}}
        cases = [('valid', clean, 0, 0)]
        mutations = {
            'empty counters': lambda x: x['metadata'].update(vulnerabilities={}),
            'missing counter': lambda x: x['metadata']['vulnerabilities'].pop('critical'),
            'extra counter': lambda x: x['metadata']['vulnerabilities'].update(other=0),
            'boolean counter': lambda x: x['metadata']['vulnerabilities'].update(total=False),
            'negative counter': lambda x: x['metadata']['vulnerabilities'].update(low=-1),
            'finding counter': lambda x: x['metadata']['vulnerabilities'].update(high=1),
            'contradictory finding': lambda x: x['vulnerabilities'].update(package={'severity':'high'}),
            'missing dependencies': lambda x: x['metadata'].pop('dependencies'),
            'empty dependencies': lambda x: x['metadata'].update(dependencies={}),
            'zero packages': lambda x: x['metadata']['dependencies'].update(total=0),
            'boolean packages': lambda x: x['metadata']['dependencies'].update(total=True),
            'negative packages': lambda x: x['metadata']['dependencies'].update(optional=-1),
            'wrong version': lambda x: x.update(auditReportVersion=3),
            'boolean version': lambda x: x.update(auditReportVersion=True),
            'error': lambda x: x.update(error='unavailable'),
        }
        for name, change in mutations.items():
            raw=copy.deepcopy(clean); change(raw); cases.append((name,raw,0,1))
        cases.extend([('empty',{},0,1), ('nonzero',clean,1,1), ('bad exit',clean,None,1),
                      ('boolean exit',clean,False,1), ('truncated','{',0,1)])
        for name, raw, code, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root=Path(d); output=root/'output'; output.mkdir()
                def scan(argv,cwd,out,label):
                    (out/(label+'.stdout')).write_text(raw if isinstance(raw,str) else json.dumps(raw))
                    return {'exit_code':code,'execution_error':None}
                with patch.object(vex,'capture',side_effect=scan):
                    self.assertEqual(vex.run_node(root,output),expected)
                self.assertEqual(vex.load(output/'node-policy.json')['status'], 'passed' if expected==0 else 'refused')

    def test_workflow_node_is_independent_and_all_changes_trigger(self):
        workflow=Path(__file__).parents[1]/'workflows/dependency-audit.yml'
        text=workflow.read_text()
        self.assertNotIn('    paths:', text)
        self.assertIn('Independently audit Node even after Python failure\n        if: always()',text)
        self.assertIn('Retain raw Python audits and evaluate exact application scope\n        if: always()',text)
        self.assertNotIn('--ignore-vuln',text)


if __name__ == '__main__': unittest.main()
