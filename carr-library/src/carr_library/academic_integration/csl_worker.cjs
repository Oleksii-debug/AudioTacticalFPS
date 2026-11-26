'use strict';
// Fixed local worker. Input is JSON data; it is never interpolated into code.
// No process, require, filesystem or network objects are exposed to the engine.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = __dirname;
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'UPSTREAM.json'), 'utf8'));
const processor = fs.readFileSync(path.join(root, 'vendor', 'citeproc.js'));
if (crypto.createHash('sha256').update(processor).digest('hex') !== manifest.processor.sha256) {
  throw new Error('The installed citation processor failed its integrity check.');
}
const blocks = [];
let total = 0;
process.stdin.on('data', block => {
  total += block.length;
  if (total > 16000000) {
    process.stderr.write('Citation input exceeds the processing budget.');
    process.exit(2);
  }
  blocks.push(block);
});
process.stdin.on('end', () => {
  try {
    const input = JSON.parse(Buffer.concat(blocks).toString('utf8'));
    const context = vm.createContext({input_json: JSON.stringify(input)}, {
      codeGeneration: {strings: false, wasm: false}
    });
    new vm.Script(processor.toString('utf8'), {filename: 'pinned-citeproc.js'})
      .runInContext(context, {timeout: 15000});
    const operation = `
      'use strict';
      const data = JSON.parse(input_json);
      const references = Object.create(null);
      for (const item of data.references) references[item.id] = item;
      const warnings = [];
      CSL.debug = function (message) {
        if (warnings.length < 100) warnings.push(String(message).slice(0, 2000));
      };
      const system = {
        retrieveLocale: function (language) {
          if (data.locales[language]) return data.locales[language];
          const prefix = String(language).split('-')[0];
          const match = Object.keys(data.locales).find(x => x.split('-')[0] === prefix);
          return data.locales[match || 'en-US'];
        },
        retrieveItem: function (id) {
          if (!Object.prototype.hasOwnProperty.call(references, id)) {
            throw new Error('Unknown citation reference.');
          }
          return references[id];
        }
      };
      const engine = new CSL.Engine(system, data.style_xml, data.locale, true);
      engine.setOutputFormat('html');
      const ordered = [];
      for (const cluster of data.clusters) {
        for (const item of cluster.items) if (!ordered.includes(item.id)) ordered.push(item.id);
      }
      for (const item of data.references) if (!ordered.includes(item.id)) ordered.push(item.id);
      engine.updateItems(ordered);
      const citations = [];
      for (let index = 0; index < data.clusters.length; index++) {
        const cluster = data.clusters[index];
        const citation = {
          citationID: cluster.id, citationItems: cluster.items,
          properties: {noteIndex: cluster.note_index || index + 1}
        };
        const updates = engine.appendCitationCluster(citation);
        for (const entry of updates) citations[entry[0]] = entry[1];
      }
      const bibliography = engine.makeBibliography();
      if (!bibliography) throw new Error('The style has no bibliography.');
      JSON.stringify({
        bibliography: bibliography[1], parameters: bibliography[0], citations,
        warnings, processor_version: CSL.PROCESSOR_VERSION
      });
    `;
    const result = new vm.Script(operation, {filename: 'citation-operation'})
      .runInContext(context, {timeout: 20000});
    if (Buffer.byteLength(result, 'utf8') > 16000000) throw new Error('Output budget exceeded.');
    process.stdout.write(result);
  } catch (error) {
    // Avoid publishing source text, URLs, credentials or engine debug traces.
    process.stderr.write('Citation processing failed (' + String(error.name || 'Error') + ').');
    process.exitCode = 2;
  }
});
