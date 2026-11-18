// Run with: node --test tests/preview_race.test.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const script = fs.readFileSync(path.join(__dirname, '..', 'src', 'kpw6_studio', 'web', 'app.js'), 'utf8');

function harness() {
  const elements = new Map();
  const pending = [];
  function element() {
    return {style: {}, classList: {toggle(){}, add(){}, remove(){}}, dataset: {},
      value: '', checked: false, innerText: '', appendChild(){}, addEventListener(){}, setAttribute(){}};
  }
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      querySelectorAll: () => [], createElement: element,
    },
    window: {addEventListener(){}},
    fetch: (url, args) => new Promise(resolve => pending.push({url, payload: args && JSON.parse(args.body), resolve})),
  });
  vm.runInContext(script, context);
  const run = code => vm.runInContext(code, context);
  const reply = (i, data) => pending[i].resolve({ok:true, json:async()=>data});
  const preview = (page, name) => ({page_num:page,total_pages:10,kpw6_pages:[],slices_meta:[],source_annotated:name});
  return {context, elements, pending, run, reply, preview};
}

test('late preview cannot replace a more recent page preview', async () => {
  const h = harness();
  const first = h.run('refreshPreview()');
  h.run('currentPage = 5');
  const second = h.run('refreshPreview()');
  h.reply(1, h.preview(5, 'page6'));
  await second;
  h.reply(0, h.preview(0, 'page1'));
  await first;
  assert.equal(h.run('currentPage'), 5);
  assert.equal(h.elements.get('sourcePreviewImg').src, 'page6');
});

test('preview from previous file cannot replace the selected file', async () => {
  const h = harness();
  const first = h.run('refreshPreview()');
  h.run('currentFileId = "new_book"');
  h.reply(0, h.preview(0, 'old_book_page1'));
  await first;
  assert.notEqual(h.elements.get('sourcePreviewImg')?.src, 'old_book_page1');
});

test('late document metadata cannot change the newer selection mode', async () => {
  const h = harness();
  const first = h.run('switchToUploadedFile("old_book", "Old")');
  const second = h.run('switchToUploadedFile("new_book", "New")');
  h.reply(1, {detected:{recommended_mode:'tech_book',reason:'new metadata'},highlights:[]});
  await new Promise(setImmediate);
  h.reply(2, h.preview(0, 'new_book_page1'));
  await second;
  h.reply(0, {detected:{recommended_mode:'pure_text',reason:'old metadata'},highlights:[]});
  await new Promise(setImmediate);
  // Let any unwanted extra request finish too, so failure is an assertion.
  if (h.pending.length > 3) h.reply(3, h.preview(0, 'new_book_page1'));
  await first;
  assert.equal(h.run('currentFileId'), 'new_book');
  assert.equal(h.run('currentMode'), 'tech_book');
  assert.equal(h.elements.get('detectBanner').innerText, '🤖 智能识别：new metadata');
});
