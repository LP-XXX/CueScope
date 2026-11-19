"""A report's presence cannot substitute for a successful release check."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def release(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'tools/finalize_package.py'
    spec = importlib.util.spec_from_file_location('release_evidence_025', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    module.DIST = tmp_path / 'candidate'
    reports = module.DIST / 'validation'
    reports.mkdir(parents=True)
    (tmp_path / 'source.py').write_bytes(b'x = 1\n')
    for name in module.REQUIRED_EVIDENCE:
        data = {'passed': True, 'payload': {'ok': True}}
        if name == 'source-snapshot.json':
            data = {'source.py': hashlib.sha256(b'x = 1\n').hexdigest()}
        (reports / name).write_text(json.dumps(data), encoding='utf-8')
    return module, reports


@pytest.mark.parametrize('report', ['automated-tests.json', 'package-smoke',
                                  'capture-component-self-test', 'capture-pool-self-test'])
def test_failed_evidence_blocks_release_even_when_file_exists(release, report):
    module, reports = release
    name = report if report.endswith('.json') else module._versioned(report)
    (reports / name).write_text('{"passed":false,"payload":{"ok":false}}', encoding='utf-8')
    with pytest.raises(ValueError, match='未通过'):
        module.verify_required_evidence()


def test_matching_source_and_successful_evidence_pass(release):
    module, _ = release
    module.verify_required_evidence()


def test_source_drift_still_blocks_release(release):
    module, _ = release
    (module.ROOT / 'source.py').write_bytes(b'x = 2\n')
    with pytest.raises(ValueError, match='指纹'):
        module.verify_required_evidence()
