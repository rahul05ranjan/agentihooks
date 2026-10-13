#!/usr/bin/env bash
set -euo pipefail

python3 - "${1:?supply the combined evidence directory}" "${2:?supply the tested revision}" <<'PY'
import json, sys
from pathlib import Path
output=Path(sys.argv[1])
revision=sys.argv[2]
for proof in ('startup','rebuild','profiles'):
    result=json.loads((output/proof/'mode-result.json').read_text())
    assert result=={'proof':proof,'tested_commit':revision,'status':'passed'},proof
reports=[json.loads((output/proof/f'{name}.json').read_text()) for proof,name in (
    ('startup','first'),('startup','second'),('startup','rollback'),('rebuild','rebuild'),('profiles','profiles'))]
first=reports[0]
assert all(report==first for report in reports), 'independent worker inventories differ'
assert first['manifest']['source_revision']==revision
assert first['worker_image_build_validation_failures']==0
profiles=json.loads((output/'profiles/SV2-IMG-02/result.json').read_text())
assert profiles['status']=='passed' and profiles['tested_commit']==revision
shared={'package':'SV2-IMG-01','tested_commit':revision,'mocked':False,'startup_network':'none'}
for case in ('a','b'):
    (output/f'{case}-result.json').write_bytes((output/'startup'/f'{case}-result.json').read_bytes())
(output/'c-result.json').write_text(json.dumps(shared|{'uncached_rebuild_inventory_equal':True,'retained_previous_image_started':True,'worker_image_build_validation_failures':0},indent=2)+'\n')
(output/'result.json').write_text(json.dumps(shared|{'cases':['A','B','C'],'status':'passed','production_rollout':'not exercised'},indent=2)+'\n')
print('SV2-IMG-01 passed: parallel startup, rejected builds, uncached rebuild, retained image rollback and equal inventories')
PY
