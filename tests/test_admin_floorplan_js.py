"""The editor must never report success or move a marker on a failed save."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required for the editor check")
@pytest.mark.parametrize("result", ["http_error", "network_error", "success"])
def test_position_save_feedback(result):
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const status = {textContent: '', setAttribute() {}};
let pick, place, markers = 0;
const button = {dataset: {number: '1'}, classList: {add() {}, remove() {}}, addEventListener(_, fn) {pick = fn;}};
const plan = {querySelectorAll() {return [];}, querySelector() {return null;}, appendChild() {markers++;}};
const image = {addEventListener(_, fn) {place = fn;}, getBoundingClientRect() {return {left: 0, top: 0, width: 800, height: 500};}};
const document = {
 getElementById(id) {return {'picker-status': status, 'plan-inner': plan, 'plan-image': image}[id];},
 querySelectorAll() {return [button];},
 querySelector(selector) {return selector.startsWith('meta') ? {content: 'test-token'} : button;},
 createElement() {return {dataset: {}, style: {}};}
};
const fetch = async () => {
 if (process.argv[1] === 'network_error') throw new Error('offline');
 return {ok: process.argv[1] === 'success'};
};
vm.runInNewContext(fs.readFileSync('static/admin_floorplan.js', 'utf8'), {document, fetch});
(async () => {
 pick();
 await place({clientX: 100, clientY: 100});
 assert.equal(markers, process.argv[1] === 'success' ? 1 : 0);
 assert.match(status.textContent, process.argv[1] === 'success' ? /gespeichert/ : /nicht gespeichert/);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    subprocess.run(
        ["node", "-e", script, result],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
