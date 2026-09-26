#!/usr/bin/env node
/* Offline catalog audit; English-equivalent is not proof of missing translation. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const root = path.resolve(__dirname, "..");
const sandbox = {window: {}, console: {warn() {}}};
vm.createContext(sandbox);
const read = file => fs.readFileSync(path.join(root, file), "utf8");
for (const file of ["translations", "language-extension", "locale-301", "help-content"]) {
  vm.runInContext(read(`basswiesn/app/static/js/${file}.js`), sandbox, {timeout:1000});
}
const i = sandbox.window.BasswiesnI18n;
const scopes = {};
for (const [name, file, variable, indent] of [
  ["remote", "remote.js", "messages", ""], ["restarts", "reboots.js", "messages", "  "],
  ["workbench", "js/lab-workbench.js", "words", "  "], ["dlna", "js/dlna-library.js", "copy", "  "]
]) {
  const source = read("basswiesn/app/static/" + file);
  const marker = `const ${variable} = {`;
  const start = source.indexOf(marker) + marker.length - 1;
  const end = source.indexOf(`\n${indent}};`, start) + 1 + indent.length;
  if (start < marker.length - 1 || end < start) throw Error("Catalog declaration not found: " + name);
  scopes[name] = vm.runInContext("(" + source.slice(start, end + 1) + ")", sandbox, {timeout:1000});
}
const report = {scope:"Common catalog, remote, restarts, LAB workbench, DLNA, help vocabulary and tutorials. Not a whole-application native-language certification.",
  english_equivalent:"Includes correct shared technical terms as well as fallback. Counts are not quality percentages.", languages:{}, errors:[]};
const samples = [...new Set(Object.values(i.catalogs).flatMap(Object.values)
  .concat(Object.values(i.vocabulary).flatMap(Object.values),Object.values(i.extraTerms).flat()))];
for (const lang of i.languages) {
  i.setLanguage(lang);
  const row = {common_entries:0, common_different_from_english:0, scoped_entries:0,
    scoped_different_from_english:0, help_vocabulary:0, tutorials:0, tutorial_steps:0,
    english_equivalent:[]};
  for (const key of Object.keys(i.catalogs.en)) {
    row.common_entries++;
    const value = i.catalogs[lang]?.[key] || i.catalogs.en[key];
    if (!value) report.errors.push(`${lang}:common:${key}:empty`);
    if (value !== i.catalogs.en[key]) row.common_different_from_english++;
    else if (lang !== "en") row.english_equivalent.push(`common:${key}`);
  }
  for (const [name, catalog] of Object.entries(scopes)) {
    const translated = i.scoped(catalog);
    for (const [key, value] of Object.entries(translated)) {
      row.scoped_entries++;
      if (value == null || value === "") report.errors.push(`${lang}:${name}:${key}:empty`);
      if (JSON.stringify(value) !== JSON.stringify(catalog.en[key])) row.scoped_different_from_english++;
      else if (lang !== "en") row.english_equivalent.push(`${name}:${key}`);
    }
  }
  const vocab=i.vocabulary[lang], terms=i.extraTerms[lang], tutorials=sandbox.window.BasswiesnHelpContent[lang];
  if (Object.keys(vocab || {}).length !== 52 || terms?.length !== 35 || Object.keys(tutorials || {}).length !== 10) {
    report.errors.push(lang+":missing help catalog");
  }
  row.help_vocabulary=Object.keys(vocab || {}).length+(terms?.length || 0);
  row.tutorials=Object.keys(tutorials || {}).length;
  row.tutorial_steps=Object.values(tutorials || {}).flat().length;
  for (const steps of Object.values(tutorials || {})) {
    if (steps.length !== 3 || steps.some(value => !value.trim())) report.errors.push(lang+":empty tutorial");
  }
  // Translation observers must reach a fixed point, never oscillate.
  for (const source of samples) {
    let value=source; const seen=new Set();
    for(let attempt=0; attempt<10; attempt++) {
      const next=i.dynamic(value);
      if(next===value) break;
      if(seen.has(next) || attempt===9) {report.errors.push(lang+":unstable phrase:"+source); break;}
      seen.add(value); value=next;
    }
  }
  report.languages[lang]=row;
}
if (process.argv.includes("--check")) {
  if(report.errors.length) {console.error(JSON.stringify(report.errors)); process.exitCode=1;}
  else console.log(`PASS: ${Object.keys(report.languages).length} locales; help, tutorial parity, fallback and fixed-point checks.`);
} else console.log(JSON.stringify(report, null, 2));
