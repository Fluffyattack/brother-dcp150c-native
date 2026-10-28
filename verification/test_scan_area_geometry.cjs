/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const vm = require("node:vm");
const source = readFileSync(join(__dirname, "../dcp150c/static/scan-area.js"), "utf8");
const context = vm.createContext({});
vm.runInContext(source.split("// DOM event bindings")[0], context);
const evaluate = (expression) => vm.runInContext(expression, context);
const plain = (expression) => JSON.parse(evaluate(`JSON.stringify(${expression})`));
evaluate('var frame = {left:10,top:20,width:200,height:300}; var selection = {left:0.25,top:0.5,width:0.5,height:0.25};');
assert.deepEqual(plain("scanAreaFromSelection(frame,selection)"), {left:60,top:170,width:100,height:75});
console.log("PASS: a drawn selection becomes the physical scanner coordinates, including a nonzero frame origin");
assert.deepEqual(plain("scanSelectionFromArea(frame,scanAreaFromSelection(frame,selection))"), {left:0.25,top:0.5,width:0.5,height:0.25});
console.log("PASS: scanner coordinates restore the same image selection without depending on preview DPI");
assert.equal(evaluate('scanAreaWithinGlass({left:10,top:20,width:100,height:75},{width:210,height:297})'),true);
for(const area of [{left:-1,top:0,width:10,height:10},{left:0,top:0,width:0.2,height:10},{left:200,top:0,width:20,height:10},{left:0,top:0,width:20,height:400}]){
 assert.equal(evaluate(`scanAreaWithinGlass(${JSON.stringify(area)},{width:210,height:297})`),false);
}
assert.equal(evaluate('scanAreaWithinGlass({left:0,top:0,width:Infinity,height:10},{width:210,height:297})'),false);
console.log("PASS: areas outside the glass and invalid or submillimetre dimensions are refused");
evaluate('selectedPhysicalScanArea={left:60,top:170,width:100,height:75}');
const settings=plain('settingsWithScanArea({id:"scan-form"},{resolution:300,scan_area_left:"60",scan_area_top:"170",scan_area_width:"100",scan_area_height:"75"})');
assert.deepEqual(settings,{resolution:300,area:{left:60,top:170,width:100,height:75}});
evaluate('selectedPhysicalScanArea.left=80');
assert.equal(settings.area.left,60);
console.log("PASS: scan and profile requests contain independent millimetre settings rather than form-only coordinates");
evaluate('var copyAreaSelected=false; function findElementById(){return{get checked(){return copyAreaSelected}}}');
assert.deepEqual(plain('settingsWithScanArea({id:"copy-form"},{copies:2,use_scan_area:"on"})'),{copies:2});
evaluate('copyAreaSelected=true');
assert.equal(plain('settingsWithScanArea({id:"copy-form"},{copies:2,use_scan_area:"on"})').area.left,80);
console.log("PASS: copying uses the selected area only after the user explicitly enables it");
